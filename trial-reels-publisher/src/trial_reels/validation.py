"""Local file checks: completeness, MP4 structure, hashing and (optional) ffprobe metadata.

Hard limits come from Meta's IG User Media reference (Reels specs, checked Nov 2026):
MOV/MP4, H.264/HEVC, AAC <=48 kHz 1-2 ch, 23-60 fps, <=1920 px wide,
aspect 0.01:1..10:1 (9:16 recommended), <=25 Mbps video, 128 kbps audio,
3 s..15 min, 300 MB max. (The official sample README still says 1 GB; we use the
smaller, newer limit.)
"""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import List, Optional, Tuple

from .errors import VideoValidationError, redact

MAX_FILE_BYTES = 300 * 1024 * 1024
MIN_DURATION_S = 3.0
MAX_DURATION_S = 15 * 60.0
MIN_FPS, MAX_FPS = 23.0, 60.0
MAX_WIDTH = 1920
MAX_VIDEO_BPS = 25_000_000
RECOMMENDED_AUDIO_BPS = 128_000
MAX_AUDIO_HZ = 48_000


@dataclass
class VideoReport:
    path: Path
    size_bytes: int
    sha256: str
    duration_s: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    metadata_checked: bool = False
    # moov atom after media data: Meta asks for "moov at the front", so a fast-start copy is needed
    needs_faststart: bool = False
    warnings: List[str] = field(default_factory=list)


def wait_for_stable_size(path: Path, checks: int = 2, interval_s: float = 1.0) -> int:
    """Fail if the file is still being written/copied/downloaded."""
    last = path.stat().st_size
    for _ in range(checks):
        time.sleep(interval_s)
        size = path.stat().st_size
        if size != last:
            raise VideoValidationError(f"{path.name} is still changing size ({last} -> {size} bytes). "
                                       "Wait for the copy/download to finish, then run again.")
    return last


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def read_top_level_boxes(path: Path) -> List[Tuple[str, int, int]]:
    """Return (type, offset, size) for top-level ISO-BMFF boxes. Detects truncation."""
    boxes = []
    file_size = path.stat().st_size
    with open(path, "rb") as f:
        offset = 0
        while offset < file_size:
            f.seek(offset)
            header = f.read(8)
            if len(header) < 8:
                raise VideoValidationError(f"{path.name}: truncated MP4 (incomplete box header).")
            size, kind = struct.unpack(">I4s", header)
            try:
                kind_s = kind.decode("ascii")
            except UnicodeDecodeError:
                raise VideoValidationError(f"{path.name} is not a valid MP4 file (bad box type).")
            if size == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    raise VideoValidationError(f"{path.name}: truncated MP4.")
                size = struct.unpack(">Q", ext)[0]
            elif size == 0:
                size = file_size - offset
            if size < 8:
                raise VideoValidationError(f"{path.name} is not a valid MP4 file (bad box size).")
            if offset + size > file_size:
                raise VideoValidationError(f"{path.name}: truncated MP4 ('{kind_s}' box runs past "
                                           "end of file). The file may be incomplete.")
            boxes.append((kind_s, offset, size))
            offset += size
    return boxes


def check_mp4_structure(path: Path) -> bool:
    """Validate top-level MP4/MOV boxes. Returns True if 'moov' comes after 'mdat' (not fast start)."""
    boxes = read_top_level_boxes(path)
    kinds = [b[0] for b in boxes]
    if not kinds or kinds[0] != "ftyp":
        raise VideoValidationError(f"{path.name} does not start with an MP4/MOV 'ftyp' box; "
                                   "it is not a valid MP4/MOV file.")
    if "moov" not in kinds:
        raise VideoValidationError(f"{path.name} has no 'moov' box (incomplete or corrupt video).")
    if "mdat" not in kinds:
        raise VideoValidationError(f"{path.name} has no 'mdat' media data (incomplete or corrupt).")
    return kinds.index("moov") > kinds.index("mdat")


def _ratio(value: Optional[str]) -> Optional[float]:
    if not value or value in ("0/0", "N/A"):
        return None
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None


def run_ffprobe(path: Path, ffprobe: str) -> dict:
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except subprocess.TimeoutExpired:
        raise VideoValidationError(f"ffprobe timed out reading {path.name}.")
    if out.returncode != 0:
        raise VideoValidationError(f"ffprobe could not read {path.name}: {redact(out.stderr.strip())[:500]}")
    return json.loads(out.stdout or "{}")


