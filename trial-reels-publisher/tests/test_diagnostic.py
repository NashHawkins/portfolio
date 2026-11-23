"""--diagnostic-public-override: one normal PUBLIC Reel of IMG_2248.MOV on @nashhawkins while trial jobs
#3 and #4 stay UNKNOWN."""
import dataclasses
import hashlib

import pytest

from conftest import FakeResponse, UPLOAD_URI, fake_mp4, happy_session
from trial_reels import cli, diagnostic
from trial_reels.diagnostic import APPROVED_DIAGNOSTIC, CONFIRM_PHRASE, ApprovedDiagnostic
from trial_reels.errors import StateError
from trial_reels.meta_client import MetaClient
from trial_reels.remux import Remuxer
from trial_reels.state import StateStore, is_uncertain

pytestmark = pytest.mark.usefixtures("legacy_resumable_trial")  # see conftest

PERSONAL_IG = "17840000000000002"
TEST_IG = "17840000000000001"
C3, C4 = "18657792691044336", "18658002550044336"
OLD_HASH = "b016ac4cda1eddfae39e102a41c19a6e285bc6237266ce769e0e2442ad6cbbad"   # IMG_3813.MOV
R2 = "https://pub-example.r2.dev/IMG_3813_cloud.mp4"
ARGS = ("--account", "personal", "--public", "--diagnostic-public-override")
YES = ("nashhawkins", CONFIRM_PHRASE)
REAL_2207085 = {"error": {"message": "An internal server error occurred. Please try again later.",
                          "type": "OAuthException", "code": -1, "error_subcode": 2207085,
                          "is_transient": False, "fbtrace_id": "T"}}


class TTY:
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def interactive(monkeypatch):
    monkeypatch.setattr("sys.stdin", TTY())


