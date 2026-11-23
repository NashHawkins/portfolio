"""Trial Reels must use video_url, and a published TRIAL request is never reported as a verified Trial Reel.

Runs WITHOUT the legacy_resumable_trial fixture: this is production behaviour. Offline only."""
import dataclasses
import json

import pytest

from conftest import FakeResponse, IG_ID, fake_mp4, happy_session
from test_url_mode import URL, hosted_ok, run as run_url, store, writes
from trial_reels import cli, models
from trial_reels.errors import PublisherError
from trial_reels.meta_client import MetaClient, reel_container_params, url_reel_container_body
from trial_reels.models import PUBLIC, RESUMABLE, TRIAL, URL as URL_METHOD, VideoJob
from trial_reels.publisher import build_job, mode_check, publish_job
from trial_reels.remux import Remuxer
from trial_reels.validation import validate_video


class TTY:
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def interactive(monkeypatch):
    monkeypatch.setattr("sys.stdin", TTY())


@pytest.fixture
def video(videos_dir):
    p = videos_dir / "IMG_3813.MOV"
    p.write_bytes(fake_mp4())
    return p


@pytest.fixture
def cfg(make_config):
    return dataclasses.replace(make_config(), profile="personal")


def run(cfg, s, *argv):
    factory = lambda c: MetaClient(c, session=s, sleep=lambda x: None)  # noqa: E731
    return cli.main(list(argv), config=cfg, client_factory=factory, input_fn=lambda p: "testacct",
                    remuxer_factory=lambda c: Remuxer(c.tmp_dir, ffmpeg=None))


def test_flag_defaults_to_blocked():
    assert models.RESUMABLE_TRIAL_ALLOWED is False


# ------------------------------------------------------------------ every layer refuses TRIAL + resumable
@pytest.mark.parametrize("argv", [
    ("--account", "personal"),
    ("--account", "personal", "--dry-run"),
    ("--account", "personal", "--yes"),
    ("--account", "personal", "--diagnostic-trial-reattempt-override"),
])
def test_cli_refuses_resumable_trial_before_any_network(cfg, video, argv, capsys):
    s = happy_session()
    with pytest.raises(SystemExit) as e:
        run(cfg, s, *argv)
    assert e.value.code == 2 and s.calls == []
    assert "--upload-method url" in capsys.readouterr().err
    assert not cfg.state_path.exists() or store(cfg).all() == []


@pytest.mark.parametrize("argv", [("--account", "personal", "--status"),
                                  ("--account", "personal", "--public", "--dry-run")])
def test_cli_other_commands_unaffected(cfg, video, argv):
    assert run(cfg, happy_session(), *argv) in (0, 2)


def test_build_job_refuses(cfg, video):
    with pytest.raises(PublisherError, match="upload-method url"):
        build_job(cfg, video.resolve(), validate_video(video), TRIAL, RESUMABLE)
    assert build_job(cfg, video.resolve(), validate_video(video), PUBLIC, RESUMABLE).publish_mode == PUBLIC


def test_container_params_refuse_trial_public_unchanged():
    with pytest.raises(ValueError):
        reel_container_params("", TRIAL, "MANUAL")
    assert reel_container_params("", PUBLIC) == {"media_type": "REELS", "upload_type": "resumable"}
    assert url_reel_container_body("", TRIAL, "MANUAL", URL)["trial_params"] == {"graduation_strategy": "MANUAL"}


def test_publish_job_refuses_hand_built_resumable_trial(cfg, video):
    r = validate_video(video)
    job = VideoJob(path=video.resolve(), account_id=IG_ID, caption="", graduation_strategy="MANUAL",
                   sha256=r.sha256, size_bytes=r.size_bytes, publish_mode=TRIAL, upload_method=RESUMABLE)
    s = happy_session()
    with pytest.raises(PublisherError):
        publish_job(job, client=MetaClient(cfg, session=s, sleep=lambda x: None), store=store(cfg),
                    out=lambda m: None, relative_path="IMG_3813.MOV", remuxer=Remuxer(cfg.tmp_dir))
    assert s.calls == [] and store(cfg).all() == []


