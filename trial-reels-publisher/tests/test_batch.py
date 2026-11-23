"""--batch DAY: post a day's LipSync videos from R2 as Trial Reels via video_url. Offline only."""
import dataclasses
import hashlib
import json
from datetime import date, datetime, timezone

import pytest

from conftest import FakeResponse, FakeSession, IG_ID, TOKEN, fake_mp4
from trial_reels import batch, cli
from trial_reels.errors import PublisherError
from trial_reels.hosted import HostedVideo, HostedVideoError
from trial_reels.meta_client import MetaClient
from trial_reels.state import StateStore
from trial_reels.validation import VideoReport

DAY = "2026-11-12"
BASE = "https://pub-x.r2.dev"
CAPTION = "This is a trial reel so thank goodness we met this song is a banger"
LIST_TOKEN = "LISTTOKEN-abcdef0123456789"
BYTES = {n: fake_mp4(payload=n.encode() * 500) for n in ("a", "b", "c")}


def vid(name, uploaded):
    key = f"lipsync/{DAY}/{name}.mp4"
    return batch.DayVideo(key=key, size=len(BYTES.get(name, b"")), uploaded=uploaded, url=f"{BASE}/{key}")


VIDEOS = [vid("a", "2026-11-12T15:00:00Z"), vid("b", "2026-11-12T16:00:00Z"), vid("c", "2026-11-12T17:00:00Z")]


class TTY:
    def __init__(self, tty=True):
        self.tty = tty

    def isatty(self):
        return self.tty


@pytest.fixture(autouse=True)
def interactive(monkeypatch):
    monkeypatch.setattr("sys.stdin", TTY())


@pytest.fixture
def cfg(make_config):
    return dataclasses.replace(make_config(DEFAULT_CAPTION=CAPTION), profile="personal",
                               lipsync_worker_url="https://w.example.workers.dev", lipsync_list_token=LIST_TOKEN)


def inspector(invalid=()):
    calls = []

    def insp(u, *, tmp_root, source_path, ffmpeg):
        calls.append(u)
        assert source_path is None and ffmpeg is None
        name = u.rsplit("/", 1)[-1][:-4]
        if name in invalid:
            raise HostedVideoError("the hosted MP4 has its 'moov' atom at the end.")
        data = BYTES[name]
        h = hashlib.sha256(data).hexdigest()
        return HostedVideo(url=u, host="pub-x.r2.dev", addresses=["104.18.2.35"], content_type="video/mp4",
                           content_length=len(data), sha256=h,
                           report=VideoReport(path=tmp_root, size_bytes=len(data), sha256=h))
    insp.calls = calls
    return insp


def meta(n=3, quota_used=2, container_fail=(), publish=None, username="nashhawkins"):
    """Container 90i / media 180i for the i-th POST /media."""
    s = FakeSession()
    s.on("GET", f"/{IG_ID}", FakeResponse(200, {"id": IG_ID, "username": username}))
    s.on("GET", "/me/permissions", FakeResponse(200, {"data": [
        {"permission": p, "status": "granted"} for p in
        ("instagram_basic", "instagram_content_publish", "pages_read_engagement", "pages_show_list")]}))
    s.on("GET", "/content_publishing_limit", FakeResponse(200, {"data": [
        {"quota_usage": quota_used, "config": {"quota_total": 100, "quota_duration": 86400}}]}))
    s.on("POST", f"/{IG_ID}/media", *[
        FakeResponse(400, {"error": {"message": "Invalid parameter", "type": "OAuthException", "code": 100}})
        if i in container_fail else FakeResponse(200, {"id": f"90{i}"}) for i in range(n)])
    s.on("GET", f"/{IG_ID}/media", FakeResponse(200, {"data": []}))
    for i in range(n):
        s.on("GET", f"/90{i}", FakeResponse(200, {"status_code": "FINISHED", "status": "FINISHED"}))
        s.on("GET", f"/180{i}", FakeResponse(200, {"id": f"180{i}", "permalink": f"https://www.instagram.com/reel/r{i}/",
                                                   "media_product_type": "REELS", "is_shared_to_feed": False}))
    s.on("POST", "/media_publish", *(publish or [FakeResponse(200, {"id": f"180{i}"}) for i in range(n)]))
    return s


