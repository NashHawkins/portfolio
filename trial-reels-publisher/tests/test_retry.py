"""--retry-publish: one user-approved re-send of media_publish for job #3's existing container."""
import dataclasses
import hashlib

import pytest

from conftest import TOKEN, FakeResponse, FakeSession, fake_mp4
from trial_reels import cli
from trial_reels.meta_client import MetaClient
from trial_reels.remux import Remuxer
from trial_reels.state import StateStore, is_uncertain

pytestmark = pytest.mark.usefixtures("legacy_resumable_trial")  # see conftest

PERSONAL_IG = "17840000000000002"
CONTAINER = "18657792691044336"
OTHER_IG = "17840000000000001"
MEDIA = "18000000000000099"
REAL_2207085 = {"error": {"message": "An internal server error occurred. Please try again later.",
                          "type": "OAuthException", "code": -1, "error_subcode": 2207085,
                          "is_transient": False, "fbtrace_id": "APj3idYk1jXSXU4WC5dOAC-"}}
ORIGINAL_ERROR = ("Meta API error during publish media (HTTP 400, code=-1, subcode=2207085, "
                  "type=OAuthException): An internal server error occurred. Please try again later.")
HASH = hashlib.sha256(b"job3-original").hexdigest()
STARTED = "2026-11-11T01:16:17+00:00"


class TTY:
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def interactive(monkeypatch):
    monkeypatch.setattr("sys.stdin", TTY())


@pytest.fixture
def cfg(make_config):
    return dataclasses.replace(make_config(IG_USER_ID=PERSONAL_IG), profile="personal")


def seed(cfg):
    """Reproduce the real layout: #1 test-account pre-publish failure, #2 test-account public Reel,
    #3 personal-account Trial Reel left UNKNOWN by 2207085 at media_publish."""
    st = StateStore(cfg.state_path)
    st.insert(job_key="j1", account_id=OTHER_IG, source_path="/v/IMG.mov", relative_path="IMG.mov",
              content_hash=HASH, size_bytes=10, graduation_strategy="MANUAL", account_profile="test")
    st.update("j1", "FAILED", failure_phase="PRE_PUBLISH", last_error="2207081")
    st.insert(job_key="j2", account_id=OTHER_IG, source_path="/v/IMG.mov", relative_path="IMG.mov",
              content_hash=HASH, size_bytes=10, graduation_strategy="MANUAL", publish_mode="PUBLIC",
              account_profile="test")
    st.update("j2", "PUBLISHED", media_id="18108325910203954")
    st.insert(job_key="j3", account_id=PERSONAL_IG, source_path="/v/IMG.mov", relative_path="IMG.mov",
              content_hash=HASH, size_bytes=10, graduation_strategy="MANUAL", account_profile="personal",
              account_username="nashhawkins")
    st.update("j3", "CONTAINER_CREATED", container_id=CONTAINER)
    st.update("j3", "UNKNOWN", last_error=ORIGINAL_ERROR)
    st.conn.execute("UPDATE jobs SET publish_attempts=1, publish_started_at=? WHERE job_key='j3'", (STARTED,))
    assert st.get("j3").id == 3
    return st


def session(*statuses, publish=None, account_id=PERSONAL_IG):
    s = FakeSession()
    s.on("GET", f"/{PERSONAL_IG}", FakeResponse(200, {"id": account_id, "username": "nashhawkins"}))
    s.on("GET", f"/{CONTAINER}", *[FakeResponse(200, {"status_code": x, "status": x}) for x in
                                   (statuses or ("FINISHED",))])
    s.on("GET", "/content_publishing_limit", FakeResponse(200, {"data": [
        {"quota_usage": 0, "config": {"quota_total": 100}}]}))
    s.on("POST", "/media_publish", publish or FakeResponse(200, {"id": MEDIA}))
    s.on("GET", f"/{MEDIA}", FakeResponse(200, {"id": MEDIA, "permalink": "https://www.instagram.com/reel/r/"}))
    return s


def run(cfg, s, *argv, answer="RETRY 3"):
    factory = lambda c: MetaClient(c, session=s, sleep=lambda x: None)  # noqa: E731
    return cli.main(list(argv), config=cfg, client_factory=factory, input_fn=lambda prompt: answer,
                    remuxer_factory=lambda c: Remuxer(c.tmp_dir, ffmpeg=None))


RETRY = ("--account", "personal", "--retry-publish", "--job", "3")


def writes(s):
    return [c["url"] for c in s.calls if c["method"] == "POST"]


def job3(cfg):
    return StateStore(cfg.state_path).get("j3")


