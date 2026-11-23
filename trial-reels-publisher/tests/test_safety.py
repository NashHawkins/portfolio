"""Publish-outcome classification, UNKNOWN handling, account blocks, v4 migration, reconciliation."""
import hashlib
import sqlite3

import pytest
import requests

from conftest import IG_ID, TOKEN, FakeResponse, FakeSession, fake_mp4, happy_session
from trial_reels import cli
from trial_reels.errors import AmbiguousPublishError, MetaApiError, PublishRejectedError, StateError
from trial_reels.meta_client import MetaClient
from trial_reels.outcomes import NOT_PUBLISHED, UNCERTAIN, classify_publish_error
from trial_reels.remux import Remuxer
from trial_reels.state import SCHEMA_VERSION, StateStore, is_uncertain

pytestmark = pytest.mark.usefixtures("legacy_resumable_trial")  # see conftest

OTHER_ID = "17841400000000002"
REAL_2207085 = {"error": {"message": "An internal server error occurred. Please try again later.",
                          "type": "OAuthException", "code": -1, "error_subcode": 2207085,
                          "is_transient": False, "fbtrace_id": "APj3idYk1jXSXU4WC5dOAC-"}}


@pytest.fixture
def video(videos_dir):
    p = videos_dir / "IMG_3813.mp4"
    p.write_bytes(fake_mp4())
    return p


def run(cfg, session, *argv):
    factory = lambda c: MetaClient(c, session=session, sleep=lambda s: None)  # noqa: E731
    return cli.main(list(argv or ("--yes",)), config=cfg, client_factory=factory,
                    remuxer_factory=lambda c: Remuxer(c.tmp_dir, ffmpeg=None))


def writes(session):
    return [c for c in session.calls if c["method"] == "POST"]


def failing_publish(*responses):
    s = happy_session()
    s.routes.insert(0, ["POST", "/media_publish", list(responses)])
    return s


def err(status, code=None, subcode=None, transient=None, body=None):
    return MetaApiError("x", http_status=status, code=code, subcode=subcode, is_transient=transient)


# ------------------------------------------------------------------ classifier
@pytest.mark.parametrize("e,why", [
    (err(400, -1, 2207085), "server-fault"),          # the real failure
    (err(400, -1, 2207001), "server-fault"),
    (err(400, -2, 2207003), "server-fault"),
    (err(400, 1, None), "server-fault"),
    (err(400, 2, None), "server-fault"),
    (err(500, 2, None), "HTTP 500"),
    (err(503, None, None), "HTTP 503"),
    (err(400, 100, 2207089), "not a documented"),     # FINISHED-but-unpublishable reports
    (err(400, 24, 2207008), "not a documented"),      # container missing/expired: resolve via status
    (err(403, 4, 2207051), "not a documented"),       # reported to accompany posts that went live
    (err(400, 10, 2207081), "not a documented"),      # eligibility subcode at publish time
    (err(400, 9007, 2207027, transient=True), "transient"),
    (err(400, None, None), "without a Meta error code"),
    (err(None, None, None), "no HTTP response"),
    (err(200, None, None), "unexpected HTTP 200"),
])
def test_uncertain_classifications(e, why):
    c = classify_publish_error(e)
    assert c.outcome == UNCERTAIN and why in c.reason


@pytest.mark.parametrize("e", [err(400, 9007, 2207027), err(400, 9, 2207042), err(400, 190, None),
                               err(400, 190, 463), err(403, 10, None), err(403, 200, None),
                               err(400, 4, None), err(400, 17, None), err(400, 613, None)])
def test_definitive_rejections(e):
    assert classify_publish_error(e).outcome == NOT_PUBLISHED


def test_never_sent_is_not_published():
    assert classify_publish_error(None, request_may_have_reached_meta=False).outcome == NOT_PUBLISHED
    assert classify_publish_error(None).outcome == UNCERTAIN