def run(cfg, s, *argv, answers=("nashhawkins",), insp=None, videos=VIDEOS, now=None):
    it = iter(answers)
    lister_calls = []

    def lister(c, d):
        lister_calls.append(d)
        return list(videos)
    factory = lambda c: MetaClient(c, session=s, sleep=lambda x: None)  # noqa: E731
    rc = cli.main(["--account", "personal", "--batch", DAY, *argv], config=cfg, client_factory=factory,
                  input_fn=lambda p: next(it, ""), hosted_inspector=insp or inspector(),
                  batch_kwargs={"lister": lister, "now": now})
    run.lister_calls = lister_calls
    return rc


def creates(s):
    return [json.loads(c["data"]) for c in s.calls if c["method"] == "POST" and c["url"].endswith(f"/{IG_ID}/media")]


def publishes(s):
    return [c for c in s.calls if c["method"] == "POST" and c["url"].endswith("/media_publish")]


def jobs(cfg):
    return StateStore(cfg.state_path).all()


# ------------------------------------------------------------------ day resolution
@pytest.mark.parametrize("utc, expected", [
    ("2026-11-14T07:30:00", "2026-11-13"),   # 00:30 MST on the 14th
    ("2026-11-14T06:59:00", "2026-11-12"),   # 23:59 MST on the 13th
    ("2026-07-01T06:00:00", "2026-06-30"),   # 00:00 MDT on 1 July
    ("2026-07-01T05:59:00", "2026-06-29"),   # 23:59 MDT on 30 June
])
def test_yesterday_is_by_denver_calendar(utc, expected):
    now = datetime.fromisoformat(utc).replace(tzinfo=timezone.utc)
    assert batch.resolve_day("yesterday", "America/Denver", now).isoformat() == expected


def test_day_arguments():
    now = datetime(2026, 11, 14, 15, tzinfo=timezone.utc)
    assert batch.resolve_day("today", "America/Denver", now) == date(2026, 11, 14)
    assert batch.resolve_day("2026-02-28", "America/Denver", now) == date(2026, 2, 28)
    for bad in ("2026-02-30", "11/12/2026", "", "tomorrow", "2026-1-2"):
        with pytest.raises(PublisherError):
            batch.resolve_day(bad, "America/Denver", now)


# ------------------------------------------------------------------ dry run
def test_dry_run_lists_checks_and_writes_nothing(cfg, capsys):
    s = meta()
    insp = inspector()
    assert run(cfg, s, "--dry-run", answers=(), insp=insp) == 0
    assert [c for c in s.calls if c["method"] == "POST"] == []
    assert insp.calls == [v.url for v in VIDEOS]
    assert jobs(cfg) == []
    o = capsys.readouterr().out
    assert "3 in lipsync/2026-11-12/; 3 ready to post" in o
    assert CAPTION in o and "TRIAL / MANUAL via video_url" in o
    assert o.index("a.mp4") < o.index("b.mp4") < o.index("c.mp4")


def test_dry_run_reports_pause(cfg, capsys):
    st = StateStore(cfg.state_path)
    st.insert(job_key="unk", account_id=IG_ID, source_path="/v/x.MOV", relative_path="x.MOV", content_hash="x" * 64,
              size_bytes=1, graduation_strategy="MANUAL")
    st.update("unk", "UNKNOWN", container_id="c1")
    s = meta()
    assert run(cfg, s, "--dry-run", answers=()) == 2
    assert [c for c in s.calls if c["method"] == "POST"] == []
    assert "PAUSED" in capsys.readouterr().out


