"""--public (regular Reel) mode, and how it coexists with the default Trial mode."""
import json
import sqlite3

import pytest
import requests

from conftest import IG_ID, TOKEN, FakeResponse, FakeSession, fake_mp4, happy_session
from test_remux import FakeFFmpeg
from trial_reels import cli
from trial_reels.errors import MetaApiError, TrialRejectedError
from trial_reels.meta_client import MetaClient, reel_container_params
from trial_reels.models import PUBLIC, TRIAL, make_job_key
from trial_reels.remux import Remuxer
from trial_reels.state import StateStore

pytestmark = pytest.mark.usefixtures("legacy_resumable_trial")  # see conftest


@pytest.fixture
def video(videos_dir):
    p = videos_dir / "test_reel.mp4"
    p.write_bytes(fake_mp4())
    return p


class Answers:
    """input_fn stub: returns scripted answers and records each prompt."""

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        return self.answers.pop(0)


def run(cfg, session, *argv, answers=None, tty=True, monkeypatch=None, remuxer=None):
    if monkeypatch is not None:
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: tty, raising=False)
    factory = lambda c: MetaClient(c, session=session, sleep=lambda s: None)  # noqa: E731
    rf = (lambda c: remuxer) if remuxer else (lambda c: Remuxer(c.tmp_dir, ffmpeg=None))
    return cli.main(list(argv), config=cfg, client_factory=factory,
                    input_fn=answers or Answers(), remuxer_factory=rf)


def creates(session):
    return [c for c in session.calls if c["method"] == "POST" and c["url"].endswith(f"{IG_ID}/media")]


def writes(session):
    return [c for c in session.calls if c["method"] == "POST"]


# ------------------------------------------------------------------ request parameters
def test_public_params_omit_trial_params_entirely():
    p = reel_container_params("", PUBLIC)
    assert p == {"media_type": "REELS", "upload_type": "resumable"}
    assert "trial_params" not in p and "video_url" not in p
    assert reel_container_params("hi", PUBLIC)["caption"] == "hi"


def test_trial_params_unchanged():
    p = reel_container_params("", TRIAL, "MANUAL")
    assert json.loads(p["trial_params"]) == {"graduation_strategy": "MANUAL"}
    assert p["media_type"] == "REELS" and p["upload_type"] == "resumable"


@pytest.mark.parametrize("args", [(PUBLIC, "MANUAL"), (TRIAL, None), (TRIAL, "manual"), ("FEED", None)])
def test_invalid_mode_strategy_combinations(args):
    with pytest.raises(ValueError):
        reel_container_params("", *args)


def test_public_container_request(make_config):
    s = happy_session()
    MetaClient(make_config(), session=s).create_reel_container("", PUBLIC)
    data = creates(s)[0]["data"]
    assert data == {"media_type": "REELS", "upload_type": "resumable"}


def test_public_error_mentioning_trial_is_not_trial_rejection(make_config):
    s = FakeSession().on("POST", f"/{IG_ID}/media", FakeResponse(400, {"error": {
        "message": "trial something", "code": 100}}))
    with pytest.raises(MetaApiError) as ei:
        MetaClient(make_config(), session=s).create_reel_container("", PUBLIC)
    assert not isinstance(ei.value, TrialRejectedError)


def test_public_and_trial_job_keys_differ_and_trial_key_is_backward_compatible(tmp_path):
    import hashlib
    path = tmp_path / "a.mp4"
    old = hashlib.sha256(f"v1|{IG_ID}|{path}|abc".encode()).hexdigest()
    assert make_job_key(IG_ID, path, "abc") == old == make_job_key(IG_ID, path, "abc", TRIAL)
    assert make_job_key(IG_ID, path, "abc", PUBLIC) != old


# ------------------------------------------------------------------ default stays trial
def test_default_run_is_still_trial(make_config, video, monkeypatch):
    cfg, s = make_config(), happy_session()
    assert run(cfg, s, "--yes", monkeypatch=monkeypatch, tty=False) == 0
    assert "trial_params" in creates(s)[0]["data"]
    rec = StateStore(cfg.state_path).all()[0]
    assert rec.publish_mode == "TRIAL" and rec.graduation_strategy == "MANUAL"


