"""--diagnostic-reattempt-override: ONE normal PUBLIC Reel re-attempting UNKNOWN job #5's own video on the
personal account, while jobs #3/#4/#5 stay UNKNOWN. Offline only."""
import dataclasses
import hashlib

import pytest

from conftest import FakeResponse
from test_diagnostic import (C3, C4, PERSONAL_IG, REAL_2207085, TEST_IG, TTY, run, seed, session, store,  # noqa: F401
                             writes)
from trial_reels import diagnostic
from trial_reels.diagnostic import (APPROVED_REATTEMPT, REATTEMPT_CONFIRM_PHRASE, REATTEMPT_DUPLICATE_PHRASE,
                                    REATTEMPT_KIND)
from trial_reels.errors import PublisherError, StateError
from trial_reels.models import PUBLIC, RESUMABLE, make_job_key
from trial_reels.state import StateStore

from conftest import fake_mp4

C5 = "18658013635044336"
ARGS = ("--account", "personal", "--public", "--diagnostic-reattempt-override")
YES = ("nashhawkins", REATTEMPT_CONFIRM_PHRASE, REATTEMPT_DUPLICATE_PHRASE)
JOB_KEYS = ("j1", "j2", "j3", "j4")


@pytest.fixture(autouse=True)
def interactive(monkeypatch):
    monkeypatch.setattr("sys.stdin", TTY())


@pytest.fixture
def video(videos_dir, monkeypatch):
    p = videos_dir / "IMG_2248.MOV"
    p.write_bytes(fake_mp4(payload=b"img-2248" * 300))
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    monkeypatch.setattr(diagnostic, "APPROVED_REATTEMPT", dataclasses.replace(APPROVED_REATTEMPT, content_sha256=sha))
    monkeypatch.setattr(diagnostic, "APPROVED_DIAGNOSTIC",
                        dataclasses.replace(diagnostic.APPROVED_DIAGNOSTIC, content_sha256=sha))
    return p


@pytest.fixture
def cfg(make_config):
    return dataclasses.replace(make_config(IG_USER_ID=PERSONAL_IG), profile="personal")


def key5(video):
    return make_job_key(PERSONAL_IG, video.resolve(), sha(video), PUBLIC, RESUMABLE)


def sha(video):
    return hashlib.sha256(video.read_bytes()).hexdigest()


def seed5(cfg, video):
    """Real layout: #1–#4 as before, #5 = PUBLIC resumable IMG_2248 UNKNOWN under the spent diagnostic #1,
    #6 = the same video PUBLISHED on the TEST account."""
    st = seed(cfg, video.parent)
    k5 = key5(video)
    st.insert(job_key=k5, account_id=PERSONAL_IG, source_path=str(video.resolve()), relative_path="IMG_2248.MOV",
              content_hash=sha(video), size_bytes=video.stat().st_size, graduation_strategy=None,
              publish_mode=PUBLIC, account_profile="personal", account_username="nashhawkins")
    st.update(k5, "CONTAINER_CREATED", container_id=C5)
    st.update(k5, "UNKNOWN", last_error="code=-1 subcode=2207085")
    st.conn.execute("UPDATE jobs SET publish_attempts=1, publish_started_at='2026-11-11T16:40:10+00:00' "
                    "WHERE job_key=?", (k5,))
    st.record_diagnostic_override(
        kind=diagnostic.KIND, account_id=PERSONAL_IG, account_profile="personal", account_username="nashhawkins",
        content_hash=sha(video), publish_mode=PUBLIC, upload_method=RESUMABLE, new_job_key=k5, confirmation="x",
        blockers=[(st.get("j3"), "FINISHED"), (st.get("j4"), "FINISHED")])
    st.conn.execute("UPDATE diagnostic_overrides SET publish_claimed_at='2026-11-11T16:40:10+00:00'")
    st.insert(job_key="j6", account_id=TEST_IG, source_path=str(video.resolve()), relative_path="IMG_2248.MOV",
              content_hash=sha(video), size_bytes=1, graduation_strategy=None, publish_mode=PUBLIC)
    st.update("j6", "PUBLISHED", media_id="18118989991819535")
    assert [st.get(k).id for k in (k5, "j6")] == [5, 6]
    return st


def snap(cfg, video):
    st = StateStore(cfg.state_path)
    keys = (*JOB_KEYS, key5(video), "j6")
    return [(st.get(k), st.events(k), st.api_calls(k)) for k in keys], st.overrides()


def sess(s5="FINISHED"):
    s = session()
    s.on("GET", f"/{C5}", s5 if isinstance(s5, FakeResponse) else FakeResponse(200, {"status_code": s5, "status": s5}))
    return s


def new_job(cfg):
    return next(r for r in store(cfg).all() if r.id > 6)


