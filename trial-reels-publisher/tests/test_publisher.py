import logging

import pytest
import requests

from conftest import IG_ID, TOKEN, FakeResponse, FakeSession, fake_mp4, happy_session
from trial_reels import cli
from trial_reels.meta_client import MetaClient
from trial_reels.remux import Remuxer
from test_remux import FakeFFmpeg
from trial_reels.state import StateStore

pytestmark = pytest.mark.usefixtures("legacy_resumable_trial")  # see conftest


@pytest.fixture
def video(videos_dir):
    p = videos_dir / "test_reel.mp4"
    p.write_bytes(fake_mp4())
    return p


def run(cfg, session, *argv, inp="post", remuxer=None):
    factory = lambda c: MetaClient(c, session=session, sleep=lambda s: None)  # noqa: E731
    if not argv:
        argv = ("--yes",)
    rf = (lambda c: remuxer) if remuxer is not None else (lambda c: Remuxer(c.tmp_dir, ffmpeg=None))
    return cli.main(list(argv), config=cfg, client_factory=factory, input_fn=lambda _: inp,
                    remuxer_factory=rf)


def writes(session):
    return [c["url"] for c in session.calls if c["method"] == "POST"]


def test_dry_run_makes_no_writes(make_config, video, capsys):
    s = happy_session()
    assert run(make_config(), s, "--dry-run") == 0
    assert writes(s) == []
    out = capsys.readouterr().out
    assert "test_reel.mp4" in out and "TRIAL / MANUAL" in out and "@testacct" in out


def test_dry_run_without_credentials_is_local_only(make_config, video, capsys):
    s = FakeSession()
    assert run(make_config(IG_USER_ID=None, IG_ACCESS_TOKEN=None), s, "--dry-run") == 0
    assert s.calls == [] and "not configured" in capsys.readouterr().out


def test_check_auth_read_only(make_config, video, capsys):
    s = happy_session()
    assert run(make_config(), s, "--check-auth") == 0
    assert writes(s) == []
    out = capsys.readouterr().out
    assert f"@testacct (IG ID {IG_ID})" in out and "2 of 100 used" in out


def test_check_auth_missing_publish_permission(make_config, video, capsys):
    s = happy_session()
    s.routes.insert(0, ["GET", "/me/permissions", [FakeResponse(200, {"data": [
        {"permission": "instagram_basic", "status": "granted"}]})]])
    assert run(make_config(), s, "--check-auth") == 2
    assert "instagram_content_publish" in capsys.readouterr().err


def test_zero_or_two_videos_no_api_write(make_config, videos_dir, capsys):
    s = happy_session()
    assert run(make_config(), s) == 2
    (videos_dir / "a.mp4").write_bytes(fake_mp4())
    (videos_dir / "b.mp4").write_bytes(fake_mp4())
    assert run(make_config(), s) == 2
    assert writes(s) == []


def test_full_publish_then_rerun_is_noop(make_config, video, capsys):
    cfg = make_config()
    s = happy_session()
    assert run(cfg, s) == 0
    assert [u.rsplit("/", 1)[-1] for u in writes(s)] == ["media", "900", "media_publish"]
    out = capsys.readouterr().out
    assert "Published: IG media ID 1800" in out and "instagram.com/reel/abc" in out
    assert "trial" in out.lower()
    rec = StateStore(cfg.state_path).all()[0]
    assert rec.state == "PUBLISHED" and rec.media_id == "1800" and rec.container_id == "900"
    assert video.read_bytes() == fake_mp4()  # original untouched

    s2 = happy_session()
    assert run(cfg, s2) == 0
    assert writes(s2) == []
    assert "Already published by this tool (TRIAL): IG media ID 1800" in capsys.readouterr().out


def test_changed_contents_is_a_new_job(make_config, video):
    cfg = make_config()
    run(cfg, happy_session())
    video.write_bytes(fake_mp4(payload=b"z" * 4096))
    s = happy_session(media_id="1801")
    assert run(cfg, s) == 0
    assert any(u.endswith("media_publish") for u in writes(s))
    assert {r.media_id for r in StateStore(cfg.state_path).all()} == {"1800", "1801"}


@pytest.mark.parametrize("statuses", [("IN_PROGRESS", "ERROR"), ("EXPIRED",)])
def test_processing_failure_never_publishes(make_config, video, statuses):
    cfg = make_config()
    s = happy_session(statuses=statuses)
    assert run(cfg, s) == 2
    assert not any(u.endswith("media_publish") for u in writes(s))
    assert StateStore(cfg.state_path).all()[0].state == "FAILED"


