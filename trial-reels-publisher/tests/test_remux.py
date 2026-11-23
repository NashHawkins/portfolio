import os
import subprocess
from pathlib import Path

import pytest

from conftest import IG_ID, fake_mp4
from trial_reels import validation as v
from trial_reels.models import VideoJob
from trial_reels.remux import FFMPEG_MISSING_HELP, Remuxer, RemuxError


class FakeFFmpeg:
    """Stands in for subprocess.run: writes a fast-start file to the output path."""

    def __init__(self, returncode=0, output=None, stderr="", on_run=None):
        self.returncode, self.output, self.stderr, self.on_run = returncode, output, stderr, on_run
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if self.on_run:
            self.on_run(cmd)
        if self.returncode == 0:
            Path(cmd[-1]).write_bytes(self.output if self.output is not None else fake_mp4(payload=b"R" * 2048))
        return subprocess.CompletedProcess(cmd, self.returncode, "", self.stderr)


def make_job(path: Path, codec="hevc", needs=True):
    rep = v.validate_video(path, stable_checks=0)
    return VideoJob(path=path.resolve(), account_id=IG_ID, caption="", graduation_strategy="MANUAL",
                    sha256=rep.sha256, size_bytes=rep.size_bytes, needs_faststart=needs, video_codec=codec)


@pytest.fixture
def mov(videos_dir):
    p = videos_dir / "IMG_3813.MOV"
    p.write_bytes(fake_mp4(moov_first=False))
    return p


def snapshot(p):
    st = p.stat()
    return p.read_bytes(), st.st_mtime_ns, st.st_size


def test_no_remux_needed_uses_original(tmp_path, videos_dir):
    p = videos_dir / "a.mp4"
    p.write_bytes(fake_mp4())
    fake = FakeFFmpeg()
    r = Remuxer(tmp_path / "tmp", ffmpeg=None, run=fake)
    prep = r.prepare(make_job(p, needs=False))
    assert prep.path == p.resolve() and not prep.remuxed and fake.calls == []


def test_remux_command_output_and_original_untouched(tmp_path, mov):
    before = snapshot(mov)
    fake = FakeFFmpeg()
    tmp_root = tmp_path / "data" / "tmp"
    r = Remuxer(tmp_root, ffmpeg="/opt/ffmpeg", run=fake)
    job = make_job(mov)
    prep = r.prepare(job)

    cmd = fake.calls[0]
    assert cmd[0] == "/opt/ffmpeg"
    assert cmd[cmd.index("-i") + 1] == str(mov.resolve())
    assert cmd[cmd.index("-c") + 1] == "copy" and cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert "-n" in cmd and "-nostdin" in cmd
    assert cmd[cmd.index("-tag:v") + 1] == "hvc1"
    assert not any(c in cmd for c in ("-c:v", "libx264", "libx265", "-crf"))  # never re-encode
    assert Path(cmd[-1]).parent.parent == tmp_root and cmd[-1].endswith(".mp4")

    assert prep.remuxed and prep.path.exists() and prep.sha256 != job.sha256
    assert v.check_mp4_structure(prep.path) is False  # moov now first
    assert snapshot(mov) == before
    assert mov.parent not in prep.path.parents  # temp copy is outside the video folder

    r.cleanup(prep)
    assert not prep.tmp_dir.exists() and snapshot(mov) == before


def test_h264_gets_no_hevc_tag(tmp_path, mov):
    fake = FakeFFmpeg()
    Remuxer(tmp_path / "tmp", ffmpeg="ffmpeg", run=fake).prepare(make_job(mov, codec="h264"))
    assert "-tag:v" not in fake.calls[0]


def test_ffmpeg_missing_explains_and_runs_nothing(tmp_path, mov):
    fake = FakeFFmpeg()
    r = Remuxer(tmp_path / "tmp", ffmpeg=None, run=fake)
    with pytest.raises(RemuxError) as ei:
        r.prepare(make_job(mov))
    assert "ffmpeg" in str(ei.value) and "Nothing was posted" in str(ei.value)
    assert "brew install ffmpeg" in FFMPEG_MISSING_HELP
    assert fake.calls == []


def test_ffmpeg_failure_cleans_up(tmp_path, mov):
    before = snapshot(mov)
    r = Remuxer(tmp_path / "tmp", ffmpeg="ffmpeg", run=FakeFFmpeg(returncode=1, stderr="Invalid data"))
    with pytest.raises(RemuxError, match="(?s)exit 1.*Invalid data"):
        r.prepare(make_job(mov))
    assert list((tmp_path / "tmp").iterdir()) == []
    assert snapshot(mov) == before


def test_output_still_not_faststart_rejected(tmp_path, mov):
    r = Remuxer(tmp_path / "tmp", ffmpeg="ffmpeg", run=FakeFFmpeg(output=fake_mp4(moov_first=False)))
    with pytest.raises(RemuxError, match="still has its 'moov' atom at the end"):
        r.prepare(make_job(mov))
    assert list((tmp_path / "tmp").iterdir()) == []


def test_invalid_output_rejected(tmp_path, mov):
    r = Remuxer(tmp_path / "tmp", ffmpeg="ffmpeg", run=FakeFFmpeg(output=b"garbage" * 10))
    with pytest.raises(Exception):
        r.prepare(make_job(mov))
    assert list((tmp_path / "tmp").iterdir()) == []


def test_source_changing_during_remux_rejected(tmp_path, mov):
    def touch(cmd):
        with open(mov, "ab") as f:
            f.write(b"x")
    r = Remuxer(tmp_path / "tmp", ffmpeg="ffmpeg", run=FakeFFmpeg(on_run=touch))
    with pytest.raises(RemuxError, match="changed while"):
        r.prepare(make_job(mov))


def test_duration_mismatch_rejected(tmp_path, mov):
    job = make_job(mov)
    job = VideoJob(**{**job.__dict__, "duration_s": 10.0})

    def validator(path, stable_checks=0):
        rep = v.validate_video(path, stable_checks=0)
        rep.duration_s = 7.0
        return rep
    r = Remuxer(tmp_path / "tmp", ffmpeg="ffmpeg", run=FakeFFmpeg(), validator=validator)
    with pytest.raises(RemuxError, match="duration"):
        r.prepare(job)


def test_remove_dir_refuses_outside_tmp_root(tmp_path, mov):
    r = Remuxer(tmp_path / "tmp", ffmpeg="ffmpeg")
    (tmp_path / "tmp").mkdir()
    for bad in (mov.parent, tmp_path / "tmp", tmp_path):
        with pytest.raises(RemuxError, match="refusing to delete"):
            r.remove_dir(bad)
    assert mov.exists()


def test_cleanup_stale(tmp_path):
    root = tmp_path / "tmp"
    (root / "abc").mkdir(parents=True)
    (root / "abc" / "upload.mp4").write_bytes(b"x")
    assert Remuxer(root, ffmpeg=None).cleanup_stale() == 1
    assert list(root.iterdir()) == []
