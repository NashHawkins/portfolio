"""--diagnostic-trial-reattempt-override: IMG_2248 as ONE Trial Reel (MANUAL) on the personal account, despite
UNKNOWN job #5 and PUBLISHED-then-deleted job #7 of the same video. Offline only."""
import dataclasses
import json

import pytest

from conftest import FakeResponse
from test_diagnostic import PERSONAL_IG, REAL_2207085, TTY, run, store, writes
from test_reattempt import JOB_KEYS, key5, seed5, sess, sha
from trial_reels import diagnostic
from trial_reels.diagnostic import (APPROVED_TRIAL_REATTEMPT, TRIAL_REATTEMPT_CONFIRM_PHRASE,
                                    TRIAL_REATTEMPT_DUPLICATE_PHRASE, TRIAL_REATTEMPT_KIND)
from trial_reels.errors import PublisherError, StateError
from trial_reels.models import PUBLIC, RESUMABLE, TRIAL, make_job_key
from trial_reels.state import ACCEPTED_PUBLISHED_DUPLICATE, StateStore

from conftest import fake_mp4  # noqa: F401
import hashlib

pytestmark = pytest.mark.usefixtures("legacy_resumable_trial")  # see conftest

M7 = "18084194930405612"
ARGS = ("--account", "personal", "--diagnostic-trial-reattempt-override")
YES = ("nashhawkins", TRIAL_REATTEMPT_CONFIRM_PHRASE, TRIAL_REATTEMPT_DUPLICATE_PHRASE)
MANUAL = {"graduation_strategy": "MANUAL"}


@pytest.fixture(autouse=True)
def interactive(monkeypatch):
    monkeypatch.setattr("sys.stdin", TTY())


@pytest.fixture
def video(videos_dir, monkeypatch):
    p = videos_dir / "IMG_2248.MOV"
    p.write_bytes(fake_mp4(payload=b"img-2248" * 300))
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    for name in ("APPROVED_TRIAL_REATTEMPT", "APPROVED_REATTEMPT", "APPROVED_DIAGNOSTIC"):
        monkeypatch.setattr(diagnostic, name, dataclasses.replace(getattr(diagnostic, name), content_sha256=h))
    return p


@pytest.fixture
def cfg(make_config):
    return dataclasses.replace(make_config(IG_USER_ID=PERSONAL_IG), profile="personal")


def key7(video):
    return make_job_key(PERSONAL_IG, video.resolve(), sha(video), PUBLIC, RESUMABLE, attempt=2)


def seed7(cfg, video):
    """Real layout: #1–#6 as before, spent re-attempt override #2, #7 = attempt-2 PUBLIC PUBLISHED (M7)."""
    st = seed5(cfg, video)
    k7 = key7(video)
    st.record_diagnostic_override(
        kind=diagnostic.REATTEMPT_KIND, account_id=PERSONAL_IG, account_profile="personal",
        account_username="nashhawkins", content_hash=sha(video), publish_mode=PUBLIC, upload_method=RESUMABLE,
        new_job_key=k7, confirmation="x", blockers=[(st.get(k), "FINISHED") for k in ("j3", "j4", key5(video))],
        allowed_duplicate_job_key=key5(video))
    st.conn.execute("UPDATE diagnostic_overrides SET publish_claimed_at='2026-11-11T20:17:35+00:00'")
    st.insert(job_key=k7, account_id=PERSONAL_IG, source_path=str(video.resolve()), relative_path="IMG_2248.MOV",
              content_hash=sha(video), size_bytes=1, graduation_strategy=None, publish_mode=PUBLIC, attempt=2)
    st.update(k7, "PUBLISHED", container_id="18658069018044336", media_id=M7)
    assert st.get(k7).id == 7
    return st


def snap(cfg, video):
    st = StateStore(cfg.state_path)
    keys = (*JOB_KEYS, key5(video), "j6", key7(video))
    return [(st.get(k), st.events(k), st.api_calls(k)) for k in keys], st.overrides(), \
        st.diagnostic_overrides()[:2]


def new_job(cfg):
    return next(r for r in store(cfg).all() if r.id > 7)


def media_posts(s):
    return [c for c in writes(s) if c["url"].endswith("/media")]