# ------------------------------------------------------------------ success
def test_success_marks_published_and_unpauses(cfg, capsys):
    st = seed(cfg)
    s = session()
    assert run(cfg, s, *RETRY) == 0
    assert writes(s) == [f"https://graph.facebook.com/v27.0/{PERSONAL_IG}/media_publish"]  # nothing else
    assert s.calls[-1]["url"].endswith(MEDIA)  # permalink read after publish
    post = next(c for c in s.calls if c["method"] == "POST")
    assert post["data"] == {"creation_id": CONTAINER} and TOKEN not in str(post["data"])
    # status read twice (display + immediately before sending), the last GET of it right before the POST
    idx = s.calls.index(post)
    status_reads = [i for i, c in enumerate(s.calls) if c["url"].endswith(CONTAINER)]
    assert len(status_reads) == 2 and status_reads[-1] < idx
    rec = job3(cfg)
    assert rec.state == "PUBLISHED" and rec.media_id == MEDIA and rec.permalink.endswith("/reel/r/")
    assert rec.publish_attempts == 2 and rec.publish_started_at == STARTED  # original time kept
    assert rec.container_id == CONTAINER
    assert StateStore(cfg.state_path).account_blockers(PERSONAL_IG) == []
    call = st.api_calls("j3")[-1]
    assert (call["step"], call["outcome"], call["media_id"], call["container_status_before"]) == \
        ("retry_publish", "OK", MEDIA, "FINISHED")
    assert any("MANUAL RETRY" in (e["detail"] or "") for e in st.events("j3"))
    out = capsys.readouterr().out
    for shown in ("@nashhawkins", PERSONAL_IG, "#3", CONTAINER, "subcode=2207085", "Current status:   FINISHED"):
        assert shown in out


def test_works_without_the_video_file(cfg):
    """No upload, no remux, no file access: the source folder is empty."""
    seed(cfg)
    assert not any(cfg.videos_dir.iterdir())
    s = session()
    assert run(cfg, s, *RETRY) == 0
    assert not any("rupload" in u or u.endswith(f"{PERSONAL_IG}/media") for u in writes(s))


# ------------------------------------------------------------------ failure keeps UNKNOWN
def test_repeated_2207085_keeps_unknown_and_pause(cfg, videos_dir, capsys):
    st = seed(cfg)
    s = session(publish=FakeResponse(400, REAL_2207085))
    assert run(cfg, s, *RETRY) == 3
    assert len(writes(s)) == 1
    rec = job3(cfg)
    assert rec.state == "UNKNOWN" and rec.publish_attempts == 2 and is_uncertain(rec)
    assert "manual retry" in rec.last_error and "original attempt" in rec.last_error
    assert [b.id for b in st.account_blockers(PERSONAL_IG)] == [3]
    call = st.api_calls("j3")[-1]
    assert (call["step"], call["outcome"], call["error_code"], call["error_subcode"]) == \
        ("retry_publish", "UNCERTAIN", -1, 2207085)
    assert "stays UNKNOWN" in capsys.readouterr().out
    # normal publishing to this account is still blocked, with no write of any kind
    (videos_dir / "new.mp4").write_bytes(fake_mp4(payload=b"y" * 4096))
    s2 = session()
    assert run(cfg, s2, "--account", "personal", "--yes") == 2 and writes(s2) == []


def test_documented_rejection_of_retry_still_unknown(cfg):
    """A rejection of the RETRY proves nothing about the ORIGINAL attempt."""
    seed(cfg)
    s = session(publish=FakeResponse(400, {"error": {"message": "not ready", "code": 9007,
                                                     "error_subcode": 2207027}}))
    assert run(cfg, s, *RETRY) == 3
    rec = job3(cfg)
    assert rec.state == "UNKNOWN" and rec.failure_phase is None
    assert StateStore(cfg.state_path).api_calls("j3")[-1]["outcome"] == "REJECTED"


def test_interrupt_during_retry_is_unknown(cfg):
    seed(cfg)
    s = session(publish=KeyboardInterrupt())
    assert run(cfg, s, *RETRY) == 130
    assert job3(cfg).state == "UNKNOWN"


def test_success_without_media_id_is_unknown(cfg):
    seed(cfg)
    s = session(publish=FakeResponse(200, {"success": True}))
    assert run(cfg, s, *RETRY) == 3 and job3(cfg).state == "UNKNOWN"


# ------------------------------------------------------------------ container status gate
@pytest.mark.parametrize("status", ["EXPIRED", "PUBLISHED", "ERROR", "IN_PROGRESS", None])
def test_non_finished_container_is_never_retried(cfg, status, capsys):
    seed(cfg)
    s = session(status)
    assert run(cfg, s, *RETRY) == 2 and writes(s) == []
    rec = job3(cfg)
    assert rec.state == "UNKNOWN" and rec.publish_attempts == 1
    assert "Nothing was sent" in capsys.readouterr().err