def test_failed_job_retries_with_fresh_container(make_config, video):
    cfg = make_config()
    run(cfg, happy_session(statuses=("ERROR",)))
    s = happy_session()
    assert run(cfg, s) == 0
    assert sum(u.endswith(f"{IG_ID}/media") for u in writes(s)) == 1


def test_ambiguous_publish_then_rerun_makes_no_second_publish(make_config, video, capsys):
    cfg = make_config()
    s = happy_session()
    s.routes.insert(0, ["POST", "/media_publish", [requests.ReadTimeout("timed out")]])
    assert run(cfg, s) == 3
    assert StateStore(cfg.state_path).all()[0].state == "UNKNOWN"
    assert "UNCERTAIN" in capsys.readouterr().out

    s2 = happy_session()
    assert run(cfg, s2) == 3
    assert writes(s2) == []
    assert "--reconcile" in capsys.readouterr().out


def test_reconcile_flow(make_config, video, capsys):
    cfg = make_config()
    s = happy_session()
    s.routes.insert(0, ["POST", "/media_publish", [requests.ReadTimeout("timed out")]])
    run(cfg, s)

    # FINISHED proves nothing: reconcile reports it, mark-not-published is refused, state stays UNKNOWN
    s2 = happy_session(statuses=("FINISHED",))
    assert run(cfg, s2, "--reconcile") == 0 and writes(s2) == []
    out = capsys.readouterr().out
    assert "Verdict: UNRESOLVED" in out and "proves neither" in out
    assert run(cfg, s2, "--mark-not-published") == 2
    assert StateStore(cfg.state_path).all()[0].state == "UNKNOWN"

    # EXPIRED is Meta's documented proof it was never published -> allowed; history kept
    s3 = happy_session(statuses=("EXPIRED",))
    assert run(cfg, s3, "--mark-not-published") == 0 and writes(s3) == []
    rec = StateStore(cfg.state_path).all()[0]
    assert rec.state == "FAILED" and rec.failure_phase == "RECONCILED_NOT_PUBLISHED"

    # next attempt; if Meta says PUBLISHED, mark-not-published is refused, mark-published allowed
    s4 = happy_session()
    s4.routes.insert(0, ["POST", "/media_publish", [requests.ReadTimeout("timed out")]])
    assert run(cfg, s4) == 3
    s5 = happy_session(statuses=("PUBLISHED",))
    assert run(cfg, s5, "--mark-not-published") == 2
    assert run(cfg, s5, "--mark-published", "1800") == 0
    assert StateStore(cfg.state_path).all()[0].state == "PUBLISHED"
    assert writes(s5) == []


def test_trial_rejected_no_fallback(make_config, video, capsys):
    cfg = make_config()
    s = happy_session()
    s.routes.insert(0, ["POST", f"/{IG_ID}/media", [FakeResponse(400, {"error": {
        "message": "The parameter trial_params is not supported with upload_type", "code": 100}})]])
    assert run(cfg, s) == 2
    creates = [c for c in s.calls if c["method"] == "POST"]
    assert len(creates) == 1 and "trial_params" in creates[0]["data"]
    assert "NOT retry as an ordinary Reel" in capsys.readouterr().err
    assert StateStore(cfg.state_path).all()[0].state == "FAILED"


def test_no_quota_left_stops_before_write(make_config, video):
    s = happy_session()
    s.routes.insert(0, ["GET", "/content_publishing_limit", [FakeResponse(200, {"data": [
        {"quota_usage": 100, "config": {"quota_total": 100}}]})]])
    assert run(make_config(), s) == 2
    assert writes(s) == []


def test_expired_token_stops_before_write(make_config, video, capsys):
    s = happy_session()
    s.routes.insert(0, ["GET", f"/{IG_ID}", [FakeResponse(400, {"error": {
        "message": "Session has expired", "code": 190}})]])
    assert run(make_config(), s) == 2
    assert writes(s) == [] and "expired" in capsys.readouterr().err