@pytest.fixture
def video(videos_dir, monkeypatch):
    p = videos_dir / "IMG_2248.MOV"
    p.write_bytes(fake_mp4(payload=b"new-video" * 300))
    monkeypatch.setattr(diagnostic, "APPROVED_DIAGNOSTIC", dataclasses.replace(
        APPROVED_DIAGNOSTIC, content_sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
    return p


@pytest.fixture
def cfg(make_config):
    return dataclasses.replace(make_config(IG_USER_ID=PERSONAL_IG), profile="personal")


def seed(cfg, videos_dir):
    """The real layout: #1/#2 on the test account, #3 resumable trial UNKNOWN, #4 URL trial UNKNOWN,
    and the spent trial override #1 for job #3."""
    st = StateStore(cfg.state_path)
    old = str(videos_dir / "IMG_3813.MOV")
    st.insert(job_key="j1", account_id=TEST_IG, source_path=old, relative_path="IMG_3813.MOV",
              content_hash=OLD_HASH, size_bytes=1, graduation_strategy="MANUAL")
    st.update("j1", "FAILED", failure_phase="PRE_PUBLISH", last_error="2207081")
    st.insert(job_key="j2", account_id=TEST_IG, source_path=old, relative_path="IMG_3813.MOV",
              content_hash=OLD_HASH, size_bytes=1, graduation_strategy=None, publish_mode="PUBLIC")
    st.update("j2", "PUBLISHED", media_id="18108325910203954")
    for key, cont, method, attempts in (("j3", C3, "resumable", 2), ("j4", C4, "url", 1)):
        st.insert(job_key=key, account_id=PERSONAL_IG, source_path=old, relative_path="IMG_3813.MOV",
                  content_hash=OLD_HASH, size_bytes=1, graduation_strategy="MANUAL", account_profile="personal",
                  account_username="nashhawkins", upload_method=method, video_url=R2 if method == "url" else None)
        st.update(key, "CONTAINER_CREATED", container_id=cont)
        st.update(key, "UNKNOWN", last_error="code=-1 subcode=2207085")
        st.conn.execute("UPDATE jobs SET publish_attempts=?, publish_started_at='2026-11-11T15:59:52+00:00' "
                        "WHERE job_key=?", (attempts, key))
    st.record_override(kind="URL_TRIAL_DESPITE_UNKNOWN", blocker=st.get("j3"), blocker_container_status="FINISHED",
                       account_profile="personal", account_username="nashhawkins", upload_method="url",
                       video_url=R2, new_job_key="j4", confirmation="x")
    st.conn.execute("UPDATE overrides SET publish_claimed_at='2026-11-11T15:59:52+00:00'")
    assert [st.get(k).id for k in ("j3", "j4")] == [3, 4]
    return st


def snapshot(cfg):
    st = StateStore(cfg.state_path)
    return [(st.get(k), st.events(k), st.api_calls(k)) for k in ("j1", "j2", "j3", "j4")], st.overrides()


def session(s3="FINISHED", s4="FINISHED"):
    s = happy_session(ig_id=PERSONAL_IG, username="nashhawkins")
    for cont, st in ((C3, s3), (C4, s4)):
        s.on("GET", f"/{cont}", st if isinstance(st, FakeResponse) else
             FakeResponse(200, {"status_code": st, "status": st}))
    return s


def run(cfg, s, argv=ARGS, answers=YES, ffmpeg=None):
    it = iter(answers)
    factory = lambda c: MetaClient(c, session=s, sleep=lambda x: None)  # noqa: E731
    return cli.main(list(argv), config=cfg, client_factory=factory, input_fn=lambda p: next(it, ""),
                    remuxer_factory=lambda c: Remuxer(c.tmp_dir, ffmpeg=ffmpeg))


def writes(s):
    return [c for c in s.calls if c["method"] == "POST"]


def store(cfg):
    return StateStore(cfg.state_path)


def new_job(cfg):
    return next(r for r in store(cfg).all() if r.id > 4)


# ------------------------------------------------------------------ approval is pinned to the real experiment
def test_approval_is_pinned_to_img_2248_on_personal():
    a = APPROVED_DIAGNOSTIC
    assert a.profile == "personal" and a.ig_user_id == PERSONAL_IG
    assert a.content_sha256 == "395f9eb51ddbedc7d6f49f7ed8404beb7ee07de1170637cd28fddcde6760ff7f"
    assert a.blocker_job_ids == frozenset({3, 4})


# ------------------------------------------------------------------ success
def test_success_public_resumable_no_trial_params(cfg, video, capsys):
    seed(cfg, video.parent)
    before = snapshot(cfg)
    s = session()
    assert run(cfg, s) == 0
    assert [c["url"] for c in writes(s)] == [f"https://graph.facebook.com/v27.0/{PERSONAL_IG}/media", UPLOAD_URI,
                                            f"https://graph.facebook.com/v27.0/{PERSONAL_IG}/media_publish"]
    assert writes(s)[0]["data"] == {"media_type": "REELS", "upload_type": "resumable"}     # exactly, no trial
    assert writes(s)[2]["data"] == {"creation_id": "900"}
    assert sum(c["url"].endswith("media_publish") for c in s.calls) == 1

    assert snapshot(cfg) == before                                  # #1–#4 and the trial override untouched
    rec = new_job(cfg)
    assert rec.id == 5 and rec.state == "PUBLISHED" and rec.publish_mode == "PUBLIC"
    assert rec.upload_method == "resumable" and rec.graduation_strategy is None and rec.publish_attempts == 1
    assert rec.relative_path == "IMG_2248.MOV" and rec.media_id == "1800" and rec.container_id == "900"
    [ov] = store(cfg).diagnostic_overrides()
    assert ov["kind"] == diagnostic.KIND and ov["new_job_key"] == rec.job_key and ov["publish_claimed_at"]
    assert ov["publish_mode"] == "PUBLIC" and ov["upload_method"] == "resumable" and ov["account_id"] == PERSONAL_IG
    assert [(b["blocker_job_id"], b["blocker_container_id"], b["blocker_container_status"]) for b in ov["blockers"]] \
        == [(3, C3, "FINISHED"), (4, C4, "FINISHED")]
    out = capsys.readouterr().out
    assert "PUBLIC" in out and "Stays UNKNOWN: job #3" in out and "Stays UNKNOWN: job #4" in out
    assert {b.id for b in store(cfg).account_blockers(PERSONAL_IG)} == {3, 4}            # still paused


def test_status_lists_diagnostic_separately(cfg, video, capsys):
    seed(cfg, video.parent)
    assert run(cfg, session()) == 0
    capsys.readouterr()
    assert cli.main(["--account", "personal", "--status"], config=cfg) == 0
    out = capsys.readouterr().out
    assert "diagnostic override #1" in out and "#3 (FINISHED), #4 (FINISHED)" in out and "new job #5" in out
    assert "override #1 " in out                                     # the trial override is still listed


# ------------------------------------------------------------------ single use, no reuse of the trial override
def test_diagnostic_is_single_use(cfg, video, capsys):
    seed(cfg, video.parent)
    assert run(cfg, session()) == 0
    s = session()
    capsys.readouterr()
    assert run(cfg, s) == 2 and s.calls == []
    assert "already used" in capsys.readouterr().err


def test_pre_publish_failure_spends_diagnostic(cfg, video, capsys):
    seed(cfg, video.parent)
    s = session()
    s.routes.insert(0, ["POST", f"/{PERSONAL_IG}/media", [FakeResponse(400, {"error": {"message": "bad", "code": 100}})]])
    assert run(cfg, s) == 2
    assert new_job(cfg).state == "FAILED" and new_job(cfg).failure_phase == "PRE_PUBLISH" and len(writes(s)) == 1
    s2 = session()
    capsys.readouterr()
    assert run(cfg, s2) == 2 and s2.calls == [] and "already used" in capsys.readouterr().err


def test_trial_override_cannot_be_reused(cfg, video):
    seed(cfg, video.parent)
    s = session()
    argv = ("--account", "personal", "--upload-method", "url", "--video-url", R2, "--override-unknown-job", "3")
    assert run(cfg, s, argv=argv) == 2 and s.calls == []
    assert store(cfg).diagnostic_overrides() == []


def test_normal_public_run_still_blocked(cfg, video):
    seed(cfg, video.parent)
    for argv in (("--account", "personal", "--public"), ("--account", "personal", "--yes")):
        s = session()
        assert run(cfg, s, argv=argv, answers=("post", "PUBLIC")) == 2 and writes(s) == []
    assert store(cfg).diagnostic_overrides() == []


# ------------------------------------------------------------------ confirmations
@pytest.mark.parametrize("answers", [("", CONFIRM_PHRASE), ("post", CONFIRM_PHRASE), ("nashhawkins", ""),
                                     ("nashhawkins", "PUBLIC"), ("nashhawkins", "I ACCEPT A POSSIBLE DUPLICATE"),
                                     ("nashhawkins", CONFIRM_PHRASE.lower())])
def test_wrong_answer_cancels_without_recording(cfg, video, answers):
    seed(cfg, video.parent)
    before = snapshot(cfg)
    s = session()
    assert run(cfg, s, answers=answers) == 1
    assert writes(s) == [] and store(cfg).diagnostic_overrides() == [] and len(store(cfg).all()) == 4
    assert snapshot(cfg) == before


def test_non_interactive_refused_before_network(cfg, video, monkeypatch):
    seed(cfg, video.parent)
    monkeypatch.setattr("sys.stdin", type("N", (), {"isatty": lambda self: False})())
    s = session()
    assert run(cfg, s) == 2 and s.calls == []


# ------------------------------------------------------------------ scope (all refused locally, no network)
def test_only_personal_profile(cfg, video):
    seed(cfg, video.parent)
    s = session()
    assert run(dataclasses.replace(cfg, profile="test"), s, argv=("--account", "test") + ARGS[2:]) == 2
    assert s.calls == []


def test_only_personal_ig_id(cfg, video, make_config):
    seed(cfg, video.parent)
    other = dataclasses.replace(make_config(IG_USER_ID=TEST_IG), profile="personal")
    assert other.state_path == cfg.state_path
    s = session()
    assert run(other, s) == 2 and s.calls == []


def test_only_the_selected_video(cfg, video):
    seed(cfg, video.parent)
    video.write_bytes(fake_mp4(payload=b"some other video" * 200))
    s = session()
    assert run(cfg, s) == 2 and s.calls == []


def test_more_than_one_video_refused(cfg, video):
    seed(cfg, video.parent)
    (video.parent / "IMG_3813.MOV").write_bytes(fake_mp4())
    s = session()
    assert run(cfg, s) == 2 and s.calls == []


@pytest.mark.parametrize("account", [PERSONAL_IG, ""])
def test_other_unresolved_job_still_blocks(cfg, video, account):
    st = seed(cfg, video.parent)
    st.insert(job_key="j9", account_id=account, source_path="/v/x.mp4", relative_path="x.mp4",
              content_hash="x", size_bytes=1, graduation_strategy="MANUAL")
    st.update("j9", "UNKNOWN")
    s = session()
    assert run(cfg, s) == 2 and s.calls == [] and store(cfg).diagnostic_overrides() == []


def test_duplicate_protection_intact(cfg, video):
    st = seed(cfg, video.parent)
    st.insert(job_key="j9", account_id=PERSONAL_IG, source_path="/elsewhere/copy.mov", relative_path="copy.mov",
              content_hash=hashlib.sha256(video.read_bytes()).hexdigest(), size_bytes=1, graduation_strategy="MANUAL")
    st.update("j9", "PUBLISHED", media_id="1")
    s = session()
    assert run(cfg, s) == 2 and s.calls == [] and store(cfg).diagnostic_overrides() == []


def test_moov_at_end_without_ffmpeg_refused_before_network(cfg, video, monkeypatch):
    seed(cfg, video.parent)
    video.write_bytes(fake_mp4(moov_first=False, payload=b"new-video" * 300))
    monkeypatch.setattr(diagnostic, "APPROVED_DIAGNOSTIC", dataclasses.replace(
        diagnostic.APPROVED_DIAGNOSTIC, content_sha256=hashlib.sha256(video.read_bytes()).hexdigest()))
    s = session()
    assert run(cfg, s, ffmpeg=None) == 2 and s.calls == []


# ------------------------------------------------------------------ blocker evidence from Meta
def test_blocker_published_refused(cfg, video, capsys):
    seed(cfg, video.parent)
    s = session(s4="PUBLISHED")
    assert run(cfg, s) == 2 and writes(s) == [] and store(cfg).diagnostic_overrides() == []
    assert "--mark-published --job 4" in capsys.readouterr().err


def test_blocker_status_unreadable_refused(cfg, video):
    seed(cfg, video.parent)
    s = session(s3=FakeResponse(500, {"error": {"message": "unknown", "code": 1}}))
    assert run(cfg, s) == 2 and writes(s) == []


def test_expired_blocker_is_recorded_not_resolved(cfg, video):
    seed(cfg, video.parent)
    assert run(cfg, session(s3="EXPIRED")) == 0
    assert store(cfg).get("j3").state == "UNKNOWN"                     # evidence recorded, job NOT changed
    [ov] = store(cfg).diagnostic_overrides()
    assert ov["blockers"][0]["blocker_container_status"] == "EXPIRED"


def test_blocker_resolved_since_approval_needs_no_exception(cfg, video):
    seed(cfg, video.parent).update("j3", "FAILED", failure_phase="RECONCILED_NOT_PUBLISHED")
    assert run(cfg, session()) == 0
    [ov] = store(cfg).diagnostic_overrides()
    assert [b["blocker_job_id"] for b in ov["blockers"]] == [4]


# ------------------------------------------------------------------ ambiguous outcome
def test_ambiguous_publish_stays_unknown_and_never_retries(cfg, video):
    seed(cfg, video.parent)
    before = snapshot(cfg)
    s = session()
    s.routes.insert(0, ["POST", "/media_publish", [FakeResponse(400, REAL_2207085)]])
    assert run(cfg, s) == 3
    assert sum(c["url"].endswith("media_publish") for c in s.calls) == 1
    rec = new_job(cfg)
    assert rec.state == "UNKNOWN" and is_uncertain(rec) and rec.publish_attempts == 1
    assert snapshot(cfg) == before
    assert {b.id for b in store(cfg).account_blockers(PERSONAL_IG)} == {3, 4, rec.id}
    calls = store(cfg).api_calls(rec.job_key)
    assert calls[-1]["error_subcode"] == 2207085 and calls[-1]["fbtrace_id"] == "T"
    for argv in (ARGS, ("--account", "personal", "--public")):
        s2 = session()
        assert run(cfg, s2, argv=argv, answers=("post", "PUBLIC")) in (2, 3) and writes(s2) == []


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), ConnectionResetError("reset")])
def test_interrupted_publish_is_unknown(cfg, video, exc):
    import requests
    seed(cfg, video.parent)
    s = session()
    err = requests.ConnectionError("reset") if isinstance(exc, ConnectionResetError) else exc
    s.routes.insert(0, ["POST", "/media_publish", [err]])
    rc = run(cfg, s)
    assert rc in (3, 130)
    assert new_job(cfg).state == "UNKNOWN"