# ------------------------------------------------------------------ transport
def test_real_2207085_response_is_ambiguous_with_full_details(make_config):
    s = FakeSession().on("POST", "/media_publish", FakeResponse(400, REAL_2207085))
    with pytest.raises(AmbiguousPublishError) as ei:
        MetaClient(make_config(), session=s).publish_container("900")
    m = ei.value.meta
    assert (m.http_status, m.code, m.subcode, m.error_type, m.is_transient, m.fbtrace_id) == \
        (400, -1, 2207085, "OAuthException", False, "APj3idYk1jXSXU4WC5dOAC-")
    assert "server-fault" in ei.value.reason and len(s.calls) == 1  # never retried


@pytest.mark.parametrize("resp", [
    FakeResponse(400, {"error": "just a string"}),                                  # malformed error
    FakeResponse(400, {"error": {"code": "-1", "error_subcode": "2207085", "message": "m"}}),  # strings
    FakeResponse(400, {"error": {"code": True, "message": "bool code"}}),
    FakeResponse(400, {"unexpected": "shape"}),
    FakeResponse(400, None, text="<html>bad gateway</html>"),
    FakeResponse(502, None, text="<html>bad gateway</html>"),
    FakeResponse(200, {"success": True}),                                            # no id
    FakeResponse(200, None, text="not json"),
])
def test_unexpected_formats_are_ambiguous(make_config, resp):
    s = FakeSession().on("POST", "/media_publish", resp)
    with pytest.raises(AmbiguousPublishError):
        MetaClient(make_config(), session=s).publish_container("900")


def test_string_codes_are_normalized(make_config):
    s = FakeSession().on("POST", "/media_publish", FakeResponse(400, {"error": {
        "code": "9007", "error_subcode": "2207027", "message": "not ready", "is_transient": "false"}}))
    with pytest.raises(PublishRejectedError) as ei:
        MetaClient(make_config(), session=s).publish_container("900")
    assert ei.value.code == 9007 and ei.value.is_transient is False


@pytest.mark.parametrize("exc", [requests.ReadTimeout("t"), requests.ConnectionError("reset"),
                                 requests.exceptions.ChunkedEncodingError("cut")])
def test_interruptions_after_send_are_ambiguous(make_config, exc):
    s = FakeSession().on("POST", "/media_publish", exc)
    with pytest.raises(AmbiguousPublishError):
        MetaClient(make_config(), session=s).publish_container("900")


def test_connect_timeout_is_definitive(make_config):
    s = FakeSession().on("POST", "/media_publish", requests.ConnectTimeout("no route"))
    with pytest.raises(PublishRejectedError):
        MetaClient(make_config(), session=s).publish_container("900")


# ------------------------------------------------------------------ end to end
def test_2207085_end_to_end_is_unknown_with_structured_log(make_config, video, capsys):
    cfg = make_config()
    s = failing_publish(FakeResponse(400, REAL_2207085))
    assert run(cfg, s) == 3
    store = StateStore(cfg.state_path)
    rec = store.all()[0]
    assert rec.state == "UNKNOWN" and rec.failure_phase is None and rec.publish_attempts == 1
    assert sum(c["url"].endswith("media_publish") for c in writes(s)) == 1
    call = [c for c in store.api_calls(rec.job_key) if c["step"] == "publish"][0]
    assert call["outcome"] == "UNCERTAIN" and call["phase"] == "PUBLISH"
    assert (call["http_status"], call["error_code"], call["error_subcode"], call["error_type"]) == \
        (400, -1, 2207085, "OAuthException")
    assert call["is_transient"] == 0 and call["fbtrace_id"] == "APj3idYk1jXSXU4WC5dOAC-"
    assert call["container_id"] == "900" and call["account_id"] == IG_ID
    assert call["content_hash"] == hashlib.sha256(video.read_bytes()).hexdigest()
    assert call["container_status_before"] == "FINISHED"
    assert (call["quota_usage_before"], call["quota_total_before"]) == (2, 100)
    assert call["attempted_at"] == rec.publish_started_at
    assert "internal server error" in call["error_message"]
    out = capsys.readouterr().out
    assert "UNCERTAIN" in out and "paused" in out