def test_unreadable_container_is_never_retried(cfg):
    seed(cfg)
    s = session()
    s.routes.insert(0, ["GET", f"/{CONTAINER}", [FakeResponse(400, {"error": {"message": "x", "code": 100}})]])
    assert run(cfg, s, *RETRY) == 2 and writes(s) == []


def test_expires_while_confirmation_prompt_is_open(cfg):
    seed(cfg)
    s = session("FINISHED", "EXPIRED")
    assert run(cfg, s, *RETRY) == 2 and writes(s) == []
    assert job3(cfg).publish_attempts == 1


# ------------------------------------------------------------------ wrong account / job / container
def test_token_for_wrong_account_refused(cfg):
    seed(cfg)
    s = session(account_id=OTHER_IG)
    assert run(cfg, s, *RETRY) == 2 and writes(s) == []
    assert job3(cfg).state == "UNKNOWN"


def test_wrong_profile_refused_without_network(make_config):
    test_cfg = dataclasses.replace(make_config(IG_USER_ID=PERSONAL_IG), profile="test")
    seed(test_cfg)
    s = session()
    assert run(test_cfg, s, "--account", "test", "--retry-publish", "--job", "3") == 2
    assert s.calls == []


def test_profile_pointing_at_other_ig_id_refused(make_config):
    other = dataclasses.replace(make_config(IG_USER_ID=OTHER_IG), profile="personal")
    seed(other)
    s = session()
    assert run(other, s, *RETRY) == 2 and s.calls == []


@pytest.mark.parametrize("job", ["1", "2", "4"])
def test_unapproved_job_refused(cfg, job):
    seed(cfg)
    s = session()
    assert run(cfg, s, "--account", "personal", "--retry-publish", "--job", job) == 2 and s.calls == []


def test_container_mismatch_refused(cfg):
    st = seed(cfg)
    st.conn.execute("UPDATE jobs SET container_id='999' WHERE job_key='j3'")
    s = session()
    assert run(cfg, s, *RETRY) == 2 and s.calls == []


# ------------------------------------------------------------------ duplicates / confirmation
def test_second_retry_after_success_refused(cfg):
    seed(cfg)
    assert run(cfg, session(), *RETRY) == 0
    s = session()
    assert run(cfg, s, *RETRY) == 2 and s.calls == []
    assert job3(cfg).publish_attempts == 2


def test_second_retry_after_failure_refused(cfg):
    seed(cfg)
    assert run(cfg, session(publish=FakeResponse(400, REAL_2207085)), *RETRY) == 3
    s = session()
    assert run(cfg, s, *RETRY) == 2 and s.calls == []
    assert job3(cfg).publish_attempts == 2


def test_begin_retry_is_compare_and_set(cfg):
    st = seed(cfg)
    st.begin_retry_publish("j3", CONTAINER, 1)
    with pytest.raises(Exception, match="Refusing to retry"):
        st.begin_retry_publish("j3", CONTAINER, 1)        # concurrent second attempt loses
    st.conn.execute("UPDATE jobs SET state='UNKNOWN' WHERE job_key='j3'")
    with pytest.raises(Exception, match="Refusing to retry"):
        st.begin_retry_publish("j3", CONTAINER, 1)        # attempts now 2: no more retries


def test_other_unresolved_job_on_account_refuses(cfg):
    st = seed(cfg)
    st.insert(job_key="j4", account_id=PERSONAL_IG, source_path="/v/b.mp4", relative_path="b.mp4",
              content_hash="other", size_bytes=1, graduation_strategy="MANUAL")
    st.update("j4", "UNKNOWN", container_id="901")
    s = session()
    assert run(cfg, s, *RETRY) == 2 and s.calls == []


@pytest.mark.parametrize("answer", ["", "post", "retry 3", "RETRY", "RETRY 4", "yes"])
def test_wrong_confirmation_cancels(cfg, answer):
    seed(cfg)
    s = session()
    assert run(cfg, s, *RETRY, answer=answer) == 1 and writes(s) == []
    assert job3(cfg).publish_attempts == 1 and job3(cfg).state == "UNKNOWN"


def test_non_interactive_refused(cfg, monkeypatch):
    seed(cfg)
    monkeypatch.setattr("sys.stdin", type("NoTTY", (), {"isatty": lambda self: False})())
    s = session()
    assert run(cfg, s, *RETRY) == 2 and s.calls == []


@pytest.mark.parametrize("argv", [("--account", "personal", "--retry-publish"),
                                  ("--retry-publish", "--job", "3"),
                                  RETRY + ("--yes",), RETRY + ("--public",)])
def test_cli_requires_explicit_arguments(cfg, argv):
    seed(cfg)
    with pytest.raises(SystemExit) as e:
        run(cfg, session(), *argv)
    assert e.value.code == 2
