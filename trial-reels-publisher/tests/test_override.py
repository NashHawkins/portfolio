"""--override-unknown-job: one confirmed URL Trial Reel attempt while job #3 stays UNKNOWN."""
import dataclasses
import hashlib
import json
import sqlite3

import pytest

from conftest import FakeResponse, fake_mp4, happy_session
from trial_reels import cli
from trial_reels.errors import StateError
from trial_reels.hosted import HostedVideo
from trial_reels.meta_client import MetaClient
from trial_reels.override import CONFIRM_PHRASE
from trial_reels.remux import Remuxer
from trial_reels.state import StateStore, is_uncertain
from trial_reels.validation import VideoReport

pytestmark = pytest.mark.usefixtures("legacy_resumable_trial")  # see conftest

PERSONAL_IG = "17840000000000002"
TEST_IG = "17840000000000001"
CONTAINER = "18657792691044336"
URL = "https://pub-example.r2.dev/IMG_3813_cloud.mp4"
HOSTED = fake_mp4(payload=b"h" * 3000)
REAL_2207085 = {"error": {"message": "An internal server error occurred. Please try again later.",
                          "type": "OAuthException", "code": -1, "error_subcode": 2207085,
                          "is_transient": False, "fbtrace_id": "T"}}
ARGS = ("--account", "personal", "--upload-method", "url", "--video-url", URL, "--override-unknown-job", "3")
YES = ("nashhawkins", CONFIRM_PHRASE)


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
    return dataclasses.replace(make_config(IG_USER_ID=PERSONAL_IG), profile="personal")


def vhash(video):
    return hashlib.sha256(video.read_bytes()).hexdigest()


def seed(cfg, video):
    """The real layout: #1 test pre-publish failure, #2 test PUBLIC Reel, #3 personal TRIAL UNKNOWN."""
    h = vhash(video)
    st = StateStore(cfg.state_path)
    st.insert(job_key="j1", account_id=TEST_IG, source_path=str(video), relative_path=video.name,
              content_hash=h, size_bytes=1, graduation_strategy="MANUAL", account_profile="test")
    st.update("j1", "FAILED", failure_phase="PRE_PUBLISH", last_error="2207081")
    st.insert(job_key="j2", account_id=TEST_IG, source_path=str(video), relative_path=video.name,
              content_hash=h, size_bytes=1, graduation_strategy=None, publish_mode="PUBLIC")
    st.update("j2", "PUBLISHED", media_id="18108325910203954")
    st.insert(job_key="j3", account_id=PERSONAL_IG, source_path=str(video), relative_path=video.name,
              content_hash=h, size_bytes=1, graduation_strategy="MANUAL", account_profile="personal",
              account_username="nashhawkins")
    st.update("j3", "CONTAINER_CREATED", container_id=CONTAINER)
    st.update("j3", "UNKNOWN", last_error="code=-1 subcode=2207085")
    st.conn.execute("UPDATE jobs SET publish_attempts=2, publish_started_at='2026-11-11T01:40:00+00:00' "
                    "WHERE job_key='j3'")
    assert st.get("j3").id == 3
    return st


def snapshot(cfg, key="j3"):
    st = StateStore(cfg.state_path)
    return st.get(key), st.events(key), st.api_calls(key)


def session(blocker_status="FINISHED"):
    s = happy_session(ig_id=PERSONAL_IG, username="nashhawkins")
    if isinstance(blocker_status, FakeResponse):
        s.on("GET", f"/{CONTAINER}", blocker_status)
    else:
        s.on("GET", f"/{CONTAINER}", FakeResponse(200, {"status_code": blocker_status, "status": blocker_status}))
    return s


def hosted(identity="MATCH"):
    calls = []

    def inspector(u, *, tmp_root, source_path, ffmpeg):
        calls.append(u)
        return HostedVideo(url=u, host="pub-x.r2.dev", addresses=["104.18.2.35"], content_type="video/mp4",
                           content_length=len(HOSTED), sha256=hashlib.sha256(HOSTED).hexdigest(),
                           report=VideoReport(path=source_path, size_bytes=len(HOSTED), sha256="x"),
                           stream_sha256="ab" * 32,
                           source_stream_sha256="ab" * 32 if identity == "MATCH" else "cd" * 32,
                           identity=identity)
    inspector.calls = calls
    return inspector


def run(cfg, s, argv=ARGS, answers=YES, inspector=None):
    it = iter(answers)
    factory = lambda c: MetaClient(c, session=s, sleep=lambda x: None)  # noqa: E731
    return cli.main(list(argv), config=cfg, client_factory=factory, input_fn=lambda p: next(it, ""),
                    remuxer_factory=lambda c: Remuxer(c.tmp_dir, ffmpeg=None),
                    hosted_inspector=inspector or hosted())


