"""--test-mode: manual experiments that ignore earlier locks/duplicates but never rewrite history. Offline only."""
import dataclasses
import hashlib
import json

import pytest

from conftest import FakeResponse, IG_ID, fake_mp4, happy_session
from trial_reels import cli
from trial_reels.errors import StateError
from trial_reels.hosted import HostedVideo
from trial_reels.meta_client import MetaClient
from trial_reels.remux import Remuxer
from trial_reels.state import TEST_MODE_PREFIX, StateStore
from trial_reels.validation import VideoReport

URL = "https://pub-example.r2.dev/IMG_3813_cloud.mp4"
HOSTED = fake_mp4(payload=b"r2" * 2000)
H = hashlib.sha256(HOSTED).hexdigest()
PHRASE = {"TRIAL": "TEST TRIAL REEL, DUPLICATES POSSIBLE", "PUBLIC": "TEST PUBLIC REEL, DUPLICATES POSSIBLE"}
AMBIGUOUS = {"error": {"message": "An internal server error occurred.", "type": "OAuthException", "code": -1,
                       "error_subcode": 2207085, "is_transient": False, "fbtrace_id": "T"}}


class TTY:
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def interactive(monkeypatch):
    monkeypatch.setattr("sys.stdin", TTY())


@pytest.fixture
def cfg(make_config):
    return dataclasses.replace(make_config(), profile="personal")


def inspector():
    calls = []

    def insp(u, *, tmp_root, source_path, ffmpeg):
        calls.append((u, source_path))
        return HostedVideo(url=u, host="pub-x.r2.dev", addresses=["104.18.2.35"], content_type="video/mp4",
                           content_length=len(HOSTED), sha256=H,
                           report=VideoReport(path=tmp_root, size_bytes=len(HOSTED), sha256=H))
    insp.calls = calls
    return insp


def run(cfg, s, *argv, answers=None, insp=None, mode="TRIAL"):
    it = iter(answers if answers is not None else ("testacct", PHRASE[mode]))
    factory = lambda c: MetaClient(c, session=s, sleep=lambda x: None)  # noqa: E731
    return cli.main(["--account", "personal", *argv], config=cfg, client_factory=factory,
                    input_fn=lambda p: next(it, ""), remuxer_factory=lambda c: Remuxer(c.tmp_dir, ffmpeg=None),
                    hosted_inspector=insp or inspector())


URL_ARGS = ("--test-mode", "--upload-method", "url", "--video-url", URL)


def writes(s):
    return [c for c in s.calls if c["method"] == "POST"]


def store(cfg):
    return StateStore(cfg.state_path)


def seed_history(cfg):
    """An UNKNOWN job (account paused) and a PUBLISHED job of the SAME remote bytes."""
    st = store(cfg)
    st.insert(job_key="unk", account_id=IG_ID, source_path="/v/a.MOV", relative_path="a.MOV", content_hash="a" * 64,
              size_bytes=1, graduation_strategy="MANUAL")
    st.update("unk", "UNKNOWN", container_id="c-unk")
    st.insert(job_key="pub", account_id=IG_ID, source_path="/v/b.mp4", relative_path="b.mp4", content_hash=H,
              size_bytes=1, graduation_strategy=None, publish_mode="PUBLIC")
    st.update("pub", "PUBLISHED", media_id="m-pub")
    return [(st.get(k), st.events(k)) for k in ("unk", "pub")]


def history(cfg):
    st = store(cfg)
    return [(st.get(k), st.events(k)) for k in ("unk", "pub")]


def new_jobs(cfg):
    return [r for r in store(cfg).all() if r.job_key not in ("unk", "pub")]


# ------------------------------------------------------------------ URL trial: no local file, locks ignored
def test_url_trial_ignores_locks_and_keeps_history(cfg, capsys):
    before = seed_history(cfg)
    s, insp = happy_session(), inspector()
    assert run(cfg, s, *URL_ARGS, insp=insp) == 0
    assert insp.calls == [(URL, None)]                                         # no local file involved
    body = json.loads(writes(s)[0]["data"])
    assert body == {"media_type": "REELS", "video_url": URL, "trial_params": {"graduation_strategy": "MANUAL"}}
    assert sum(c["url"].endswith("media_publish") for c in s.calls) == 1
    assert history(cfg) == before                                              # nothing rewritten
    [job] = new_jobs(cfg)
    assert job.state == "PUBLISHED" and job.publish_mode == "TRIAL" and job.graduation_strategy == "MANUAL"
    assert job.content_hash == H and job.video_url == URL and job.upload_method == "url"
    assert job.container_id == "900" and job.media_id == "1800" and job.publish_started_at
    [ov] = [o for o in store(cfg).diagnostic_overrides() if o["kind"].startswith(TEST_MODE_PREFIX)]
    assert ov["new_job_key"] == job.job_key and ov["publish_claimed_at"]
    assert sorted(b["blocker_container_status"] for b in ov["blockers"]) == \
        ["IGNORED_IN_TEST_MODE:PUBLISHED", "IGNORED_IN_TEST_MODE:UNKNOWN"]
    details = " ".join(e["detail"] or "" for e in store(cfg).events(job.job_key))
    assert '"trial_params": {"graduation_strategy": "MANUAL"}' in details and "TEST MODE" in details
    out = capsys.readouterr().out
    assert "Duplicate risk: YES" in out and "@testacct" in out and "TRIAL requested" in out
    assert "Confirmed mode:  NOT VERIFIED" in out and "Trial Reel published" not in out