# ------------------------------------------------------------------ approval pinned
def test_approval_is_pinned():
    a = APPROVED_TRIAL_REATTEMPT
    assert a.profile == "personal" and a.ig_user_id == PERSONAL_IG and a.mode == TRIAL and a.attempt == 1
    assert a.content_sha256 == "395f9eb51ddbedc7d6f49f7ed8404beb7ee07de1170637cd28fddcde6760ff7f"
    assert a.blocker_job_ids == frozenset({3, 4, 5}) and a.allowed_duplicate_job_id == 5
    assert (a.published_duplicate_job_id, a.published_duplicate_media_id) == (7, M7)
    assert a.kind == TRIAL_REATTEMPT_KIND not in (diagnostic.KIND, diagnostic.REATTEMPT_KIND)


# ------------------------------------------------------------------ success
def test_success_trial_manual_resumable_history_untouched(cfg, video, capsys):
    seed7(cfg, video)
    before = snap(cfg, video)
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=YES) == 0
    [m] = media_posts(s)
    assert {k: v for k, v in m["data"].items() if k != "trial_params"} == {"media_type": "REELS",
                                                                         "upload_type": "resumable"}
    assert json.loads(m["data"]["trial_params"]) == MANUAL
    assert [c["url"].rsplit("/", 1)[-1] for c in writes(s)][::2] == ["media", "media_publish"]
    assert sum(c["url"].endswith("media_publish") for c in s.calls) == 1
    assert snap(cfg, video) == before                                          # #1–#7 and overrides #1/#2

    rec = new_job(cfg)
    assert rec.id == 8 and rec.state == "PUBLISHED" and rec.publish_mode == TRIAL and rec.attempt == 1
    assert rec.graduation_strategy == "MANUAL" and rec.upload_method == RESUMABLE and rec.publish_attempts == 1
    ov = store(cfg).diagnostic_overrides()[2]
    assert ov["kind"] == TRIAL_REATTEMPT_KIND and ov["publish_mode"] == TRIAL and ov["new_job_key"] == rec.job_key
    assert ov["allowed_duplicate_job_key"] == key5(video) and ov["publish_claimed_at"]
    assert [(b["blocker_job_id"], b["blocker_container_status"]) for b in ov["blockers"]] == \
        [(3, "FINISHED"), (4, "FINISHED"), (5, "FINISHED"), (7, ACCEPTED_PUBLISHED_DUPLICATE)]
    out = capsys.readouterr().out
    assert "TRIAL Reel" in out and "Never falls back" in out and "job #7" in out


# ------------------------------------------------------------------ never a public fallback, never a retry
def test_trial_rejection_does_not_fall_back_to_public(cfg, video):
    seed7(cfg, video)
    s = sess()
    s.routes.insert(0, ["POST", f"/{PERSONAL_IG}/media", [FakeResponse(400, {"error": {
        "message": "does not meet the trial reel follower requirement", "code": 10, "error_subcode": 2207081}})]])
    assert run(cfg, s, argv=ARGS, answers=YES) != 0
    assert len(media_posts(s)) == 1 and "trial_params" in media_posts(s)[0]["data"]
    assert not any(c["url"].endswith("media_publish") for c in s.calls)
    assert new_job(cfg).state == "FAILED"
    s2 = sess()
    assert run(cfg, s2, argv=ARGS, answers=YES) != 0 and writes(s2) == []           # spent


def test_ambiguous_publish_unknown_no_retry(cfg, video):
    seed7(cfg, video)
    before = snap(cfg, video)
    s = sess()
    s.routes.insert(0, ["POST", "/media_publish", [FakeResponse(400, REAL_2207085)]])
    assert run(cfg, s, argv=ARGS, answers=YES) == 3
    assert sum(c["url"].endswith("media_publish") for c in s.calls) == 1
    assert new_job(cfg).state == "UNKNOWN" and snap(cfg, video) == before


# ------------------------------------------------------------------ single use, confirmations, flags
def test_single_use_and_earlier_overrides_stay_spent(cfg, video):
    seed7(cfg, video)
    assert run(cfg, sess(), argv=ARGS, answers=YES) == 0
    for argv in (ARGS, ("--account", "personal", "--public", "--diagnostic-reattempt-override"),
                 ("--account", "personal", "--public", "--diagnostic-public-override")):
        s = sess()
        assert run(cfg, s, argv=argv, answers=YES) != 0 and writes(s) == []
    assert len(store(cfg).diagnostic_overrides()) == 3