# ------------------------------------------------------------------ atomic claim in SQLite
def _armed(cfg, video):
    st = seed(cfg, video.parent)
    h = hashlib.sha256(video.read_bytes()).hexdigest()
    st.insert(job_key="new", account_id=PERSONAL_IG, source_path=str(video), relative_path=video.name,
              content_hash=h, size_bytes=1, graduation_strategy=None, publish_mode="PUBLIC")
    st.update("new", "READY")
    ov = st.record_diagnostic_override(kind="K", account_id=PERSONAL_IG, account_profile="personal",
                                       account_username="nashhawkins", content_hash=h, publish_mode="PUBLIC",
                                       upload_method="resumable", new_job_key="new", confirmation="x",
                                       blockers=[(st.get("j3"), "FINISHED"), (st.get("j4"), "FINISHED")])
    return st, ov, h


def test_claim_rechecks_late_blocker(cfg, video):
    st, ov, _ = _armed(cfg, video)
    st.insert(job_key="late", account_id=PERSONAL_IG, source_path="/v/o", relative_path="o", content_hash="o",
              size_bytes=1, graduation_strategy="MANUAL")
    st.update("late", "UNKNOWN")
    with pytest.raises(StateError):
        st.begin_diagnostic_publish("new", ov["id"])
    assert st.get("new").state == "READY" and st.get_diagnostic_override(ov["id"])["publish_claimed_at"] is None