def test_rerun_after_unknown_makes_no_request_of_any_kind_to_write(make_config, video):
    cfg = make_config()
    run(cfg, failing_publish(FakeResponse(400, REAL_2207085)))
    for argv in [(), ("--public",)]:
        s = happy_session()
        rc = run(cfg, s, *argv) if argv else run(cfg, s)
        assert writes(s) == [] and rc in (2, 3)


def test_account_block_stops_other_videos_on_same_account(make_config, video, videos_dir, capsys):
    cfg = make_config()
    run(cfg, failing_publish(FakeResponse(400, REAL_2207085)))
    video.unlink()
    (videos_dir / "other.mp4").write_bytes(fake_mp4(payload=b"different" * 300))
    capsys.readouterr()
    s = happy_session()
    assert run(cfg, s) == 2
    assert s.calls == []  # refused locally, before any network call
    err_ = capsys.readouterr().err
    assert "PAUSED" in err_ and "job #1" in err_ and "container=900" in err_ and "state=UNKNOWN" in err_


def test_account_block_allows_read_only_commands(make_config, video, capsys):
    cfg = make_config()
    run(cfg, failing_publish(FakeResponse(400, REAL_2207085)))
    s = happy_session(statuses=("FINISHED",))
    assert run(cfg, s, "--check-auth") == 0
    assert run(cfg, s, "--reconcile") == 0
    assert run(cfg, s, "--dry-run") == 2
    assert run(cfg, s, "--status") == 0
    assert writes(s) == []
    out = capsys.readouterr().out
    assert "PAUSED" in out and "UNRESOLVED: account paused" in out


def test_account_block_does_not_affect_other_accounts(make_config, video):
    cfg = make_config()
    run(cfg, failing_publish(FakeResponse(400, REAL_2207085)))
    other = make_config(IG_USER_ID=OTHER_ID)
    s = happy_session(ig_id=OTHER_ID)
    assert run(other, s) == 0
    assert any(c["url"].endswith("media_publish") for c in writes(s))


def test_account_block_survives_restart(make_config, video):
    cfg = make_config()
    run(cfg, failing_publish(FakeResponse(400, REAL_2207085)))
    store = StateStore(cfg.state_path)
    store.close()
    assert [b.id for b in StateStore(cfg.state_path).account_blockers(IG_ID)] == [1]


def test_begin_publish_atomic_account_block(tmp_path):
    st = StateStore(tmp_path / "s.db")
    for key, h in (("a", "h1"), ("b", "h2")):
        st.insert(job_key=key, account_id=IG_ID, source_path=f"/v/{key}.mp4", relative_path=key,
                  content_hash=h, size_bytes=1, graduation_strategy="MANUAL")
    st.update("a", "UNKNOWN")
    st.update("b", "READY")
    with pytest.raises(StateError, match="unresolved"):
        st.begin_publish("b")


def test_pre_publish_failure_is_definite_and_does_not_block(make_config, video):
    cfg = make_config()
    s = happy_session()
    s.routes.insert(0, ["POST", "/ig-api-upload/v27.0/900", [FakeResponse(400, {"debug_info": {
        "retriable": False, "type": "ProcessingFailedError", "message": "bad"}})]])
    assert run(cfg, s) == 2
    store = StateStore(cfg.state_path)
    rec = store.all()[0]
    assert rec.state == "FAILED" and rec.failure_phase == "PRE_PUBLISH" and rec.publish_attempts == 0
    assert not is_uncertain(rec) and store.account_blockers(IG_ID) == []
    assert [c["step"] for c in store.api_calls(rec.job_key)] == ["upload"]
    assert run(cfg, happy_session()) == 0  # fresh container allowed