def test_confirmation_prompt_declined(make_config, video, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    s = happy_session()
    factory = lambda c: MetaClient(c, session=s, sleep=lambda x: None)  # noqa: E731
    assert cli.main([], config=make_config(), client_factory=factory, input_fn=lambda _: "no",
                    remuxer_factory=lambda c: Remuxer(c.tmp_dir, ffmpeg=None)) == 1
    assert writes(s) == []


def test_token_never_printed_or_logged(make_config, video, capsys):
    cfg = make_config()
    s = happy_session()
    s.routes.insert(0, ["POST", f"/{IG_ID}/media", [FakeResponse(400, {"error": {
        "message": f"Bad request for access_token={TOKEN} and Bearer {TOKEN}", "code": 100}})]])
    run(cfg, s)
    logging.shutdown()
    captured = capsys.readouterr()
    log_text = "".join(p.read_text() for p in cfg.log_dir.glob("*.log"))
    db_text = cfg.state_path.read_bytes().decode("latin-1")
    for text in (captured.out, captured.err, log_text, db_text):
        assert TOKEN not in text
    assert "[REDACTED]" in captured.err


def test_state_and_logs_outside_video_folder(make_config, videos_dir, tmp_path):
    from trial_reels.errors import ConfigError
    with pytest.raises(ConfigError, match="inside the monitored video folder"):
        make_config(TRIAL_REELS_DATA_DIR=str(videos_dir / "data"))


def test_lowercase_strategy_rejected(make_config):
    from trial_reels.errors import ConfigError
    with pytest.raises(ConfigError, match="uppercase"):
        make_config(TRIAL_GRADUATION_STRATEGY="manual")



# ---------------------------------------------------------------- .mov / fast-start remux
REMUXED = fake_mp4(payload=b"R" * 2048)


@pytest.fixture
def late_moov_mov(videos_dir):
    p = videos_dir / "IMG_3813.MOV"
    p.write_bytes(fake_mp4(moov_first=False))
    return p


def upload_call(session):
    return next(c for c in session.calls if "/ig-api-upload/" in c["url"])


def test_faststart_mov_is_uploaded_as_original_when_no_remux_needed(make_config, videos_dir):
    p = videos_dir / "clip.MOV"
    p.write_bytes(fake_mp4())
    cfg, s = make_config(), happy_session()
    assert run(cfg, s) == 0
    assert upload_call(s)["body_bytes"] == p.read_bytes()
    rec = StateStore(cfg.state_path).all()[0]
    assert rec.upload_remuxed == 0 and rec.upload_sha256 == rec.content_hash


def test_late_moov_mov_publishes_temp_copy_and_keeps_original_identity(make_config, late_moov_mov):
    import hashlib
    original = late_moov_mov.read_bytes()
    mtime = late_moov_mov.stat().st_mtime_ns
    cfg, s, fake = make_config(), happy_session(), FakeFFmpeg(output=REMUXED)
    assert run(cfg, s, remuxer=Remuxer(cfg.tmp_dir, ffmpeg="ffmpeg", run=fake)) == 0

    up = upload_call(s)
    assert up["body_bytes"] == REMUXED and up["headers"]["file_size"] == str(len(REMUXED))
    rec = StateStore(cfg.state_path).all()[0]
    assert rec.state == "PUBLISHED" and rec.source_path == str(late_moov_mov.resolve())
    assert rec.content_hash == hashlib.sha256(original).hexdigest()       # identity = original
    assert rec.upload_sha256 == hashlib.sha256(REMUXED).hexdigest() and rec.upload_remuxed == 1
    assert late_moov_mov.read_bytes() == original and late_moov_mov.stat().st_mtime_ns == mtime
    assert not cfg.tmp_dir.exists() or list(cfg.tmp_dir.iterdir()) == []  # temp copy cleaned up
    assert sorted(x.name for x in late_moov_mov.parent.iterdir()) == ["IMG_3813.MOV"]

    # rerun: same original -> already published, no ffmpeg, no API writes
    s2, fake2 = happy_session(), FakeFFmpeg(output=REMUXED)
    assert run(cfg, s2, remuxer=Remuxer(cfg.tmp_dir, ffmpeg="ffmpeg", run=fake2)) == 0
    assert writes(s2) == [] and fake2.calls == []


def test_late_moov_without_ffmpeg_exits_before_any_network(make_config, late_moov_mov, capsys):
    cfg, s = make_config(), happy_session()
    before = late_moov_mov.read_bytes()
    assert run(cfg, s, remuxer=Remuxer(cfg.tmp_dir, ffmpeg=None)) == 2
    assert s.calls == []  # not even the read-only preflight
    err = capsys.readouterr().err
    assert "ffmpeg" in err and "Nothing was posted" in err
    assert late_moov_mov.read_bytes() == before
    assert StateStore(cfg.state_path).all() == []


def test_dry_run_reports_remux_need(make_config, late_moov_mov, capsys):
    cfg = make_config()
    s = happy_session()
    assert run(cfg, s, "--dry-run", remuxer=Remuxer(cfg.tmp_dir, ffmpeg=None)) == 2
    out = capsys.readouterr().out
    assert "ffmpeg is not installed" in out and "NOT READY" in out
    fake = FakeFFmpeg()
    assert run(cfg, s, "--dry-run", remuxer=Remuxer(cfg.tmp_dir, ffmpeg="ffmpeg", run=fake)) == 0
    assert "temporary fast-start copy will be made" in capsys.readouterr().out
    assert fake.calls == [] and writes(s) == []  # dry run never remuxes or writes


def test_ffmpeg_failure_marks_failed_without_api_write(make_config, late_moov_mov):
    cfg, s = make_config(), happy_session()
    assert run(cfg, s, remuxer=Remuxer(cfg.tmp_dir, ffmpeg="ffmpeg", run=FakeFFmpeg(returncode=1))) == 2
    assert writes(s) == []
    assert StateStore(cfg.state_path).all()[0].state == "FAILED"


def test_upload_failure_cleans_temp_copy(make_config, late_moov_mov):
    cfg, s = make_config(), happy_session()
    s.routes.insert(0, ["POST", "/ig-api-upload/v27.0/900", [FakeResponse(400, {"debug_info": {
        "retriable": False, "type": "ProcessingFailedError", "message": "bad"}})]])
    assert run(cfg, s, remuxer=Remuxer(cfg.tmp_dir, ffmpeg="ffmpeg", run=FakeFFmpeg(output=REMUXED))) == 2
    assert list(cfg.tmp_dir.iterdir()) == []
    assert StateStore(cfg.state_path).all()[0].state == "FAILED"
    assert not any(u.endswith("media_publish") for u in writes(s))


def test_ambiguous_publish_after_remux_keeps_state_and_blocks_rerun(make_config, late_moov_mov):
    cfg, s = make_config(), happy_session()
    s.routes.insert(0, ["POST", "/media_publish", [requests.ReadTimeout("timed out")]])
    assert run(cfg, s, remuxer=Remuxer(cfg.tmp_dir, ffmpeg="ffmpeg", run=FakeFFmpeg(output=REMUXED))) == 3
    rec = StateStore(cfg.state_path).all()[0]
    assert rec.state == "UNKNOWN" and rec.container_id == "900" and rec.upload_remuxed == 1
    assert rec.source_path == str(late_moov_mov.resolve()) and rec.upload_sha256
    assert list(cfg.tmp_dir.iterdir()) == []  # bytes already at Meta; copy not needed for reconcile

    s2, fake2 = happy_session(), FakeFFmpeg(output=REMUXED)
    assert run(cfg, s2, remuxer=Remuxer(cfg.tmp_dir, ffmpeg="ffmpeg", run=fake2)) == 3
    assert writes(s2) == [] and fake2.calls == []


def test_stale_temp_dirs_removed_at_start(make_config, video):
    cfg = make_config()
    stale = cfg.tmp_dir / "deadbeef"
    stale.mkdir(parents=True)
    (stale / "upload.mp4").write_bytes(b"old")
    assert run(cfg, happy_session()) == 0
    assert not stale.exists()


def test_state_db_migrates_old_schema(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite3"
    c = sqlite3.connect(db)
    c.executescript("""CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT NOT NULL UNIQUE,
        account_id TEXT NOT NULL, source_path TEXT NOT NULL, relative_path TEXT NOT NULL,
        content_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL, graduation_strategy TEXT NOT NULL,
        scheduled_for TEXT, state TEXT NOT NULL, container_id TEXT, upload_uri TEXT, media_id TEXT,
        permalink TEXT, last_error TEXT, publish_attempts INTEGER NOT NULL DEFAULT 0,
        publish_started_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO jobs (job_key, account_id, source_path, relative_path, content_hash, size_bytes,
        graduation_strategy, state, media_id, created_at, updated_at)
        VALUES ('k','1','/a.mp4','a.mp4','h',1,'MANUAL','PUBLISHED','1800','t','t');""")
    c.commit()
    c.close()
    rec = StateStore(db).get("k")
    assert rec.state == "PUBLISHED" and rec.media_id == "1800" and rec.upload_remuxed == 0