def test_each_test_is_a_new_attempt(cfg):
    assert run(cfg, happy_session(), *URL_ARGS) == 0
    assert run(cfg, happy_session(), *URL_ARGS) == 0
    jobs = new_jobs(cfg)
    assert [j.attempt for j in jobs] == [1, 2] and len({j.job_key for j in jobs}) == 2
    assert len([o for o in store(cfg).diagnostic_overrides() if o["kind"].startswith(TEST_MODE_PREFIX)]) == 2


def test_url_public_has_no_trial_params(cfg):
    s = happy_session()
    assert run(cfg, s, *URL_ARGS, "--public", mode="PUBLIC") == 0
    assert json.loads(writes(s)[0]["data"]) == {"media_type": "REELS", "video_url": URL}


# ------------------------------------------------------------------ ambiguous: UNKNOWN, no retry
def test_ambiguous_publish_is_unknown_not_retried(cfg, capsys):
    s = happy_session()
    s.routes.insert(0, ["POST", "/media_publish", [FakeResponse(400, AMBIGUOUS)]])
    assert run(cfg, s, *URL_ARGS) == 3
    assert sum(c["url"].endswith("media_publish") for c in s.calls) == 1
    [job] = new_jobs(cfg)
    assert job.state == "UNKNOWN" and job.publish_attempts == 1
    assert "UNKNOWN" in capsys.readouterr().out
    # production stays locked by it; another explicit test is still possible
    s2 = happy_session()
    assert run(cfg, s2, "--upload-method", "url", "--video-url", URL, answers=("testacct",)) != 0
    assert writes(s2) == []
    assert run(cfg, happy_session(), *URL_ARGS) == 0 and len(new_jobs(cfg)) == 2


# ------------------------------------------------------------------ resumable PUBLIC still works
def test_resumable_public_test_mode(cfg, videos_dir):
    (videos_dir / "IMG_1.MOV").write_bytes(fake_mp4())
    seed_history(cfg)
    s = happy_session()
    assert run(cfg, s, "--test-mode", "--public", mode="PUBLIC") == 0
    assert writes(s)[0]["data"] == {"media_type": "REELS", "upload_type": "resumable"}
    [job] = new_jobs(cfg)
    assert job.state == "PUBLISHED" and job.upload_method == "resumable" and job.publish_mode == "PUBLIC"


# ------------------------------------------------------------------ confirmation and refusals
@pytest.mark.parametrize("answers", [("other", PHRASE["TRIAL"]), ("testacct", "yes"), ("testacct", PHRASE["PUBLIC"])])
def test_confirmations_required(cfg, answers):
    seed_history(cfg)
    s = happy_session()
    assert run(cfg, s, *URL_ARGS, answers=answers) == 1
    assert writes(s) == [] and new_jobs(cfg) == []
    assert not [o for o in store(cfg).diagnostic_overrides() if o["kind"].startswith(TEST_MODE_PREFIX)]


def test_never_unattended(cfg, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("N", (), {"isatty": lambda self: False})())
    s, insp = happy_session(), inspector()
    assert run(cfg, s, *URL_ARGS, insp=insp) != 0
    assert s.calls == [] and insp.calls == []


@pytest.mark.parametrize("argv", [
    URL_ARGS + ("--yes",), URL_ARGS + ("--dry-run",), URL_ARGS + ("--status",),
    ("--test-mode",),                                     # TRIAL needs url: never resumable, never public fallback
])
def test_cli_refusals(cfg, argv):
    with pytest.raises(SystemExit):
        run(cfg, happy_session(), *argv)


def test_production_url_path_unchanged(cfg, videos_dir):
    seed_history(cfg)
    (videos_dir / "IMG_1.MOV").write_bytes(fake_mp4())
    s = happy_session()
    assert run(cfg, s, "--upload-method", "url", "--video-url", URL, answers=("testacct",)) != 0
    assert writes(s) == []


# ------------------------------------------------------------------ store guard
def test_begin_test_publish_requires_its_own_unclaimed_row(cfg):
    st = store(cfg)
    st.insert(job_key="t", account_id=IG_ID, source_path="/r/x", relative_path="x", content_hash=H, size_bytes=1,
              graduation_strategy="MANUAL", upload_method="url", video_url=URL)
    st.update("t", "READY")
    wrong = st.record_diagnostic_override(kind="SOMETHING_ELSE", account_id=IG_ID, account_profile="personal",
                                          account_username="a", content_hash=H, publish_mode="TRIAL",
                                          upload_method="url", new_job_key="t", confirmation="x", blockers=[])
    with pytest.raises(StateError):
        st.begin_test_publish("t", wrong["id"])
    st.conn.execute("DELETE FROM diagnostic_overrides")
    ov = st.record_diagnostic_override(kind=TEST_MODE_PREFIX + "t", account_id=IG_ID, account_profile="personal",
                                       account_username="a", content_hash=H, publish_mode="TRIAL",
                                       upload_method="url", new_job_key="t", confirmation="x", blockers=[])
    assert st.begin_test_publish("t", ov["id"]).state == "PUBLISHING"
    with pytest.raises(StateError):
        st.begin_test_publish("t", ov["id"])                                   # single use


def test_duplicate_risk_also_matches_uploaded_bytes(cfg, capsys):
    st = store(cfg)                       # like job #4: keyed on the local original, uploaded the R2 bytes
    st.insert(job_key="j4", account_id=IG_ID, source_path="/v/IMG_3813.MOV", relative_path="IMG_3813.MOV",
              content_hash="b" * 64, size_bytes=1, graduation_strategy="MANUAL", upload_method="url", video_url=URL)
    st.update("j4", "UNKNOWN", upload_sha256=H)
    assert run(cfg, happy_session(), *URL_ARGS) == 0
    out = capsys.readouterr().out
    assert "Duplicate risk: YES" in out and "(same video)" in out