def test_documented_publish_rejection_is_failed_not_blocking(make_config, video):
    cfg = make_config()
    s = failing_publish(FakeResponse(400, {"error": {"message": "The media is not ready for publishing",
                                                     "code": 9007, "error_subcode": 2207027}}))
    assert run(cfg, s) == 2
    store = StateStore(cfg.state_path)
    rec = store.all()[0]
    assert rec.state == "FAILED" and rec.failure_phase == "PUBLISH_REJECTED" and rec.publish_attempts == 1
    assert not is_uncertain(rec)
    assert store.api_calls(rec.job_key)[-1]["outcome"] == "REJECTED"
    s2 = happy_session()
    assert run(cfg, s2) == 0 and any(c["url"].endswith(f"{IG_ID}/media") for c in writes(s2))


def test_unexpected_exception_during_publish_is_unknown(make_config, video):
    cfg = make_config()
    with pytest.raises(RuntimeError):  # unexpected errors propagate, but only after state is UNKNOWN
        run(cfg, failing_publish(RuntimeError("boom")))
    rec = StateStore(cfg.state_path).all()[0]
    assert rec.state == "UNKNOWN"


def test_normal_success_logs_ok(make_config, video):
    cfg = make_config()
    assert run(cfg, happy_session()) == 0
    store = StateStore(cfg.state_path)
    rec = store.all()[0]
    call = store.api_calls(rec.job_key)[-1]
    assert rec.state == "PUBLISHED" and call["outcome"] == "OK" and call["media_id"] == "1800"
    assert store.account_blockers(IG_ID) == []


def test_unknown_blocks_same_video_in_other_mode(make_config, video, monkeypatch):
    cfg = make_config()
    run(cfg, failing_publish(FakeResponse(400, REAL_2207085)))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    s = happy_session()
    factory = lambda c: MetaClient(c, session=s, sleep=lambda x: None)  # noqa: E731
    rc = cli.main(["--public"], config=cfg, client_factory=factory, input_fn=lambda p: "PUBLIC",
                  remuxer_factory=lambda c: Remuxer(c.tmp_dir, ffmpeg=None))
    assert rc == 2 and s.calls == []


def test_api_calls_never_store_tokens(make_config, video):
    cfg = make_config()
    body = {"error": {"message": f"oops {TOKEN} access_token={TOKEN}", "code": -1, "error_subcode": 2207085}}
    run(cfg, failing_publish(FakeResponse(400, body)))
    assert TOKEN not in cfg.state_path.read_bytes().decode("latin-1")
    dump = "".join(str(r) for r in sqlite3.connect(cfg.state_path).execute("SELECT * FROM api_calls"))
    assert TOKEN not in dump and "[REDACTED]" in dump


# ------------------------------------------------------------------ v4 migration (real job #3 shape)
V3 = """CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT NOT NULL UNIQUE,
 account_id TEXT NOT NULL, source_path TEXT NOT NULL, relative_path TEXT NOT NULL, content_hash TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, publish_mode TEXT NOT NULL DEFAULT 'TRIAL' CHECK (publish_mode IN ('TRIAL','PUBLIC')),
 graduation_strategy TEXT, scheduled_for TEXT, state TEXT NOT NULL, container_id TEXT, upload_uri TEXT,
 media_id TEXT, permalink TEXT, last_error TEXT, publish_attempts INTEGER NOT NULL DEFAULT 0,
 publish_started_at TEXT, upload_sha256 TEXT, upload_size_bytes INTEGER, upload_remuxed INTEGER NOT NULL DEFAULT 0,
 account_profile TEXT, account_username TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE (account_id, content_hash, source_path, publish_mode));
CREATE INDEX jobs_state ON jobs(state);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT NOT NULL, at TEXT NOT NULL,
 state TEXT NOT NULL, detail TEXT);
PRAGMA user_version = 3;"""
H = "b016ac4cda1eddfa" + "0" * 48
PERSONAL = "17840000000000002"
TESTACCT = "17840000000000001"