# ------------------------------------------------------------------ one at a time
def test_next_posts_one_video_at_a_time_in_upload_order(cfg, capsys):
    s = meta()
    assert run(cfg, s, "--next") == 0
    body = creates(s)
    assert body == [{"media_type": "REELS", "video_url": VIDEOS[0].url,
                     "trial_params": {"graduation_strategy": "MANUAL"}, "caption": CAPTION}]
    assert len(publishes(s)) == 1
    assert [(j.relative_path, j.state, j.publish_mode, j.upload_method) for j in jobs(cfg)] == [
        (VIDEOS[0].key, "PUBLISHED", "TRIAL", "url")]
    assert "1 still waiting" not in capsys.readouterr().out  # 2 still waiting
    assert run(cfg, s, "--next") == 0
    assert [b["video_url"] for b in creates(s)] == [VIDEOS[0].url, VIDEOS[1].url]
    assert run(cfg, s, "--next") == 0
    assert run(cfg, s, "--next", answers=()) == 0   # nothing left: no prompt, no post
    assert len(creates(s)) == 3 and len(publishes(s)) == 3
    assert "Nothing to post." in capsys.readouterr().out


def test_wrong_account_name_posts_nothing(cfg):
    s = meta()
    assert run(cfg, s, "--next", answers=("example_account",)) == 1
    assert run(cfg, s, "--all", answers=("",)) == 1
    assert creates(s) == [] and jobs(cfg) == []


# ------------------------------------------------------------------ whole day
def test_all_posts_everything_once(cfg, capsys):
    s = meta()
    assert run(cfg, s, "--all") == 0
    assert [b["video_url"] for b in creates(s)] == [v.url for v in VIDEOS]
    assert all(b["caption"] == CAPTION and b["trial_params"] == {"graduation_strategy": "MANUAL"} for b in creates(s))
    assert [j.media_id for j in jobs(cfg)] == ["1800", "1801", "1802"]
    assert "Done: 3 posted." in capsys.readouterr().out
    # Rerun: everything is POSTED; nothing is downloaded or sent again.
    insp = inspector()
    assert run(cfg, s, "--all", answers=(), insp=insp) == 0
    assert insp.calls == [] and len(creates(s)) == 3


def test_unattended_needs_no_terminal_or_prompt(cfg, monkeypatch):
    monkeypatch.setattr("sys.stdin", TTY(False))
    s = meta()
    assert run(cfg, s, "--all", answers=()) == 2            # without --unattended: refused, nothing posted
    assert creates(s) == []
    assert run(cfg, s, "--all", "--unattended", answers=()) == 0
    assert len(publishes(s)) == 3


def test_same_bytes_posted_before_is_a_duplicate(cfg, capsys):
    st = StateStore(cfg.state_path)
    st.insert(job_key="old", account_id=IG_ID, source_path="/v/old.mp4", relative_path="old.mp4",
              content_hash=hashlib.sha256(BYTES["b"]).hexdigest(), size_bytes=1, graduation_strategy="MANUAL")
    st.update("old", "PUBLISHED", media_id="m-old")
    s = meta()
    assert run(cfg, s, "--all") == 0
    assert [b["video_url"] for b in creates(s)] == [VIDEOS[0].url, VIDEOS[2].url]
    assert "DUPLICATE" in capsys.readouterr().out


def test_invalid_video_is_skipped_others_post(cfg, capsys):
    s = meta()
    assert run(cfg, s, "--all", insp=inspector(invalid=("a",))) == 0
    assert [b["video_url"] for b in creates(s)] == [VIDEOS[1].url, VIDEOS[2].url]
    assert "INVALID" in capsys.readouterr().out


def test_stops_at_first_failure(cfg, capsys):
    s = meta(container_fail=(0,))
    assert run(cfg, s, "--all") == 3
    assert len(creates(s)) == 1 and publishes(s) == []
    assert "Stopped after 0 of 3" in capsys.readouterr().out
    assert [j.state for j in jobs(cfg)] == ["FAILED"]


def test_unclear_publish_pauses_the_account(cfg, capsys):
    ambiguous = FakeResponse(400, {"error": {"message": "An internal server error occurred.", "type": "OAuthException",
                                             "code": -1, "error_subcode": 2207085, "fbtrace_id": "T"}})
    s = meta(publish=[ambiguous])
    assert run(cfg, s, "--all") == 3
    assert len(publishes(s)) == 1                      # never re-sent, next videos not attempted
    assert [j.state for j in jobs(cfg)] == ["UNKNOWN"]
    lister_before = len(run.lister_calls)
    # The next run refuses before listing or any network call.
    s2 = meta()
    assert run(cfg, s2, "--all", "--unattended", answers=()) == 2
    assert s2.calls == [] and run.lister_calls == []
    assert lister_before == 1