def test_normal_trial_path_still_blocked(cfg, video):
    seed7(cfg, video)
    s = sess()
    assert run(cfg, s, argv=("--account", "personal"), answers=("nashhawkins", "y")) != 0
    assert writes(s) == []


@pytest.mark.parametrize("answers", [
    ("example_account", TRIAL_REATTEMPT_CONFIRM_PHRASE, TRIAL_REATTEMPT_DUPLICATE_PHRASE),
    ("nashhawkins", diagnostic.REATTEMPT_CONFIRM_PHRASE, TRIAL_REATTEMPT_DUPLICATE_PHRASE),
    ("nashhawkins", TRIAL_REATTEMPT_CONFIRM_PHRASE, diagnostic.REATTEMPT_DUPLICATE_PHRASE),
    ("nashhawkins", TRIAL_REATTEMPT_CONFIRM_PHRASE, ""),
])
def test_each_confirmation_required(cfg, video, answers):
    seed7(cfg, video)
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=answers) == 1
    assert writes(s) == [] and len(store(cfg).diagnostic_overrides()) == 2


@pytest.mark.parametrize("extra", [("--public",), ("--yes",), ("--dry-run",), ("--job", "8"),
                                   ("--diagnostic-reattempt-override",)])
def test_cli_rejects_combinations(cfg, extra):
    with pytest.raises(SystemExit):
        run(cfg, sess(), argv=ARGS + extra, answers=YES)


# ------------------------------------------------------------------ refusals
def test_refused_unless_manual(make_config, video):
    cfg = dataclasses.replace(make_config(IG_USER_ID=PERSONAL_IG, TRIAL_GRADUATION_STRATEGY="SS_PERFORMANCE"),
                              profile="personal")
    seed7(cfg, video)
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=YES) != 0 and s.calls == []


@pytest.mark.parametrize("change", [{"state": "UNKNOWN"}, {"media_id": "999"},
                                    {"state": "FAILED", "failure_phase": "RECONCILED_NOT_PUBLISHED"}])
def test_refused_if_job7_is_not_the_approved_published_job(cfg, video, change):
    st = seed7(cfg, video)
    cols = ", ".join(f"{k} = ?" for k in change)
    st.conn.execute(f"UPDATE jobs SET {cols} WHERE id = 7", tuple(change.values()))
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=YES) != 0 and s.calls == []


def test_refused_with_another_published_copy(cfg, video):
    st = seed7(cfg, video)
    st.insert(job_key="jcopy", account_id=PERSONAL_IG, source_path="/v/copy.MOV", relative_path="copy.MOV",
              content_hash=sha(video), size_bytes=1, graduation_strategy=None, publish_mode=PUBLIC)
    st.update("jcopy", "PUBLISHED", media_id="mc")
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=YES) != 0 and s.calls == []


def test_refused_if_job5_resolved(cfg, video):
    st = seed7(cfg, video)
    st.update(key5(video), "PUBLISHED", media_id="m5")
    s = sess()
    assert run(cfg, s, argv=ARGS, answers=YES) != 0 and s.calls == []


def test_refused_when_job5_container_published(cfg, video):
    seed7(cfg, video)
    s = sess("PUBLISHED")
    assert run(cfg, s, argv=ARGS, answers=YES) != 0 and writes(s) == []


# ------------------------------------------------------------------ store-level guards
def _ready(cfg, video, status7=ACCEPTED_PUBLISHED_DUPLICATE):
    st = seed7(cfg, video)
    k = make_job_key(PERSONAL_IG, video.resolve(), sha(video), TRIAL, RESUMABLE)
    ov = st.record_diagnostic_override(
        kind=TRIAL_REATTEMPT_KIND, account_id=PERSONAL_IG, account_profile="personal", account_username="n",
        content_hash=sha(video), publish_mode=TRIAL, upload_method=RESUMABLE, new_job_key=k, confirmation="x",
        blockers=[(st.get(x), "FINISHED") for x in ("j3", "j4", key5(video))] + [(st.get(key7(video)), status7)],
        allowed_duplicate_job_key=key5(video))
    st.insert(job_key=k, account_id=PERSONAL_IG, source_path=str(video.resolve()), relative_path="IMG_2248.MOV",
              content_hash=sha(video), size_bytes=1, graduation_strategy="MANUAL")
    st.update(k, "READY")
    return st, k, ov