def make_v3_db(path):
    c = sqlite3.connect(path)
    c.executescript(V3)
    rows = [
        (1, "k1", TESTACCT, "TRIAL", "MANUAL", "FAILED", None, None, 0, None,
         "create trial reel container: ... follower requirement ... code=10, error_subcode=2207081", None,
         "2026-11-11T00:35:44+00:00", "2026-11-11T00:35:45+00:00"),
        (2, "k2", TESTACCT, "PUBLIC", None, "PUBLISHED", "18337424200279724", "18108325910203954", 1,
         "2026-11-11T00:46:31+00:00", None, None, "2026-11-11T00:46:05+00:00", "2026-11-11T00:46:41+00:00"),
        (3, "k3", PERSONAL, "TRIAL", "MANUAL", "FAILED", "18657792691044336", None, 1,
         "2026-11-11T01:16:16+00:00",
         "publish: An internal server error occurred. Please try again later. (HTTP 400, type=OAuthException, "
         "code=-1, error_subcode=2207085, fbtrace_id=APj3idYk1jXSXU4WC5dOAC-)", "personal",
         "2026-11-11T01:15:49+00:00", "2026-11-11T01:16:18+00:00"),
    ]
    for (i, k, acct, mode, strat, state, cont, media, att, started, last, prof, cr, up) in rows:
        c.execute("INSERT INTO jobs (id, job_key, account_id, source_path, relative_path, content_hash, size_bytes, "
                  "publish_mode, graduation_strategy, state, container_id, media_id, publish_attempts, "
                  "publish_started_at, last_error, account_profile, created_at, updated_at, upload_remuxed) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                  (i, k, acct, "/v/IMG_3813.MOV", "IMG_3813.MOV", H, 19322843, mode, strat, state, cont, media,
                   att, started, last, prof, cr, up))
    for state in ("VALIDATED", "CONTAINER_CREATED", "UPLOADED", "READY", "PUBLISHING", "FAILED"):
        c.execute("INSERT INTO events (job_key, at, state, detail) VALUES ('k3', 't', ?, NULL)", (state,))
    c.execute("INSERT INTO events (job_key, at, state, detail) VALUES ('k1', 't', 'FAILED', 'TrialRejectedError')")
    c.commit()
    c.close()