# ------------------------------------------------------------------ approval pinned
def test_approval_is_pinned_to_job5_video_on_personal():
    a = APPROVED_REATTEMPT
    assert a.profile == "personal" and a.ig_user_id == PERSONAL_IG
    assert a.content_sha256 == "395f9eb51ddbedc7d6f49f7ed8404beb7ee07de1170637cd28fddcde6760ff7f"
    assert a.blocker_job_ids == frozenset({3, 4, 5}) and a.allowed_duplicate_job_id == 5 and a.attempt == 2
    assert a.kind == REATTEMPT_KIND != diagnostic.KIND


def test_attempt_one_keys_are_unchanged(tmp_path):
    p = tmp_path / "x.MOV"
    k = make_job_key(PERSONAL_IG, p, "h", PUBLIC, RESUMABLE)
    assert k == make_job_key(PERSONAL_IG, p, "h", PUBLIC, RESUMABLE, attempt=1)
    assert make_job_key(PERSONAL_IG, p, "h", PUBLIC, RESUMABLE, attempt=2) not in (k,)
    with pytest.raises(ValueError):
        make_job_key(PERSONAL_IG, p, "h", PUBLIC, RESUMABLE, attempt=0)


# ------------------------------------------------------------------ success
def test_success_public_resumable_jobs_3_4_5_untouched(cfg, video, capsys):
    seed5(cfg, video)
    before = snap(cfg, video)
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=YES) == 0
    assert writes(s)[0]["data"] == {"media_type": "REELS", "upload_type": "resumable"}      # no trial_params
    assert [c["url"].rsplit("/", 1)[-1] for c in writes(s)] == ["media", UPLOAD_TAIL(s), "media_publish"]
    assert sum(c["url"].endswith("media_publish") for c in s.calls) == 1
    assert snap(cfg, video) == before                                # #1–#6, events, api_calls, trial override

    rec = new_job(cfg)
    assert rec.id == 7 and rec.state == "PUBLISHED" and rec.attempt == 2 and rec.publish_mode == PUBLIC
    assert rec.upload_method == RESUMABLE and rec.content_hash == sha(video) and rec.publish_attempts == 1
    assert rec.job_key == make_job_key(PERSONAL_IG, video.resolve(), sha(video), PUBLIC, RESUMABLE, attempt=2)
    first, ov = store(cfg).diagnostic_overrides()
    assert first["kind"] == diagnostic.KIND and first["new_job_key"] == key5(video)   # old override unchanged
    assert ov["kind"] == REATTEMPT_KIND and ov["new_job_key"] == rec.job_key and ov["publish_claimed_at"]
    assert ov["allowed_duplicate_job_key"] == key5(video)
    assert REATTEMPT_DUPLICATE_PHRASE in ov["confirmation"] and REATTEMPT_CONFIRM_PHRASE in ov["confirmation"]
    assert [(b["blocker_job_id"], b["blocker_container_status"]) for b in ov["blockers"]] == \
        [(3, "FINISHED"), (4, "FINISHED"), (5, "FINISHED")]
    assert "DUPLICATE RISK" in capsys.readouterr().out


def UPLOAD_TAIL(s):
    return writes(s)[1]["url"].rsplit("/", 1)[-1]


def test_single_use(cfg, video):
    seed5(cfg, video)
    assert run(cfg, sess(), argv=ARGS, answers=YES) == 0
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=YES) != 0
    assert writes(s) == [] and len(store(cfg).diagnostic_overrides()) == 2


def test_previous_diagnostic_cannot_be_reused(cfg, video):
    seed5(cfg, video)
    s = sess()
    assert run(cfg, s, argv=("--account", "personal", "--public", "--diagnostic-public-override"),
               answers=("nashhawkins", diagnostic.CONFIRM_PHRASE)) != 0
    assert writes(s) == [] and len(store(cfg).diagnostic_overrides()) == 1


def test_normal_public_path_still_blocked(cfg, video):
    seed5(cfg, video)
    before = snap(cfg, video)
    s = sess()
    assert run(cfg, s, argv=("--account", "personal", "--public"), answers=("nashhawkins", "PUBLIC", "y")) != 0
    assert writes(s) == [] and snap(cfg, video) == before


# ------------------------------------------------------------------ confirmations
@pytest.mark.parametrize("answers", [
    ("example_account", REATTEMPT_CONFIRM_PHRASE, REATTEMPT_DUPLICATE_PHRASE),
    ("nashhawkins", diagnostic.CONFIRM_PHRASE, REATTEMPT_DUPLICATE_PHRASE),
    ("nashhawkins", REATTEMPT_CONFIRM_PHRASE, ""),
    ("nashhawkins", REATTEMPT_CONFIRM_PHRASE, "I ACCEPT A POSSIBLE DUPLICATE"),
])
def test_each_confirmation_required(cfg, video, answers):
    seed5(cfg, video)
    before = snap(cfg, video)
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=answers) == 1
    assert writes(s) == [] and len(store(cfg).diagnostic_overrides()) == 1 and snap(cfg, video) == before