def writes(s):
    return [c for c in s.calls if c["method"] == "POST"]


def store(cfg):
    return StateStore(cfg.state_path)


def new_job(cfg):
    return next(r for r in store(cfg).all() if r.id > 3)


# ------------------------------------------------------------------ success path
def test_success_publishes_once_and_leaves_job3_unknown(cfg, video, capsys):
    seed(cfg, video)
    before = snapshot(cfg)
    s = session()
    assert run(cfg, s) == 0
    assert [c["url"] for c in writes(s)] == [f"https://graph.facebook.com/v27.0/{PERSONAL_IG}/media",
                                            f"https://graph.facebook.com/v27.0/{PERSONAL_IG}/media_publish"]
    body = json.loads(writes(s)[0]["data"])
    assert body["video_url"] == URL and body["media_type"] == "REELS"
    assert body["trial_params"] == {"graduation_strategy": "MANUAL"} and "upload_type" not in body
    assert not any("rupload" in c["url"] for c in s.calls)

    assert snapshot(cfg) == before                                   # job #3: row, events, api_calls untouched
    assert store(cfg).get("j3").state == "UNKNOWN"
    rec = new_job(cfg)
    assert rec.id == 4 and rec.state == "PUBLISHED" and rec.upload_method == "url" and rec.video_url == URL
    assert rec.publish_mode == "TRIAL" and rec.publish_attempts == 1 and rec.content_hash == vhash(video)
    [ov] = store(cfg).overrides()
    assert ov["blocker_job_id"] == 3 and ov["blocker_job_key"] == "j3" and ov["new_job_key"] == rec.job_key
    assert ov["blocker_container_id"] == CONTAINER and ov["blocker_container_status"] == "FINISHED"
    assert ov["account_id"] == PERSONAL_IG and ov["video_url"] == URL and ov["publish_claimed_at"]
    assert CONFIRM_PHRASE in ov["confirmation"]
    out = capsys.readouterr().out
    assert "DUPLICATE RISK" in out and "Override #1 recorded" in out


def test_status_lists_override_separately(cfg, video, capsys):
    seed(cfg, video)
    assert run(cfg, session()) == 0
    capsys.readouterr()
    assert cli.main(["--account", "personal", "--status"], config=cfg) == 0
    out = capsys.readouterr().out
    assert "override #1" in out and "blocker job #3 (unchanged)" in out and "new job #4" in out
    assert "#3 " in out and "UNKNOWN" in out


def test_override_is_single_use(cfg, video, capsys):
    seed(cfg, video)
    assert run(cfg, session()) == 0
    s, insp = session(), hosted()
    capsys.readouterr()
    assert run(cfg, s, inspector=insp) == 2
    assert s.calls == [] and insp.calls == []                         # refused locally
    assert "already used" in capsys.readouterr().err
    assert len(store(cfg).overrides()) == 1


# ------------------------------------------------------------------ confirmations
@pytest.mark.parametrize("answers", [("", CONFIRM_PHRASE), ("post", CONFIRM_PHRASE), ("nashhawkins", ""),
                                     ("nashhawkins", "yes"), ("nashhawkins", CONFIRM_PHRASE.lower()),
                                     ("nashhawkins", "RETRY 3")])
def test_any_wrong_answer_cancels_without_recording(cfg, video, answers):
    seed(cfg, video)
    before = snapshot(cfg)
    s = session()
    assert run(cfg, s, answers=answers) == 1
    assert writes(s) == [] and store(cfg).overrides() == [] and len(store(cfg).all()) == 3
    assert snapshot(cfg) == before


def test_non_interactive_refused_before_network(cfg, video, monkeypatch):
    seed(cfg, video)
    monkeypatch.setattr("sys.stdin", type("N", (), {"isatty": lambda self: False})())
    s = session()
    assert run(cfg, s) == 2 and s.calls == []


# ------------------------------------------------------------------ scope restrictions (all local, no network)
def test_other_video_url_refused(cfg, video):
    seed(cfg, video)
    s, insp = session(), hosted()
    argv = list(ARGS)
    argv[argv.index(URL)] = URL.replace("IMG_3813_cloud", "other")
    assert run(cfg, s, argv=argv, inspector=insp) == 2
    assert s.calls == [] and insp.calls == [] and store(cfg).overrides() == []


def test_other_profile_or_account_refused(cfg, video, make_config):
    seed(cfg, video)
    test_cfg = dataclasses.replace(cfg, profile="test")
    argv = ["--account", "test"] + list(ARGS[2:])
    s = session()
    assert run(test_cfg, s, argv=argv) == 2 and s.calls == []
    wrong_id = dataclasses.replace(make_config(IG_USER_ID=TEST_IG, TRIAL_REELS_DATA_DIR=str(cfg.state_path.parent)),
                                   profile="personal")
    assert wrong_id.state_path == cfg.state_path
    s = session()
    assert run(wrong_id, s) == 2 and s.calls == []