def snapshot(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    jobs = {r["id"]: dict(r) for r in c.execute("SELECT * FROM jobs")}
    events = [tuple(r) for r in c.execute("SELECT id, job_key, at, state, detail FROM events ORDER BY id")]
    c.close()
    return jobs, events


def test_v4_migration_repairs_job3_only_and_preserves_history(tmp_path):
    db = tmp_path / "state.sqlite3"
    make_v3_db(db)
    jobs_before, events_before = snapshot(db)
    st = StateStore(db)
    assert st.backup_path and snapshot(st.backup_path) == (jobs_before, events_before)
    assert st.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 8
    jobs, events = snapshot(db)

    j3 = jobs[3]
    assert j3["state"] == "UNKNOWN" and j3["failure_phase"] is None
    for col in ("id", "job_key", "account_id", "container_id", "content_hash", "created_at", "updated_at",
                "publish_started_at", "publish_attempts", "last_error", "publish_mode", "graduation_strategy"):
        assert j3[col] == jobs_before[3][col], col
    j1 = jobs[1]
    assert j1["state"] == "FAILED" and j1["failure_phase"] == "PRE_PUBLISH"
    assert {k: v for k, v in jobs[2].items()
            if k not in ("failure_phase", "upload_method", "video_url", "attempt")} == jobs_before[2]
    assert all(j["upload_method"] == "resumable" and j["video_url"] is None and j["attempt"] == 1
               for j in jobs.values())

    assert events[: len(events_before)] == events_before            # full history kept, in order
    added = events[len(events_before):]
    assert len(added) == 2
    k3_event = [e for e in added if e[1] == "k3"][0]
    assert k3_event[3] == "UNKNOWN" and "FAILED -> UNKNOWN" in k3_event[4]
    assert "18657792691044336" in k3_event[4] and "2207085" in k3_event[4]
    assert any("job #3: FAILED -> UNKNOWN" in line for line in st.migration_report)
    assert [b.id for b in st.account_blockers(PERSONAL)] == [3]
    assert st.account_blockers(TESTACCT) == []


def test_v4_migration_is_idempotent(tmp_path):
    db = tmp_path / "state.sqlite3"
    make_v3_db(db)
    StateStore(db).close()
    after_first = snapshot(db)
    st = StateStore(db)                          # reopen: nothing else changes, no new backup
    assert st.backup_path is None and st.migration_report == []
    assert st.repair_legacy_failed() == []       # explicit re-run is a no-op too
    assert snapshot(db) == after_first
    assert len(list(tmp_path.glob("state.sqlite3.bak-*"))) == 1


def test_v4_migration_rolls_back_on_failure(tmp_path, monkeypatch):
    db = tmp_path / "state.sqlite3"
    make_v3_db(db)
    before = snapshot(db)
    calls = {"n": 0}
    real_event = StateStore._event

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return real_event(self, *a, **k)
    monkeypatch.setattr(StateStore, "_event", flaky)
    with pytest.raises(RuntimeError):
        StateStore(db)
    jobs, events = snapshot(db)
    assert {i: j["state"] for i, j in jobs.items()} == {i: j["state"] for i, j in before[0].items()}
    assert events == before[1]
    monkeypatch.setattr(StateStore, "_event", real_event)
    st = StateStore(db)                           # a later clean open completes the migration
    assert st.get("k3").state == "UNKNOWN"


def test_unmigrated_failed_after_attempt_still_counts_as_uncertain(tmp_path):
    st = StateStore(tmp_path / "s.db")
    st.insert(job_key="x", account_id=IG_ID, source_path="/a", relative_path="a", content_hash="h",
              size_bytes=1, graduation_strategy="MANUAL")
    st.conn.execute("UPDATE jobs SET state='FAILED', publish_attempts=1, failure_phase=NULL WHERE job_key='x'")
    rec = st.get("x")
    assert is_uncertain(rec) and [b.job_key for b in st.account_blockers(IG_ID)] == ["x"]
    assert [r.job_key for r in st.find_blocking(IG_ID, "h")] == ["x"]


def test_failed_requires_failure_phase(tmp_path):
    st = StateStore(tmp_path / "s.db")
    st.insert(job_key="x", account_id=IG_ID, source_path="/a", relative_path="a", content_hash="h",
              size_bytes=1, graduation_strategy="MANUAL")
    with pytest.raises(ValueError, match="failure_phase"):
        st.update("x", "FAILED", last_error="no phase")


# ------------------------------------------------------------------ reconciliation
def unknown_job(cfg):
    run(cfg, failing_publish(FakeResponse(400, REAL_2207085)))


def test_reconcile_finished_stays_unknown(make_config, video, capsys):
    cfg = make_config()
    unknown_job(cfg)
    capsys.readouterr()
    s = happy_session(statuses=("FINISHED",))
    assert run(cfg, s, "--reconcile") == 0 and writes(s) == []
    out = capsys.readouterr().out
    assert "Verdict: UNRESOLVED" in out and "proves neither publication nor failure" in out
    assert "quota_usage was 2 before" in out and "absence proves nothing" in out and "expire around" in out
    assert run(cfg, s, "--mark-not-published") == 2
    assert run(cfg, s, "--mark-published") == 2          # no id and not PUBLISHED: refused
    assert StateStore(cfg.state_path).all()[0].state == "UNKNOWN"


def test_reconcile_published_without_media_id(make_config, video, capsys):
    cfg = make_config()
    unknown_job(cfg)
    s = happy_session(statuses=("PUBLISHED",))
    assert run(cfg, s, "--reconcile") == 0
    assert "CONFIRMED_PUBLISHED" in capsys.readouterr().out
    assert run(cfg, s, "--mark-published") == 0 and writes(s) == []
    store = StateStore(cfg.state_path)
    rec = store.all()[0]
    assert rec.state == "PUBLISHED" and rec.media_id is None          # never invented
    assert "media id not provided by Meta" in store.events(rec.job_key)[-1]["detail"]
    assert store.account_blockers(IG_ID) == []                         # account unpaused
    s2 = happy_session()
    assert run(cfg, s2) == 0 and writes(s2) == []                      # still not reposted


@pytest.mark.parametrize("status", ["EXPIRED", "ERROR"])
def test_reconcile_expired_or_error_allows_not_published(make_config, video, status):
    cfg = make_config()
    unknown_job(cfg)
    s = happy_session(statuses=(status,))
    assert run(cfg, s, "--mark-not-published") == 0 and writes(s) == []
    store = StateStore(cfg.state_path)
    rec = store.all()[0]
    assert rec.state == "FAILED" and rec.failure_phase == "RECONCILED_NOT_PUBLISHED"
    assert status in store.events(rec.job_key)[-1]["detail"]
    assert store.account_blockers(IG_ID) == []


def test_reconcile_container_unreadable_stays_unknown(make_config, video, capsys):
    cfg = make_config()
    unknown_job(cfg)
    s = happy_session()
    s.routes.insert(0, ["GET", "/900", [FakeResponse(400, {"error": {"message": "Unsupported get request",
                                                                     "code": 100}})]])
    assert run(cfg, s, "--reconcile") == 0
    assert "could not read container" in capsys.readouterr().out
    assert run(cfg, s, "--mark-not-published") == 2


def test_mark_published_with_found_media_id(make_config, video):
    cfg = make_config()
    unknown_job(cfg)
    s = happy_session(statuses=("FINISHED",), media_id="1800")
    s.routes.insert(0, ["GET", "/1800", [FakeResponse(200, {"id": "1800", "timestamp": "2099-01-01T00:00:00+0000",
                                                            "permalink": "https://www.instagram.com/reel/x/"})]])
    assert run(cfg, s, "--mark-published", "1800") == 0
    rec = StateStore(cfg.state_path).all()[0]
    assert rec.state == "PUBLISHED" and rec.media_id == "1800"


def test_mark_published_rejects_media_older_than_attempt(make_config, video):
    cfg = make_config()
    unknown_job(cfg)
    s = happy_session(statuses=("FINISHED",))
    s.routes.insert(0, ["GET", "/old", [FakeResponse(200, {"id": "old", "timestamp": "2020-01-01T00:00:00+0000"})]])
    assert run(cfg, s, "--mark-published", "old") == 2
    assert StateStore(cfg.state_path).all()[0].state == "UNKNOWN"


def test_reconcile_lists_legacy_failed_after_attempt(make_config, video, capsys):
    cfg = make_config()
    unknown_job(cfg)
    st = StateStore(cfg.state_path)
    st.conn.execute("UPDATE jobs SET state='FAILED', failure_phase=NULL")  # as the old code left job #3
    capsys.readouterr()
    assert run(cfg, happy_session(statuses=("FINISHED",)), "--reconcile") == 0
    assert "recorded FAILED, but media_publish was sent" in capsys.readouterr().out


def test_job_selector(make_config, video):
    cfg = make_config()
    unknown_job(cfg)
    st = StateStore(cfg.state_path)
    st.insert(job_key="second", account_id=IG_ID, source_path="/b", relative_path="b", content_hash="zz",
              size_bytes=1, graduation_strategy="MANUAL")
    st.update("second", "UNKNOWN", container_id="901")
    s = happy_session(statuses=("EXPIRED",))
    assert run(cfg, s, "--mark-not-published") == 2                      # ambiguous: which job?
    assert run(cfg, s, "--mark-not-published", "--job", "1") == 0
    assert {r.id: r.state for r in StateStore(cfg.state_path).all()}[1] == "FAILED"