def test_claim_rechecks_late_duplicate(cfg, video):
    st, ov, h = _armed(cfg, video)
    st.insert(job_key="dup", account_id=PERSONAL_IG, source_path="/v/c", relative_path="c", content_hash=h,
              size_bytes=1, graduation_strategy=None, publish_mode="PUBLIC")
    st.update("dup", "PUBLISHED")
    with pytest.raises(StateError):
        st.begin_diagnostic_publish("new", ov["id"])


def test_claim_once_and_only_for_bound_job(cfg, video):
    st, ov, _ = _armed(cfg, video)
    with pytest.raises(StateError):
        st.begin_diagnostic_publish("j3", ov["id"])
    assert st.begin_diagnostic_publish("new", ov["id"]).state == "PUBLISHING"
    st.update("new", "READY")
    with pytest.raises(StateError):
        st.begin_diagnostic_publish("new", ov["id"])
    with pytest.raises(StateError):
        st.begin_publish("new")                                         # the normal path still sees #3/#4


def test_kind_unique_in_db(cfg, video):
    st, ov, h = _armed(cfg, video)
    with pytest.raises(StateError, match="once only"):
        st.record_diagnostic_override(kind="K", account_id=PERSONAL_IG, account_profile="personal",
                                      account_username="n", content_hash=h, publish_mode="PUBLIC",
                                      upload_method="resumable", new_job_key="other", confirmation="x",
                                      blockers=[])
    assert len(st.diagnostic_overrides()) == 1


