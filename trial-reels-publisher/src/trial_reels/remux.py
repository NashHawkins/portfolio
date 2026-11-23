"""Prepare the bytes that get uploaded, without ever touching the original file.

Meta's Reels spec asks for the 'moov' atom at the front of the file ("fast start").
iPhone .MOV files usually have it at the end. When that happens we make a temporary
stream-copy remux (no re-encode) with:

    ffmpeg -i <original> -map 0:v:0 -map 0:a:0? -c copy -movflags +faststart <tmp>/upload.mp4

The temporary copy lives under data/tmp/ (never inside the video folder), is validated
like any other upload, and is deleted as soon as the upload step ends. Identity and
duplicate protection always use the ORIGINAL file's path and SHA-256 (see models.VideoJob).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .errors import PublisherError, redact
from .models import VideoJob
from .validation import check_mp4_structure, sha256_file, validate_video

log = logging.getLogger(__name__)


class RemuxError(PublisherError):
    pass


FFMPEG_MISSING_HELP = (
    "This video's 'moov' atom is at the end of the file. Meta requires it at the front, so a "
    "fast-start copy has to be made, and that needs ffmpeg, which isn't installed. Nothing was "
    "posted and your file was not changed.\n"
    "Fix either way:\n"
    "  1. Install ffmpeg (for example `brew install ffmpeg` if you use Homebrew) and run again; or\n"
    "  2. Re-export the video yourself with 'optimize for network use' / fast start enabled."
)


@dataclass
class PreparedUpload:
    path: Path              # file whose bytes are uploaded (original or temp copy)
    size_bytes: int
    sha256: str
    remuxed: bool
    tmp_dir: Optional[Path] = None


def find_ffmpeg() -> Optional[str]:
    return os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")


class Remuxer:
    def __init__(self, tmp_root: Path, ffmpeg: Optional[str] = "auto",
                 run: Callable = subprocess.run, timeout_s: float = 900.0,
                 validator: Callable = validate_video):
        self.tmp_root = Path(tmp_root)
        self.ffmpeg = find_ffmpeg() if ffmpeg == "auto" else ffmpeg
        self._run = run
        self.timeout_s = timeout_s
        self._validate = validator

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg)

    def ensure_possible(self, job: VideoJob) -> None:
        """Raise before any API write if a remux is needed but impossible."""
        if job.needs_faststart and not self.available:
            raise RemuxError(FFMPEG_MISSING_HELP)

    # ------------------------------------------------------------------ prepare
    def prepare(self, job: VideoJob) -> PreparedUpload:
        if not job.needs_faststart:
            return PreparedUpload(job.path, job.size_bytes, job.sha256, remuxed=False)
        self.ensure_possible(job)

        src = Path(job.path)
        before = _fingerprint(src)
        if before[0] != job.size_bytes:
            raise RemuxError(f"{src.name} changed since validation; run again.")

        tmp_dir = self._job_dir(job)
        self.remove_dir(tmp_dir)
        tmp_dir.mkdir(parents=True, mode=0o700)
        out = tmp_dir / "upload.mp4"
        if out.resolve() == src.resolve():  # paranoia: never write onto the source
            raise RemuxError("refusing to remux onto the original file")

        cmd = [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
               "-i", str(src),
               "-map", "0:v:0", "-map", "0:a:0?",      # first video + first audio; drop data/timecode tracks
               "-c", "copy",                           # stream copy only: never re-encode
               "-map_metadata", "0",
               "-movflags", "+faststart"]
        if job.video_codec == "hevc":
            cmd += ["-tag:v", "hvc1"]                  # keep Apple-compatible HEVC tag in MP4
        cmd.append(str(out))

        try:
            try:
                proc = self._run(cmd, capture_output=True, text=True, timeout=self.timeout_s, check=False)
            except subprocess.TimeoutExpired:
                raise RemuxError(f"ffmpeg took longer than {self.timeout_s:.0f}s; nothing was posted.")
            except OSError as e:
                raise RemuxError(f"could not run ffmpeg ({self.ffmpeg}): {e}")
            if proc.returncode != 0:
                tail = redact((proc.stderr or "").strip())[-800:]
                raise RemuxError(f"ffmpeg could not make a fast-start copy (exit {proc.returncode}). "
                                 f"Nothing was posted; original unchanged.\n{tail}")
            if _fingerprint(src) != before:
                raise RemuxError(f"{src.name} changed while the upload copy was being made; run again.")
            if not out.is_file() or out.stat().st_size == 0:
                raise RemuxError("ffmpeg reported success but produced no output file.")
            if check_mp4_structure(out):
                raise RemuxError("the remuxed copy still has its 'moov' atom at the end; not uploading.")
            report = self._validate(out, stable_checks=0)  # size limit + ffprobe rules on the actual upload
            if (job.duration_s and report.duration_s
                    and abs(job.duration_s - report.duration_s) > 0.5):
                raise RemuxError(f"remuxed copy duration {report.duration_s:.2f}s differs from original "
                                 f"{job.duration_s:.2f}s; not uploading.")
            return PreparedUpload(out, report.size_bytes, report.sha256, remuxed=True, tmp_dir=tmp_dir)
        except BaseException:  # any failure (incl. Ctrl-C): leave no temp copy behind
            self.remove_dir(tmp_dir)
            raise

    # ------------------------------------------------------------------ cleanup
    def _job_dir(self, job: VideoJob) -> Path:
        return self.tmp_root / job.job_key[:24]

    def remove_dir(self, d: Optional[Path]) -> None:
        """Delete one of OUR temp directories. Refuses anything outside tmp_root."""
        if d is None or not d.exists():
            return
        root = self.tmp_root.resolve()
        target = d.resolve()
        if target == root or root not in target.parents or d.is_symlink():
            raise RemuxError(f"refusing to delete {d}: not a temp directory owned by this tool")
        shutil.rmtree(target)

    def cleanup(self, prepared: Optional[PreparedUpload]) -> None:
        if prepared is not None and prepared.remuxed:
            self.remove_dir(prepared.tmp_dir)

    def cleanup_stale(self) -> int:
        """Remove leftovers from a crashed run. Call only while holding the process lock."""
        if not self.tmp_root.is_dir():
            return 0
        n = 0
        for child in self.tmp_root.iterdir():
            if child.is_dir() and not child.is_symlink():
                self.remove_dir(child)
                n += 1
        return n


def _fingerprint(p: Path):
    st = p.stat()
    return (st.st_size, st.st_mtime_ns, st.st_ino)