# ------------------------------------------------------------------ requested vs confirmed mode
def _job(mode=TRIAL, method=URL_METHOD):
    return VideoJob(path=__import__("pathlib").Path("/v/x.MOV"), account_id=IG_ID, caption="",
                    graduation_strategy="MANUAL" if mode == TRIAL else None, sha256="h", size_bytes=1,
                    publish_mode=mode, upload_method=method)


@pytest.mark.parametrize("media, verdict", [
    ({"is_shared_to_feed": True, "media_product_type": "REELS"}, "NOT A TRIAL"),
    ({"is_shared_to_feed": False, "media_product_type": "REELS"}, "NOT VERIFIED"),
    ({"media_product_type": "REELS"}, "NOT VERIFIED"),
    (None, "NOT VERIFIED"),
])
def test_trial_is_never_reported_verified(media, verdict):
    lines = mode_check(_job(), media)
    text = "\n".join(lines)
    assert f"confirmed mode {verdict}" in lines[0] and "requested TRIAL / MANUAL via url" in lines[0]
    assert f"Confirmed mode:  {verdict}" in text and "Requested mode:  TRIAL / MANUAL via url" in text
    assert "Trial Reel published" not in text and "Confirmed mode:  TRIAL" not in text
    assert ("WARNING" in text) == (verdict == "NOT A TRIAL")


def test_public_mode_check():
    lines = mode_check(_job(PUBLIC, RESUMABLE), {"is_shared_to_feed": True, "media_product_type": "REELS"})
    assert lines[0].startswith("mode check: requested PUBLIC") and "is_shared_to_feed=true" in lines[0]


def _url_session(media):
    s = happy_session(statuses=("IN_PROGRESS", "FINISHED"))
    s.routes.insert(0, ["GET", "/1800", [FakeResponse(200, {"id": "1800", "permalink": "https://x/reel/abc/",
                                                            **media})]])
    return s


def test_url_trial_shared_to_feed_is_flagged_and_recorded(cfg, video, capsys):
    s = _url_session({"media_product_type": "REELS", "is_shared_to_feed": True})
    assert run_url(cfg, s) == 0
    body = json.loads(writes(s)[0]["data"])
    assert body["trial_params"] == {"graduation_strategy": "MANUAL"} and "upload_type" not in body
    out = capsys.readouterr().out
    assert "Confirmed mode:  NOT A TRIAL" in out and "WARNING: this looks like a NORMAL Reel" in out
    rec = store(cfg).all()[0]
    assert rec.state == "PUBLISHED" and rec.publish_mode == "TRIAL"                     # requested mode kept
    details = [e["detail"] or "" for e in store(cfg).events(rec.job_key)]
    assert any("confirmed mode NOT A TRIAL" in d and "is_shared_to_feed=true" in d for d in details)


def test_url_trial_not_shared_is_still_unverified(cfg, video, capsys):
    s = _url_session({"media_product_type": "REELS", "is_shared_to_feed": False})
    assert run_url(cfg, s) == 0
    out = capsys.readouterr().out
    assert "Confirmed mode:  NOT VERIFIED" in out and "not proof" in out and "WARNING" not in out
    rec = store(cfg).all()[0]
    assert any("confirmed mode NOT VERIFIED" in (e["detail"] or "") for e in store(cfg).events(rec.job_key))


def test_get_media_requests_is_shared_to_feed(make_config):
    s = happy_session()
    MetaClient(make_config(), session=s, sleep=lambda x: None).get_media("1800")
    assert "is_shared_to_feed" in s.calls[-1]["params"]["fields"]


def test_status_marks_published_trial_unverified(cfg, video, capsys):
    run_url(cfg, _url_session({"is_shared_to_feed": False}))
    capsys.readouterr()
    assert run(cfg, happy_session(), "--account", "personal", "--status") == 0
    assert "trial status NOT verified by API" in capsys.readouterr().out