def test_trial_rejection_never_falls_back_to_public(make_config, video, monkeypatch):
    cfg, s = make_config(), happy_session()
    s.routes.insert(0, ["POST", f"/{IG_ID}/media", [FakeResponse(400, {"error": {
        "message": "Invalid parameter trial_params", "code": 100}})]])
    assert run(cfg, s, "--yes", monkeypatch=monkeypatch, tty=False) == 2
    assert len(creates(s)) == 1 and "trial_params" in creates(s)[0]["data"]
    assert not any("trial_params" not in c["data"] for c in creates(s))
    assert len(writes(s)) == 1
    assert [r.publish_mode for r in StateStore(cfg.state_path).all()] == ["TRIAL"]


# ------------------------------------------------------------------ public publish
def test_public_publish_full_flow(make_config, video, monkeypatch, capsys):
    cfg, s = make_config(), happy_session()
    ans = Answers("post", "PUBLIC")
    assert run(cfg, s, "--public", answers=ans, monkeypatch=monkeypatch) == 0
    assert creates(s)[0]["data"] == {"media_type": "REELS", "upload_type": "resumable"}
    assert any(u["url"].endswith("media_publish") for u in writes(s))
    out = capsys.readouterr().out
    assert f"@testacct (IG ID {IG_ID})" in out                       # verified account shown
    assert "PUBLIC REEL: this is NOT a trial" in out and "FOLLOWERS" in out
    assert "regular PUBLIC Reel" in out and "FIRST-RUN CHECK" not in out
    assert "a PUBLIC Reel" in ans.prompts[0] and "Type PUBLIC" in ans.prompts[1]
    rec = StateStore(cfg.state_path).all()[0]
    assert rec.publish_mode == "PUBLIC" and rec.graduation_strategy is None and rec.state == "PUBLISHED"


def test_public_preflight_happens_before_prompts(make_config, video, monkeypatch):
    cfg, s = make_config(), happy_session()
    seen = []

    def answer(prompt):
        seen.append([c["url"].rsplit("/", 1)[-1] for c in s.calls])
        return "post" if len(seen) == 1 else "PUBLIC"
    run(cfg, s, "--public", answers=answer, monkeypatch=monkeypatch)
    assert IG_ID in seen[0] and "content_publishing_limit" in seen[0]
    assert not any(c["method"] == "POST" for c in s.calls[: len(seen[1])])


def test_yes_does_not_skip_public_confirmation(make_config, video, monkeypatch):
    cfg, s = make_config(), happy_session()
    ans = Answers("PUBLIC")
    assert run(cfg, s, "--public", "--yes", answers=ans, monkeypatch=monkeypatch) == 0
    assert len(ans.prompts) == 1 and "Type PUBLIC" in ans.prompts[0]


@pytest.mark.parametrize("answers", [("no",), ("post", "public"), ("post", "yes"), ("post", "")])
def test_public_confirmation_declined_posts_nothing(make_config, video, monkeypatch, answers, capsys):
    cfg, s = make_config(), happy_session()
    assert run(cfg, s, "--public", answers=Answers(*answers), monkeypatch=monkeypatch) == 1
    assert writes(s) == []
    assert "Nothing was posted" in capsys.readouterr().out
    assert not cfg.state_path.exists() or StateStore(cfg.state_path).all() == []


def test_public_without_terminal_refuses_before_network(make_config, video, monkeypatch, capsys):
    cfg, s = make_config(), happy_session()
    assert run(cfg, s, "--public", "--yes", monkeypatch=monkeypatch, tty=False) == 2
    assert s.calls == []
    assert "interactive terminal" in capsys.readouterr().err


def test_public_flag_rejected_with_other_commands(make_config):
    for extra in ("--check-auth", "--status", "--reconcile", "--mark-not-published"):
        with pytest.raises(SystemExit):
            cli.parse_args(["--public", extra])


def test_public_mov_uses_fast_start_copy(make_config, videos_dir, monkeypatch):
    mov = videos_dir / "IMG_1.MOV"
    mov.write_bytes(fake_mp4(moov_first=False))
    original = mov.read_bytes()
    remuxed = fake_mp4(payload=b"R" * 2048)
    cfg, s = make_config(), happy_session()
    rmx = Remuxer(cfg.tmp_dir, ffmpeg="ffmpeg", run=FakeFFmpeg(output=remuxed))
    assert run(cfg, s, "--public", answers=Answers("post", "PUBLIC"), monkeypatch=monkeypatch, remuxer=rmx) == 0
    up = next(c for c in s.calls if "/ig-api-upload/" in c["url"])
    assert up["body_bytes"] == remuxed and mov.read_bytes() == original
    rec = StateStore(cfg.state_path).all()[0]
    assert rec.publish_mode == "PUBLIC" and rec.upload_remuxed == 1
    assert list(cfg.tmp_dir.iterdir()) == []