def test_non_interactive_refused(cfg, video, monkeypatch):
    seed5(cfg, video)
    monkeypatch.setattr("sys.stdin", type("N", (), {"isatty": lambda self: False})())
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=YES) != 0
    assert s.calls == []


# ------------------------------------------------------------------ refusals keep every other guard
def test_refused_when_job5_container_published(cfg, video):
    seed5(cfg, video)
    s = sess("PUBLISHED")
    assert run(cfg, s, argv=ARGS, answers=YES) != 0
    assert writes(s) == [] and len(store(cfg).diagnostic_overrides()) == 1


def test_refused_when_job5_resolved_published(cfg, video):
    st = seed5(cfg, video)
    st.update(key5(video), "PUBLISHED", media_id="m5")
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=YES) != 0
    assert s.calls == []


def test_refused_with_other_unknown_job(cfg, video):
    st = seed5(cfg, video)
    st.insert(job_key="j8", account_id=PERSONAL_IG, source_path="/v/other.MOV", relative_path="other.MOV",
              content_hash="o" * 64, size_bytes=1, graduation_strategy=None, publish_mode=PUBLIC)
    st.update("j8", "UNKNOWN")
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=YES) != 0
    assert s.calls == []


def test_refused_when_video_already_published_elsewhere_on_account(cfg, video):
    st = seed5(cfg, video)
    st.insert(job_key="jcopy", account_id=PERSONAL_IG, source_path="/v/copy.MOV", relative_path="copy.MOV",
              content_hash=sha(video), size_bytes=1, graduation_strategy=None, publish_mode=PUBLIC)
    st.update("jcopy", "PUBLISHED", media_id="mc")
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=YES) != 0
    assert s.calls == []


def test_refused_for_other_video(cfg, video, monkeypatch):
    seed5(cfg, video)
    monkeypatch.setattr(diagnostic, "APPROVED_REATTEMPT", dataclasses.replace(APPROVED_REATTEMPT))
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=YES) != 0
    assert s.calls == []


def test_refused_on_test_account(make_config, video):
    cfg = dataclasses.replace(make_config(IG_USER_ID=TEST_IG), profile="test")
    s = sess()
    assert run(cfg, s, argv=("--account", "test", "--public", "--diagnostic-reattempt-override"), answers=YES) != 0
    assert s.calls == []


# ------------------------------------------------------------------ ambiguous publish: no retry
def test_ambiguous_publish_leaves_new_job_unknown_no_retry(cfg, video):
    seed5(cfg, video)
    before = snap(cfg, video)
    s = sess()
    s.routes.insert(0, ["POST", "/media_publish", [FakeResponse(400, REAL_2207085)]])
    assert run(cfg, s, argv=ARGS, answers=YES) == 3
    assert sum(c["url"].endswith("media_publish") for c in s.calls) == 1
    assert snap(cfg, video) == before
    rec = new_job(cfg)
    assert rec.state == "UNKNOWN" and rec.publish_attempts == 1 and rec.attempt == 2
    s2 = sess()
    assert run(cfg, s2, argv=ARGS, answers=YES) != 0 and writes(s2) == []


# ------------------------------------------------------------------ store-level guards
def _ready_reattempt(cfg, video, allowed=True):
    st = seed5(cfg, video)
    k = make_job_key(PERSONAL_IG, video.resolve(), sha(video), PUBLIC, RESUMABLE, attempt=2)
    ov = st.record_diagnostic_override(
        kind=REATTEMPT_KIND, account_id=PERSONAL_IG, account_profile="personal", account_username="nashhawkins",
        content_hash=sha(video), publish_mode=PUBLIC, upload_method=RESUMABLE, new_job_key=k, confirmation="x",
        blockers=[(st.get(x), "FINISHED") for x in ("j3", "j4", key5(video))],
        allowed_duplicate_job_key=key5(video) if allowed else None)
    st.insert(job_key=k, account_id=PERSONAL_IG, source_path=str(video.resolve()), relative_path="IMG_2248.MOV",
              content_hash=sha(video), size_bytes=1, graduation_strategy=None, publish_mode=PUBLIC, attempt=2)
    st.update(k, "READY")
    return st, k, ov


def test_store_allows_only_the_approved_unknown_duplicate(cfg, video):
    st, k, ov = _ready_reattempt(cfg, video)
    assert st.begin_diagnostic_publish(k, ov["id"]).state == "PUBLISHING"


def test_store_refuses_without_allowed_duplicate(cfg, video):
    st, k, ov = _ready_reattempt(cfg, video, allowed=False)      # NULL must not open any exception
    with pytest.raises(StateError):
        st.begin_diagnostic_publish(k, ov["id"])