def test_quota_limits_the_batch(cfg, capsys):
    s = meta(quota_used=99)
    assert run(cfg, s, "--all") == 0
    assert len(publishes(s)) == 1
    assert "Only 1 post(s) left" in capsys.readouterr().out


# ------------------------------------------------------------------ argument rules
@pytest.mark.parametrize("argv", [
    ["--batch", DAY, "--next"],                                          # no --account
    ["--account", "personal", "--batch", DAY],                           # no action
    ["--account", "personal", "--batch", DAY, "--next", "--all"],
    ["--account", "personal", "--batch", DAY, "--next", "--unattended"],
    ["--account", "personal", "--batch", DAY, "--all", "--yes"],
    ["--account", "personal", "--batch", DAY, "--all", "--public"],
    ["--account", "personal", "--batch", DAY, "--all", "--test-mode"],
    ["--account", "personal", "--batch", DAY, "--all", "--upload-method", "url", "--video-url", "https://x/y.mp4"],
    ["--account", "personal", "--all"],                                  # --all without --batch
    ["--account", "personal", "--unattended"],
])
def test_bad_argument_combinations(argv):
    with pytest.raises(SystemExit):
        cli.parse_args(argv)


# ------------------------------------------------------------------ listing from the Worker
class ListSession:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return self.resp


def listing(*keys, day=DAY, base=BASE):
    return {"date": day, "videos": [{"key": k, "size": 5, "uploaded": f"2026-11-12T1{i}:00:00Z",
                                     "publicURL": f"{base}/{k}"} for i, k in enumerate(keys)]}


def test_list_day_calls_worker_with_token(cfg):
    sess = ListSession(FakeResponse(200, listing(f"lipsync/{DAY}/b.mp4", f"lipsync/{DAY}/a.mp4")))
    got = batch.list_day(cfg, date(2026, 11, 12), session=sess)
    assert [v.name for v in got] == ["b.mp4", "a.mp4"]  # by upload time, as listed
    url, params, headers = sess.calls[0]
    assert url == "https://w.example.workers.dev/list" and params == {"date": DAY}
    assert headers == {"Authorization": f"Bearer {LIST_TOKEN}"}


@pytest.mark.parametrize("body", [
    listing("lipsync/2026-11-13/a.mp4"),                         # another day's object
    listing(f"lipsync/{DAY}/a.mov"),                             # not an mp4
    {"date": DAY, "videos": [{"key": f"lipsync/{DAY}/a.mp4", "publicURL": f"{BASE}/lipsync/{DAY}/b.mp4"}]},
    {"date": "2026-11-13", "videos": []},                        # wrong day
    {"videos": "nope", "date": DAY},
])
def test_list_day_rejects_unexpected_listings(cfg, body):
    with pytest.raises(PublisherError):
        batch.list_day(cfg, date(2026, 11, 12), session=ListSession(FakeResponse(200, body)))


def test_list_day_errors_never_show_the_token(cfg):
    sess = ListSession(FakeResponse(401, {"error": f"Wrong token {LIST_TOKEN}"}))
    with pytest.raises(PublisherError) as e:
        batch.list_day(cfg, date(2026, 11, 12), session=sess)
    assert LIST_TOKEN not in str(e.value) and "401" in str(e.value)


def test_list_day_needs_settings(make_config):
    with pytest.raises(PublisherError, match="LIPSYNC_WORKER_URL"):
        batch.list_day(make_config(), date(2026, 11, 12), session=ListSession(None))


def test_config_reads_batch_settings_and_redacts_list_token(make_config):
    from trial_reels.errors import redact
    c = make_config(LIPSYNC_WORKER_URL="https://w.example.workers.dev", LIPSYNC_LIST_TOKEN=LIST_TOKEN,
                    DEFAULT_CAPTION=CAPTION)
    assert c.lipsync_worker_url == "https://w.example.workers.dev" and c.default_caption == CAPTION
    assert LIST_TOKEN not in repr(c) and redact(f"x {LIST_TOKEN} y") == "x [REDACTED] y"