# ------------------------------------------------------------------ cross-mode duplicate protection
def test_trial_published_then_public_refused(make_config, video, monkeypatch, capsys):
    cfg = make_config()
    assert run(cfg, happy_session(), "--yes", monkeypatch=monkeypatch, tty=False) == 0
    capsys.readouterr()
    s = happy_session()
    assert run(cfg, s, "--public", answers=Answers(), monkeypatch=monkeypatch) == 2
    assert s.calls == []  # refused locally: no preflight, no prompts, no writes
    err = capsys.readouterr().err
    assert "as a TRIAL Reel" in err and "1800" in err and "either mode" in err


def test_public_published_then_trial_refused(make_config, video, monkeypatch, capsys):
    cfg = make_config()
    assert run(cfg, happy_session(), "--public", answers=Answers("post", "PUBLIC"), monkeypatch=monkeypatch) == 0
    capsys.readouterr()
    s = happy_session()
    assert run(cfg, s, "--yes", monkeypatch=monkeypatch, tty=False) == 2
    assert s.calls == []
    assert "as a PUBLIC Reel" in capsys.readouterr().err


def test_public_rerun_same_mode_reports_already_published(make_config, video, monkeypatch, capsys):
    cfg = make_config()
    run(cfg, happy_session(), "--public", answers=Answers("post", "PUBLIC"), monkeypatch=monkeypatch)
    capsys.readouterr()
    s = happy_session()
    assert run(cfg, s, "--public", answers=Answers(), monkeypatch=monkeypatch) == 0
    assert writes(s) == []
    assert "Already published by this tool (PUBLIC)" in capsys.readouterr().out


def test_uncertain_trial_blocks_public(make_config, video, monkeypatch, capsys):
    cfg, s = make_config(), happy_session()
    s.routes.insert(0, ["POST", "/media_publish", [requests.ReadTimeout("t")]])
    assert run(cfg, s, "--yes", monkeypatch=monkeypatch, tty=False) == 3
    capsys.readouterr()
    s2 = happy_session()
    assert run(cfg, s2, "--public", answers=Answers(), monkeypatch=monkeypatch) == 2
    assert s2.calls == []
    assert "UNKNOWN" in capsys.readouterr().err


def test_failed_trial_does_not_block_public(make_config, video, monkeypatch):
    cfg = make_config()
    run(cfg, happy_session(statuses=("ERROR",)), "--yes", monkeypatch=monkeypatch, tty=False)
    s = happy_session()
    assert run(cfg, s, "--public", answers=Answers("post", "PUBLIC"), monkeypatch=monkeypatch) == 0
    modes = {r.publish_mode: r.state for r in StateStore(cfg.state_path).all()}
    assert modes == {"TRIAL": "FAILED", "PUBLIC": "PUBLISHED"}


def test_renamed_copy_of_published_video_refused(make_config, video, monkeypatch, capsys):
    cfg = make_config()
    run(cfg, happy_session(), "--yes", monkeypatch=monkeypatch, tty=False)
    video.rename(video.with_name("renamed.mp4"))
    s = happy_session()
    assert run(cfg, s, "--yes", monkeypatch=monkeypatch, tty=False) == 2
    assert writes(s) == []
    assert "test_reel.mp4" in capsys.readouterr().err


def test_publish_job_guard_also_blocks_cross_mode(make_config, video, tmp_path):
    """Defence in depth: publish_job itself refuses, even if the CLI check were bypassed."""
    from trial_reels.errors import PublisherError
    from trial_reels.publisher import build_job, publish_job
    from trial_reels.validation import validate_video
    cfg = make_config()
    store = StateStore(cfg.state_path)
    rep = validate_video(video, stable_checks=0)
    trial = build_job(cfg, video, rep, TRIAL)
    store.insert(job_key=trial.job_key, account_id=IG_ID, source_path=str(trial.path), relative_path="x",
                 content_hash=trial.sha256, size_bytes=1, graduation_strategy="MANUAL")
    store.update(trial.job_key, "PUBLISHED", media_id="1800")
    s = happy_session()
    with pytest.raises(PublisherError, match="either mode"):
        publish_job(build_job(cfg, video, rep, PUBLIC), client=MetaClient(cfg, session=s), store=store,
                    out=lambda m: None, relative_path="x", remuxer=Remuxer(cfg.tmp_dir, ffmpeg=None))
    assert s.calls == []