def test_unapproved_job_id_refused(cfg, video):
    seed(cfg, video)
    argv = list(ARGS)
    argv[-1] = "4"
    s = session()
    assert run(cfg, s, argv=argv) == 2 and s.calls == []


def test_different_video_in_folder_refused(cfg, video):
    seed(cfg, video)
    video.write_bytes(fake_mp4(payload=b"different" * 300))
    s = session()
    assert run(cfg, s) == 2 and s.calls == []


def test_job3_not_unknown_refused(cfg, video):
    seed(cfg, video).update("j3", "FAILED", failure_phase="RECONCILED_NOT_PUBLISHED")
    s = session()
    assert run(cfg, s) == 2 and s.calls == []


def test_another_unresolved_job_still_blocks(cfg, video):
    st = seed(cfg, video)
    st.insert(job_key="j9", account_id=PERSONAL_IG, source_path="/v/other.mp4", relative_path="other.mp4",
              content_hash="other", size_bytes=1, graduation_strategy="MANUAL")
    st.update("j9", "UNKNOWN")
    s = session()
    assert run(cfg, s) == 2 and s.calls == [] and store(cfg).overrides() == []


def test_unknown_account_job_still_blocks(cfg, video):
    st = seed(cfg, video)
    st.insert(job_key="j9", account_id="", source_path="/v/other.mp4", relative_path="other.mp4",
              content_hash="other", size_bytes=1, graduation_strategy="MANUAL")
    st.update("j9", "UNKNOWN")
    s = session()
    assert run(cfg, s) == 2 and s.calls == []


def test_same_video_published_on_account_still_blocks(cfg, video):
    st = seed(cfg, video)
    st.insert(job_key="j9", account_id=PERSONAL_IG, source_path="/v/copy.mov", relative_path="copy.mov",
              content_hash=vhash(video), size_bytes=1, graduation_strategy=None, publish_mode="PUBLIC")
    st.update("j9", "PUBLISHED", media_id="1")
    s = session()
    assert run(cfg, s) == 2 and s.calls == []


def test_identity_mismatch_refused(cfg, video):
    seed(cfg, video)
    s = session()
    assert run(cfg, s, inspector=hosted("MISMATCH")) == 2 and s.calls == []


# ------------------------------------------------------------------ job #3 container must still be FINISHED
@pytest.mark.parametrize("status", ["PUBLISHED", "EXPIRED", "ERROR", "IN_PROGRESS"])
def test_blocker_container_not_finished_refused(cfg, video, status, capsys):
    seed(cfg, video)
    s = session(status)
    assert run(cfg, s) == 2
    assert writes(s) == [] and store(cfg).overrides() == []
    if status == "PUBLISHED":
        assert "certain duplicate" in capsys.readouterr().err


def test_blocker_status_unreadable_refused(cfg, video):
    seed(cfg, video)
    s = session(FakeResponse(400, {"error": {"message": "nope", "code": 100}}))
    assert run(cfg, s) == 2 and writes(s) == [] and store(cfg).overrides() == []


def test_wrong_token_account_refused(cfg, video):
    seed(cfg, video)
    s = session()
    s.routes.insert(0, ["GET", f"/{PERSONAL_IG}", [FakeResponse(200, {"id": TEST_IG, "username": "example_account"})]])
    assert run(cfg, s) == 2 and writes(s) == [] and store(cfg).overrides() == []


# ------------------------------------------------------------------ failure outcomes
def test_ambiguous_publish_makes_new_job_unknown_no_retry(cfg, video):
    seed(cfg, video)
    before = snapshot(cfg)
    s = session()
    s.routes.insert(0, ["POST", "/media_publish", [FakeResponse(400, REAL_2207085)]])
    assert run(cfg, s) == 3
    assert sum(c["url"].endswith("media_publish") for c in writes(s)) == 1
    rec = new_job(cfg)
    assert rec.state == "UNKNOWN" and is_uncertain(rec) and rec.publish_attempts == 1
    assert snapshot(cfg) == before
    assert {b.id for b in store(cfg).account_blockers(PERSONAL_IG)} == {3, rec.id}
    for argv in (ARGS, ARGS[:6]):                                     # override again, or a plain URL run
        s2 = session()
        assert run(cfg, s2, argv=argv) == 2 and s2.calls == []


