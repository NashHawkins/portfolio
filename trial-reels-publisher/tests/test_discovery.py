import os

import pytest

from conftest import fake_mp4
from trial_reels import discovery
from trial_reels.discovery import SingleFileSource
from trial_reels.errors import DiscoveryError


def test_missing_folder(tmp_path):
    with pytest.raises(DiscoveryError, match="not found"):
        SingleFileSource(tmp_path / "nope").discover()


def test_unreadable_folder(videos_dir, monkeypatch):
    def denied(self):
        raise PermissionError
    monkeypatch.setattr(type(videos_dir), "iterdir", denied)
    with pytest.raises(DiscoveryError, match="Privacy & Security"):
        SingleFileSource(videos_dir).discover()


def test_icloud_placeholder_file(videos_dir):
    (videos_dir / ".clip.mp4.icloud").write_bytes(b"stub")
    with pytest.raises(DiscoveryError, match="iCloud has not downloaded: clip.mp4"):
        SingleFileSource(videos_dir).discover()


def test_dataless_flag(videos_dir, monkeypatch):
    (videos_dir / "a.mp4").write_bytes(fake_mp4())
    real_stat = os.stat

    class St:
        def __init__(self, s):
            self.__dict__.update({k: getattr(s, k) for k in dir(s) if k.startswith("st_")})
            self.st_flags = discovery.SF_DATALESS
    monkeypatch.setattr(discovery.os, "stat", lambda p: St(real_stat(p)))
    with pytest.raises(DiscoveryError, match="iCloud placeholder"):
        SingleFileSource(videos_dir).discover()


def test_zero_supported_videos(videos_dir):
    (videos_dir / "IMG_1.mkv").write_bytes(b"x")
    (videos_dir / "clip.avi").write_bytes(b"x")
    with pytest.raises(DiscoveryError, match="No .mp4 or .mov video.*IMG_1.mkv"):
        SingleFileSource(videos_dir).discover()


def test_two_mp4(videos_dir):
    (videos_dir / "a.mp4").write_bytes(fake_mp4())
    (videos_dir / "b.mp4").write_bytes(fake_mp4())
    with pytest.raises(DiscoveryError, match=r"Found 2 videos \(.mp4/.mov\)"):
        SingleFileSource(videos_dir).discover()


def test_one_mp4_with_ds_store(videos_dir):
    (videos_dir / ".DS_Store").write_bytes(b"\0" * 10)
    (videos_dir / "notes.txt").write_text("hi")
    (videos_dir / "clip.mp4").write_bytes(fake_mp4())
    assert [p.name for p in SingleFileSource(videos_dir).discover()] == ["clip.mp4"]


@pytest.mark.parametrize("name", ["CLIP.MP4", "my reel ☀️ café.Mp4", "IMG_3813.MOV", "clip.mov", "x.MoV"])
def test_uppercase_and_unicode_names(videos_dir, name):
    (videos_dir / name).write_bytes(fake_mp4())
    assert SingleFileSource(videos_dir).discover()[0].name == name


def test_directory_named_mp4_and_nested_ignored(videos_dir):
    (videos_dir / "folder.mp4").mkdir()
    nested = videos_dir / "2026-11-15"
    nested.mkdir()
    (nested / "01-hook.mp4").write_bytes(fake_mp4())
    with pytest.raises(DiscoveryError, match="No .mp4 or .mov"):
        SingleFileSource(videos_dir).discover()
    (videos_dir / "real.mp4").write_bytes(fake_mp4())
    assert [p.name for p in SingleFileSource(videos_dir).discover()] == ["real.mp4"]


def test_mp4_plus_mov_is_ambiguous(videos_dir):
    (videos_dir / "IMG_1.MOV").write_bytes(fake_mp4())
    (videos_dir / "IMG_1.mp4").write_bytes(fake_mp4())
    with pytest.raises(DiscoveryError, match="needs exactly one.*IMG_1.MOV, IMG_1.mp4"):
        SingleFileSource(videos_dir).discover()


def test_directory_named_mov_ignored(videos_dir):
    (videos_dir / "folder.MOV").mkdir()
    (videos_dir / "a.mov").write_bytes(fake_mp4())
    assert [p.name for p in SingleFileSource(videos_dir).discover()] == ["a.mov"]


def test_icloud_placeholder_mov(videos_dir):
    (videos_dir / ".IMG_9.MOV.icloud").write_bytes(b"stub")
    with pytest.raises(DiscoveryError, match="iCloud has not downloaded: IMG_9.MOV"):
        SingleFileSource(videos_dir).discover()