# ------------------------------------------------------------------ dry runs
def test_public_dry_run_shows_exact_params_and_posts_nothing(make_config, video, monkeypatch, capsys):
    cfg, s = make_config(), happy_session()
    assert run(cfg, s, "--public", "--dry-run", answers=Answers(), monkeypatch=monkeypatch) == 0
    assert writes(s) == []
    out = capsys.readouterr().out
    assert f"POST https://graph.facebook.com/v27.0/{IG_ID}/media" in out
    assert "media_type=REELS" in out and "upload_type=resumable" in out
    assert "trial_params: OMITTED" in out and "trial_params={" not in out
    assert "PUBLIC (regular Reel, no trial_params)" in out and "visible to your followers" in out
    assert "creation_id=<container id>" in out and "Ready:" in out
    assert TOKEN not in out


def test_trial_dry_run_shows_trial_params(make_config, video, capsys):
    cfg, s = make_config(), happy_session()
    assert run(cfg, s, "--dry-run") == 0
    out = capsys.readouterr().out
    assert 'trial_params={"graduation_strategy": "MANUAL"}' in out and "OMITTED" not in out


def test_public_dry_run_without_credentials(make_config, video, capsys):
    s = FakeSession()
    assert run(make_config(IG_USER_ID=None, IG_ACCESS_TOKEN=None), s, "--public", "--dry-run") == 0
    assert s.calls == []
    assert "/<IG_USER_ID>/media" in capsys.readouterr().out


def test_public_dry_run_flags_cross_mode_duplicate(make_config, video, monkeypatch, capsys):
    cfg = make_config()
    run(cfg, happy_session(), "--yes", monkeypatch=monkeypatch, tty=False)
    capsys.readouterr()
    s = happy_session()
    assert run(cfg, s, "--public", "--dry-run", monkeypatch=monkeypatch) == 2
    assert writes(s) == []
    assert "NOT READY" in capsys.readouterr().out


# ------------------------------------------------------------------ state
def test_status_and_reconcile_show_mode(make_config, video, monkeypatch, capsys):
    cfg, s = make_config(), happy_session()
    s.routes.insert(0, ["POST", "/media_publish", [requests.ReadTimeout("t")]])
    assert run(cfg, s, "--public", answers=Answers("post", "PUBLIC"), monkeypatch=monkeypatch) == 3
    capsys.readouterr()
    assert run(cfg, happy_session(statuses=("FINISHED",)), "--reconcile") == 0
    assert "mode=PUBLIC" in capsys.readouterr().out
    assert run(cfg, FakeSession(), "--status") == 0
    assert "PUBLIC UNKNOWN" in " ".join(capsys.readouterr().out.split())


def test_migration_marks_old_rows_trial_and_allows_public_row(tmp_path):
    db = tmp_path / "old.sqlite3"
    c = sqlite3.connect(db)
    c.executescript("""CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT NOT NULL UNIQUE,
        account_id TEXT NOT NULL, source_path TEXT NOT NULL, relative_path TEXT NOT NULL,
        content_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL, graduation_strategy TEXT NOT NULL,
        scheduled_for TEXT, state TEXT NOT NULL, container_id TEXT, upload_uri TEXT, media_id TEXT,
        permalink TEXT, last_error TEXT, publish_attempts INTEGER NOT NULL DEFAULT 0,
        publish_started_at TEXT, upload_sha256 TEXT, upload_size_bytes INTEGER,
        upload_remuxed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (account_id, content_hash, source_path));
        INSERT INTO jobs (job_key, account_id, source_path, relative_path, content_hash, size_bytes,
        graduation_strategy, state, media_id, created_at, updated_at)
        VALUES ('k','1','/a.mp4','a.mp4','h',1,'MANUAL','PUBLISHED','1800','t','t');""")
    c.commit()
    c.close()
    store = StateStore(db)
    rec = store.get("k")
    assert rec.publish_mode == "TRIAL" and rec.media_id == "1800" and rec.state == "PUBLISHED"
    store.insert(job_key="k2", account_id="1", source_path="/a.mp4", relative_path="a.mp4",
                 content_hash="h", size_bytes=1, graduation_strategy=None, publish_mode="PUBLIC")
    assert [r.publish_mode for r in store.find_blocking("1", "h")] == ["TRIAL"]
    StateStore(db).close()  # reopening a migrated DB is a no-op
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE jobs SET publish_mode='FEED' WHERE job_key='k2'")