def apply_metadata_rules(report: VideoReport, probe: dict) -> None:
    errors: List[str] = []
    warn = report.warnings.append
    streams = probe.get("streams", [])
    fmt = probe.get("format", {})
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    fmt_name = fmt.get("format_name", "")
    if "mp4" not in fmt_name and "mov" not in fmt_name:
        errors.append(f"container is '{fmt_name}', Meta requires MP4 or MOV")

    if video is None:
        errors.append("no video stream")
    else:
        report.video_codec = video.get("codec_name")
        if report.video_codec not in ("h264", "hevc"):
            errors.append(f"video codec is '{report.video_codec}', Meta requires H.264 or HEVC")
        report.width, report.height = video.get("width"), video.get("height")
        rotation = _rotation(video)
        if rotation in (90, 270) and report.width and report.height:
            report.width, report.height = report.height, report.width
        if report.width and report.width > MAX_WIDTH:
            errors.append(f"width {report.width}px exceeds Meta's {MAX_WIDTH}px maximum")
        if report.width and report.height:
            aspect = report.width / report.height
            if not (0.01 <= aspect <= 10):
                errors.append(f"aspect ratio {aspect:.3f} is outside Meta's 0.01:1-10:1 range")
            elif abs(aspect - 9 / 16) > 0.02:
                warn(f"aspect ratio is {report.width}x{report.height}; Meta recommends 9:16 "
                     "to avoid cropping or blank space")
        report.fps = _ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate"))
        if report.fps is not None and not (MIN_FPS - 0.5 <= report.fps <= MAX_FPS + 0.5):
            errors.append(f"frame rate {report.fps:.2f} fps is outside Meta's 23-60 fps range")
        pix = video.get("pix_fmt", "")
        if pix and "420" not in pix:
            warn(f"pixel format '{pix}' is not 4:2:0 chroma subsampling, which Meta requires")
        vbr = _int(video.get("bit_rate"))
        if vbr and vbr > MAX_VIDEO_BPS:
            errors.append(f"video bitrate {vbr / 1e6:.1f} Mbps exceeds Meta's 25 Mbps maximum")

    if audio is not None:
        report.audio_codec = audio.get("codec_name")
        if report.audio_codec != "aac":
            errors.append(f"audio codec is '{report.audio_codec}', Meta requires AAC")
        hz = _int(audio.get("sample_rate"))
        if hz and hz > MAX_AUDIO_HZ:
            errors.append(f"audio sample rate {hz} Hz exceeds Meta's 48 kHz maximum")
        ch = _int(audio.get("channels"))
        if ch and ch > 2:
            errors.append(f"audio has {ch} channels; Meta allows mono or stereo")
        abr = _int(audio.get("bit_rate"))
        if abr and abr > RECOMMENDED_AUDIO_BPS * 1.1:
            warn(f"audio bitrate {abr // 1000} kbps is above Meta's stated 128 kbps")

    duration = _float(fmt.get("duration"))
    report.duration_s = duration
    if duration is not None:
        if duration < MIN_DURATION_S:
            errors.append(f"duration {duration:.1f}s is shorter than Meta's 3 s minimum")
        if duration > MAX_DURATION_S:
            errors.append(f"duration {duration:.0f}s is longer than Meta's 15 min maximum")

    report.metadata_checked = True
    if errors:
        raise VideoValidationError(f"{report.path.name} does not meet Meta's Reels requirements:\n  - "
                                   + "\n  - ".join(errors)
                                   + "\nThe file was not changed. Re-export it to fix these.")


def _rotation(stream: dict) -> int:
    rot = (stream.get("tags") or {}).get("rotate")
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = sd["rotation"]
    try:
        return abs(int(float(rot))) % 360 if rot is not None else 0
    except ValueError:
        return 0


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def validate_video(path: Path, *, ffprobe: Optional[str] = "auto",
                   stable_checks: int = 2, stable_interval_s: float = 1.0) -> VideoReport:
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as e:
        raise VideoValidationError(f"Cannot read {path.name}: {e}")
    if size == 0:
        raise VideoValidationError(f"{path.name} is empty (0 bytes).")
    if size > MAX_FILE_BYTES:
        raise VideoValidationError(f"{path.name} is {size / 1e6:.0f} MB; Meta's Reels limit is 300 MB.")
    if stable_checks:
        size = wait_for_stable_size(path, stable_checks, stable_interval_s)

    needs_faststart = check_mp4_structure(path)
    report = VideoReport(path=path, size_bytes=size, sha256=sha256_file(path),
                         needs_faststart=needs_faststart)

    exe = shutil.which("ffprobe") if ffprobe == "auto" else ffprobe
    if exe:
        apply_metadata_rules(report, run_ffprobe(path, exe))
    else:
        report.warnings.append("ffprobe not installed, so codec/duration/resolution were NOT checked "
                               "locally (Meta will still reject a bad file). Install with: brew install ffmpeg")

    if path.stat().st_size != size:
        raise VideoValidationError(f"{path.name} changed while being validated. Run again.")
    return report