# ------------------------------------------------------------------ argument rules
@pytest.mark.parametrize("argv", [
    ("--account", "personal", "--diagnostic-public-override"),
    ("--public", "--diagnostic-public-override"),
    ARGS + ("--dry-run",), ARGS + ("--yes",), ARGS + ("--job", "3"),
    ARGS + ("--upload-method", "url", "--video-url", R2),
    ARGS + ("--override-unknown-job", "3"),
])
def test_argument_rules(argv):
    with pytest.raises(SystemExit):
        cli.parse_args(list(argv))


def test_publish_job_itself_keeps_duplicate_protection(cfg, video):
    """Middle layer: even when handed a valid diagnostic override, publish_job refuses a duplicate."""
    from trial_reels.errors import PublisherError
    from trial_reels.models import PUBLIC, RESUMABLE
    from trial_reels.publisher import build_job, publish_job
    from trial_reels.validation import validate_video
    st = seed(cfg, video.parent)
    job = build_job(cfg, video.resolve(), validate_video(video), PUBLIC, RESUMABLE)
    st.insert(job_key="dup", account_id=PERSONAL_IG, source_path="/v/c", relative_path="c", content_hash=job.sha256,
              size_bytes=1, graduation_strategy=None, publish_mode="PUBLIC")
    st.update("dup", "PUBLISHED")
    ov = st.record_diagnostic_override(kind="K", account_id=PERSONAL_IG, account_profile="personal",
                                       account_username="n", content_hash=job.sha256, publish_mode="PUBLIC",
                                       upload_method="resumable", new_job_key=job.job_key, confirmation="x",
                                       blockers=[(st.get("j3"), "FINISHED"), (st.get("j4"), "FINISHED")])
    s = session()
    client = MetaClient(cfg, session=s, sleep=lambda x: None)
    with pytest.raises(PublisherError, match="Refusing to post the same video"):
        publish_job(job, client=client, store=st, out=lambda m: None, relative_path=video.name,
                    remuxer=Remuxer(cfg.tmp_dir, ffmpeg=None), diagnostic=ov)
    assert s.calls == []