def test_pre_publish_failure_spends_override(cfg, video, capsys):
    seed(cfg, video)
    s = session()
    s.routes.insert(0, ["POST", f"/{PERSONAL_IG}/media", [FakeResponse(400, {"error": {
        "message": "Invalid video_url", "code": 9004, "error_subcode": 2207052}})]])
    assert run(cfg, s) == 2
    rec = new_job(cfg)
    assert rec.state == "FAILED" and rec.failure_phase == "PRE_PUBLISH" and len(writes(s)) == 1
    [ov] = store(cfg).overrides()
    assert ov["publish_claimed_at"] is None
    s2, insp = session(), hosted()
    capsys.readouterr()
    assert run(cfg, s2, inspector=insp) == 2 and s2.calls == [] and insp.calls == []   # one attempt only
    assert "already used" in capsys.readouterr().err
    assert store(cfg).get("j3").state == "UNKNOWN"


def test_trial_rejection_never_falls_back_to_public(cfg, video):
    seed(cfg, video)
    s = session()
    s.routes.insert(0, ["POST", f"/{PERSONAL_IG}/media", [FakeResponse(400, {"error": {
        "message": "does not meet the trial reel follower requirement", "code": 10, "error_subcode": 2207081}})]])
    assert run(cfg, s) == 2
    assert len(writes(s)) == 1 and json.loads(writes(s)[0]["data"])["trial_params"]


def test_normal_commands_still_blocked_by_job3(cfg, video):
    seed(cfg, video)
    assert run(cfg, session()) == 0
    for argv in (ARGS[:6], ("--account", "personal", "--yes")):
        s = session()
        assert run(cfg, s, argv=argv) == 2 and writes(s) == []


# ------------------------------------------------------------------ atomic claim in SQLite
def _armed(cfg, video):
    st = seed(cfg, video)
    st.insert(job_key="new", account_id=PERSONAL_IG, source_path=str(video), relative_path=video.name,
              content_hash=vhash(video), size_bytes=1, graduation_strategy="MANUAL", upload_method="url",
              video_url=URL)
    st.update("new", "READY")
    ov = st.record_override(kind="K", blocker=st.get("j3"), blocker_container_status="FINISHED",
                            account_profile="personal", account_username="nashhawkins", upload_method="url",
                            video_url=URL, new_job_key="new", confirmation="x")
    return st, ov


def test_claim_rechecks_other_blockers_atomically(cfg, video):
    st, ov = _armed(cfg, video)
    st.insert(job_key="late", account_id=PERSONAL_IG, source_path="/v/o.mp4", relative_path="o.mp4",
              content_hash="o", size_bytes=1, graduation_strategy="MANUAL")
    st.update("late", "UNKNOWN")
    with pytest.raises(StateError):
        st.begin_override_publish("new", ov["id"])
    assert st.get("new").state == "READY" and st.get_override(ov["id"])["publish_claimed_at"] is None


def test_claim_only_for_bound_job_and_once(cfg, video):
    st, ov = _armed(cfg, video)
    with pytest.raises(StateError):
        st.begin_override_publish("j3", ov["id"])
    assert st.begin_override_publish("new", ov["id"]).state == "PUBLISHING"
    st.update("new", "READY")
    with pytest.raises(StateError):
        st.begin_override_publish("new", ov["id"])
    with pytest.raises(StateError):                                   # normal claim still sees job #3
        st.begin_publish("new")


def test_second_override_for_same_blocker_rejected_by_db(cfg, video):
    st, ov = _armed(cfg, video)
    with pytest.raises(StateError, match="once only"):
        st.record_override(kind="K", blocker=st.get("j3"), blocker_container_status="FINISHED",
                           account_profile="personal", account_username="n", upload_method="url",
                           video_url=URL, new_job_key="other", confirmation="x")


def test_v5_db_migrates_to_current_with_backup(cfg, video):
    seed(cfg, video).close()
    c = sqlite3.connect(str(cfg.state_path))
    c.execute("DROP TABLE overrides")
    c.execute("PRAGMA user_version = 5")
    c.commit()
    c.close()
    st = StateStore(cfg.state_path)
    assert st.backup_path is not None and st.backup_path.exists()
    assert st.conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert st.overrides() == [] and [r.id for r in st.all()] == [1, 2, 3] and st.get("j3").state == "UNKNOWN"


# ------------------------------------------------------------------ argument rules
@pytest.mark.parametrize("argv", [
    ("--account", "personal", "--override-unknown-job", "3"),
    ("--account", "personal", "--upload-method", "url", "--video-url", URL, "--override-unknown-job", "3", "--dry-run"),
    ("--account", "personal", "--upload-method", "url", "--video-url", URL, "--override-unknown-job", "3", "--yes"),
    ("--account", "personal", "--upload-method", "url", "--video-url", URL, "--override-unknown-job", "3", "--public"),
    ("--account", "personal", "--upload-method", "url", "--video-url", URL, "--override-unknown-job", "3",
     "--job", "3"),
    ("--upload-method", "url", "--video-url", URL, "--override-unknown-job", "3"),
])
def test_argument_rules(argv):
    with pytest.raises(SystemExit):
        cli.parse_args(list(argv))