def test_store_refuses_if_allowed_duplicate_became_published(cfg, video):
    st, k, ov = _ready_reattempt(cfg, video)
    st.update(key5(video), "PUBLISHED", media_id="m5")
    with pytest.raises(StateError):
        st.begin_diagnostic_publish(k, ov["id"])
    assert st.get(k).state == "READY" and st.get_diagnostic_override(ov["id"])["publish_claimed_at"] is None


def test_store_refuses_second_duplicate(cfg, video):
    st, k, ov = _ready_reattempt(cfg, video)
    st.insert(job_key="jcopy", account_id=PERSONAL_IG, source_path="/v/copy.MOV", relative_path="copy.MOV",
              content_hash=sha(video), size_bytes=1, graduation_strategy=None, publish_mode=PUBLIC)
    st.update("jcopy", "PUBLISHED", media_id="mc")
    with pytest.raises(StateError):
        st.begin_diagnostic_publish(k, ov["id"])


def test_allowed_duplicate_must_be_a_blocker(cfg, video):
    st = seed5(cfg, video)
    with pytest.raises(StateError):
        st.record_diagnostic_override(
            kind=REATTEMPT_KIND, account_id=PERSONAL_IG, account_profile="personal", account_username="n",
            content_hash=sha(video), publish_mode=PUBLIC, upload_method=RESUMABLE, new_job_key="k", confirmation="x",
            blockers=[(st.get("j3"), "FINISHED")], allowed_duplicate_job_key=key5(video))
    assert len(st.diagnostic_overrides()) == 1


def test_publish_job_refuses_allowed_duplicate_not_unknown(cfg, video):
    from trial_reels.meta_client import MetaClient
    from trial_reels.publisher import build_job, publish_job
    from trial_reels.remux import Remuxer
    from trial_reels.validation import validate_video
    st = seed5(cfg, video)
    job = dataclasses.replace(build_job(cfg, video.resolve(), validate_video(video), PUBLIC, RESUMABLE),
                              attempt=2)
    ov = st.record_diagnostic_override(
        kind=REATTEMPT_KIND, account_id=PERSONAL_IG, account_profile="personal", account_username="nashhawkins",
        content_hash=sha(video), publish_mode=PUBLIC, upload_method=RESUMABLE, new_job_key=job.job_key,
        confirmation="x", blockers=[(st.get(x), "FINISHED") for x in ("j3", "j4", key5(video))],
        allowed_duplicate_job_key=key5(video))
    st.update(key5(video), "PUBLISHED", media_id="m5")
    s = sess()
    with pytest.raises(PublisherError):
        publish_job(job, client=MetaClient(cfg, session=s, sleep=lambda x: None), store=st, out=lambda m: None,
                    relative_path="IMG_2248.MOV", remuxer=Remuxer(cfg.tmp_dir), diagnostic=ov)
    assert writes(s) == []


# ------------------------------------------------------------------ CLI flags
@pytest.mark.parametrize("extra", [("--yes",), ("--dry-run",), ("--job", "5"), ("--diagnostic-public-override",)])
def test_cli_rejects_combinations(cfg, extra):
    with pytest.raises(SystemExit):
        run(cfg, sess(), argv=ARGS + extra, answers=YES)


def test_cli_requires_public(cfg):
    with pytest.raises(SystemExit):
        run(cfg, sess(), argv=("--account", "personal", "--diagnostic-reattempt-override"), answers=YES)


def test_publish_job_refuses_allowed_duplicate_outside_blockers(cfg, video):
    from trial_reels.meta_client import MetaClient
    from trial_reels.publisher import build_job, publish_job
    from trial_reels.remux import Remuxer
    from trial_reels.validation import validate_video
    st = seed5(cfg, video)
    job = dataclasses.replace(build_job(cfg, video.resolve(), validate_video(video), PUBLIC, RESUMABLE), attempt=2)
    ov = st.record_diagnostic_override(
        kind=REATTEMPT_KIND, account_id=PERSONAL_IG, account_profile="personal", account_username="nashhawkins",
        content_hash=sha(video), publish_mode=PUBLIC, upload_method=RESUMABLE, new_job_key=job.job_key,
        confirmation="x", blockers=[(st.get(x), "FINISHED") for x in ("j3", "j4", key5(video))],
        allowed_duplicate_job_key=key5(video))
    ov = dict(ov, blocker_job_keys=["j3", "j4"])      # a tampered row: allowed key not among the blockers
    s = sess()
    with pytest.raises(PublisherError):
        publish_job(job, client=MetaClient(cfg, session=s, sleep=lambda x: None), store=st, out=lambda m: None,
                    relative_path="IMG_2248.MOV", remuxer=Remuxer(cfg.tmp_dir), diagnostic=ov)
    assert writes(s) == []