def test_store_excuses_marked_published_job(cfg, video):
    st, k, ov = _ready(cfg, video)
    assert st.begin_diagnostic_publish(k, ov["id"]).state == "PUBLISHING"


def test_store_does_not_excuse_unmarked_published_job(cfg, video):
    st, k, ov = _ready(cfg, video, status7="PUBLISHED")
    with pytest.raises(StateError):
        st.begin_diagnostic_publish(k, ov["id"])


def test_marker_only_for_published_jobs(cfg, video):
    st = seed7(cfg, video)
    with pytest.raises(StateError):
        st.record_diagnostic_override(
            kind=TRIAL_REATTEMPT_KIND, account_id=PERSONAL_IG, account_profile="personal", account_username="n",
            content_hash=sha(video), publish_mode=TRIAL, upload_method=RESUMABLE, new_job_key="k", confirmation="x",
            blockers=[(st.get(key5(video)), ACCEPTED_PUBLISHED_DUPLICATE)])


def test_publish_job_does_not_excuse_unmarked_published_job(cfg, video):
    from trial_reels.meta_client import MetaClient
    from trial_reels.publisher import build_job, publish_job
    from trial_reels.remux import Remuxer
    from trial_reels.validation import validate_video
    st = seed7(cfg, video)
    job = build_job(cfg, video.resolve(), validate_video(video), TRIAL, RESUMABLE)
    ov = st.record_diagnostic_override(
        kind=TRIAL_REATTEMPT_KIND, account_id=PERSONAL_IG, account_profile="personal", account_username="n",
        content_hash=sha(video), publish_mode=TRIAL, upload_method=RESUMABLE, new_job_key=job.job_key,
        confirmation="x", blockers=[(st.get(x), "FINISHED") for x in ("j3", "j4", key5(video), key7(video))],
        allowed_duplicate_job_key=key5(video))
    s = sess()
    with pytest.raises(PublisherError):
        publish_job(job, client=MetaClient(cfg, session=s, sleep=lambda x: None), store=st, out=lambda m: None,
                    relative_path="IMG_2248.MOV", remuxer=Remuxer(cfg.tmp_dir), diagnostic=ov)
    assert writes(s) == []


def test_store_refuses_if_marked_job_is_no_longer_published(cfg, video):
    st, k, ov = _ready(cfg, video)
    st.conn.execute("UPDATE jobs SET state = 'UNKNOWN' WHERE id = 7")
    with pytest.raises(StateError):
        st.begin_diagnostic_publish(k, ov["id"])


def test_publish_job_refuses_if_marked_job_is_no_longer_published(cfg, video):
    from trial_reels.meta_client import MetaClient
    from trial_reels.publisher import build_job, publish_job
    from trial_reels.remux import Remuxer
    from trial_reels.validation import validate_video
    st = seed7(cfg, video)
    job = build_job(cfg, video.resolve(), validate_video(video), TRIAL, RESUMABLE)
    ov = st.record_diagnostic_override(
        kind=TRIAL_REATTEMPT_KIND, account_id=PERSONAL_IG, account_profile="personal", account_username="n",
        content_hash=sha(video), publish_mode=TRIAL, upload_method=RESUMABLE, new_job_key=job.job_key,
        confirmation="x", blockers=[(st.get(x), "FINISHED") for x in ("j3", "j4", key5(video))]
        + [(st.get(key7(video)), ACCEPTED_PUBLISHED_DUPLICATE)], allowed_duplicate_job_key=key5(video))
    st.conn.execute("UPDATE jobs SET state = 'UNKNOWN' WHERE id = 7")
    s = sess()
    with pytest.raises(PublisherError):
        publish_job(job, client=MetaClient(cfg, session=s, sleep=lambda x: None), store=st, out=lambda m: None,
                    relative_path="IMG_2248.MOV", remuxer=Remuxer(cfg.tmp_dir), diagnostic=ov)
    assert writes(s) == []
