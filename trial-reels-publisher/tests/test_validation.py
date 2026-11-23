from pathlib import Path

import pytest

from conftest import IG_ID, fake_mp4
from trial_reels import validation as v
from trial_reels.errors import VideoValidationError
from trial_reels.models import make_job_key


def write(tmp_path, data, name="clip.mp4"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_valid_structure_and_ffprobe_missing_warning(tmp_path):
    r = v.validate_video(write(tmp_path, fake_mp4()))
    assert r.size_bytes > 0 and len(r.sha256) == 64
    assert any("ffprobe not installed" in w for w in r.warnings)
    assert not r.metadata_checked


def test_empty_file(tmp_path):
    with pytest.raises(VideoValidationError, match="empty"):
        v.validate_video(write(tmp_path, b""))


def test_truncated_file(tmp_path):
    data = fake_mp4()
    with pytest.raises(VideoValidationError, match="truncated"):
        v.validate_video(write(tmp_path, data[:-100]))


def test_not_an_mp4(tmp_path):
    with pytest.raises(VideoValidationError):
        v.validate_video(write(tmp_path, b"<html>this is not a video</html>" * 10))


def test_missing_moov(tmp_path):
    data = v.struct.pack(">I4s", 12, b"ftyp") + b"isom" + v.struct.pack(">I4s", 16, b"mdat") + b"x" * 8
    with pytest.raises(VideoValidationError, match="moov"):
        v.validate_video(write(tmp_path, data))


def test_moov_at_end_flags_faststart_not_error(tmp_path):
    r = v.validate_video(write(tmp_path, fake_mp4(moov_first=False), name="IMG_1.MOV"))
    assert r.needs_faststart is True
    assert v.validate_video(write(tmp_path, fake_mp4(), name="b.mov")).needs_faststart is False


def test_mov_metadata_accepted(tmp_path):
    r = report(tmp_path)
    v.apply_metadata_rules(r, probe(video={"codec_name": "hevc"},
                                    format={"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}))
    assert r.video_codec == "hevc" and r.metadata_checked


def test_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "MAX_FILE_BYTES", 100)
    with pytest.raises(VideoValidationError, match="300 MB"):
        v.validate_video(write(tmp_path, fake_mp4()))


def test_size_changing_during_validation(tmp_path, monkeypatch):
    p = write(tmp_path, fake_mp4())
    calls = {"n": 0}

    def grow(_):
        calls["n"] += 1
        with open(p, "ab") as f:
            f.write(b"more")
    monkeypatch.setattr(v.time, "sleep", grow)
    with pytest.raises(VideoValidationError, match="still changing"):
        v._orig_wait_for_stable_size(p, checks=1, interval_s=0)


def probe(**over):
    video = {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920,
             "avg_frame_rate": "30/1", "pix_fmt": "yuv420p", "bit_rate": "8000000"}
    audio = {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2,
             "bit_rate": "128000"}
    video.update(over.pop("video", {}))
    audio.update(over.pop("audio", {}))
    fmt = {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "12.5"}
    fmt.update(over.pop("format", {}))
    return {"streams": [video, audio], "format": fmt}


def report(tmp_path):
    return v.VideoReport(path=tmp_path / "x.mp4", size_bytes=1, sha256="0" * 64)


def test_metadata_good(tmp_path):
    r = report(tmp_path)
    v.apply_metadata_rules(r, probe())
    assert r.metadata_checked and r.width == 1080 and r.duration_s == 12.5 and not r.warnings


@pytest.mark.parametrize("over,msg", [
    ({"video": {"codec_name": "vp9"}}, "H.264 or HEVC"),
    ({"audio": {"codec_name": "opus"}}, "AAC"),
    ({"format": {"duration": "1.0"}}, "3 s minimum"),
    ({"format": {"duration": "1000"}}, "15 min"),
    ({"video": {"width": 3840, "height": 2160}}, "1920px"),
    ({"video": {"avg_frame_rate": "120/1"}}, "23-60 fps"),
    ({"format": {"format_name": "matroska,webm"}}, "MP4 or MOV"),
])
def test_metadata_fatal(tmp_path, over, msg):
    with pytest.raises(VideoValidationError, match=msg):
        v.apply_metadata_rules(report(tmp_path), probe(**over))


def test_rotated_iphone_video_and_aspect_warning(tmp_path):
    r = report(tmp_path)
    v.apply_metadata_rules(r, probe(video={"width": 1920, "height": 1080,
                                           "side_data_list": [{"rotation": -90}]}))
    assert (r.width, r.height) == (1080, 1920) and not r.warnings
    r2 = report(tmp_path)
    v.apply_metadata_rules(r2, probe(video={"width": 1080, "height": 1080}))
    assert any("9:16" in w for w in r2.warnings)


def test_hash_and_job_key_stable_and_content_sensitive(tmp_path):
    p = write(tmp_path, fake_mp4())
    h1 = v.sha256_file(p)
    assert h1 == v.sha256_file(p)
    k1 = make_job_key(IG_ID, p.resolve(), h1)
    assert k1 == make_job_key(IG_ID, p.resolve(), v.sha256_file(p))
    p.write_bytes(fake_mp4(payload=b"y" * 2048))
    h2 = v.sha256_file(p)
    assert h2 != h1 and make_job_key(IG_ID, p.resolve(), h2) != k1
    assert make_job_key("999", p.resolve(), h2) != make_job_key(IG_ID, p.resolve(), h2)
