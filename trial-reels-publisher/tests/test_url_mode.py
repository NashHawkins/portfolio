"""--upload-method url: hosted-video validation, SSRF guard, request shape, locks, duplicates, outcomes."""
import dataclasses
import hashlib
import json
import socket

import pytest

from conftest import IG_ID, TOKEN, FakeResponse, fake_mp4, happy_session
from trial_reels import cli
from trial_reels.errors import MetaApiError, TrialRejectedError
from trial_reels.hosted import (HostedVideo, HostedVideoError, anonymous_session, check_url_shape,
                                inspect_hosted_video, resolve_public, video_stream_sha256)
from trial_reels.meta_client import MetaClient, url_reel_container_body
from trial_reels.models import make_job_key
from trial_reels.remux import Remuxer
from trial_reels.state import StateStore, is_uncertain
from trial_reels.validation import VideoReport

pytestmark = pytest.mark.usefixtures("legacy_resumable_trial")  # see conftest

URL = "https://pub-example.r2.dev/IMG_3813_cloud.mp4"
PUBLIC_IP = "104.18.2.35"
HOSTED = fake_mp4(payload=b"h" * 3000)
REAL_2207085 = {"error": {"message": "An internal server error occurred. Please try again later.",
                          "type": "OAuthException", "code": -1, "error_subcode": 2207085,
                          "is_transient": False, "fbtrace_id": "T"}}


# ------------------------------------------------------------------ fakes
def resolver_for(*ips):
    def r(host, port, proto=None):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in ips]
    return r


class HostResp:
    def __init__(self, status=200, body=HOSTED, headers=None):
        self.status_code = status
        self.body = body
        self.headers = {"Content-Type": "video/mp4", "Content-Length": str(len(body))}
        self.headers.update(headers or {})
        self.headers = {k: v for k, v in self.headers.items() if v is not None}
        self.closed = False

    def iter_content(self, n):
        for i in range(0, len(self.body), 7):
            yield self.body[i:i + 7]

    def close(self):
        self.closed = True


class HostSession:
    def __init__(self, head=None, get=None):
        self.resp = {"HEAD": head or HostResp(), "GET": get or HostResp()}
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.resp[method]


def inspect(tmp_path, session=None, resolver=resolver_for(PUBLIC_IP), **kw):
    return inspect_hosted_video(URL, tmp_root=tmp_path / "tmp", session=session or HostSession(),
                                resolver=resolver, **kw)


# ------------------------------------------------------------------ URL shape / SSRF
@pytest.mark.parametrize("url,why", [
    ("", "empty"), ("http://x.r2.dev/a.mp4", "https"), ("ftp://x.r2.dev/a.mp4", "https"),
    ("https://user:pw@x.r2.dev/a.mp4", "username"), ("https://x.r2.dev:8443/a.mp4", "port"),
    ("https://x.r2.dev/a.mp4?X-Amz-Signature=abc", "query"), ("https://x.r2.dev/a.mp4#t", "query"),
    ("https://127.0.0.1/a.mp4", "IP address"), ("https://[::1]/a.mp4", "IP address"),
    ("https://localhost/a.mp4", "fully qualified"), ("https://nas.local/a.mp4", "not a public"),
    ("https://metadata.internal/a.mp4", "not a public"), ("https://x.r2.dev/a.mov", ".mp4"),
    ("https://x.r2.dev/a.mp4 ", "whitespace"), ("not a url", "whitespace"), ("notaurl", "https"),
])
def test_invalid_urls_rejected(url, why):
    with pytest.raises(HostedVideoError, match=why):
        check_url_shape(url)


def test_real_url_shape_ok():
    assert check_url_shape(URL)[1] == "pub-example.r2.dev"


@pytest.mark.parametrize("ips", [("127.0.0.1",), ("10.0.0.5",), ("192.168.1.10",), ("169.254.169.254",),
                                 ("::1",), ("fd00::1",), ("::ffff:127.0.0.1",), ("100.64.0.1",),
                                 ("0.0.0.0",), (PUBLIC_IP, "10.0.0.5")])
def test_ssrf_private_addresses_refused(ips, tmp_path):
    with pytest.raises(HostedVideoError, match="non-public"):
        resolve_public("x.r2.dev", resolver_for(*ips))
    s = HostSession()
    with pytest.raises(HostedVideoError):
        inspect(tmp_path, s, resolver_for(*ips))
    assert s.calls == []  # nothing fetched


def test_dns_rebinding_between_head_and_get_refused(tmp_path):
    answers = iter([[PUBLIC_IP], ["10.0.0.5"]])

    def flip(host, port, proto=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in next(answers)]
    s = HostSession()
    with pytest.raises(HostedVideoError, match="non-public"):
        inspect(tmp_path, s, flip)
    assert [c[0] for c in s.calls] == ["HEAD"]


def test_unresolvable_host(tmp_path):
    def fail(*a, **k):
        raise socket.gaierror("nodename nor servname provided")
    with pytest.raises(HostedVideoError, match="Cannot resolve"):
        inspect(tmp_path, resolver=fail)


# ------------------------------------------------------------------ HTTP / bytes
def test_valid_hosted_video(tmp_path):
    s = HostSession()
    hv = inspect(tmp_path, s, source_path=tmp_path / "orig.mov", ffmpeg="/bin/ffmpeg",
                 hasher=lambda p, f: "ab" * 32)
    assert hv.content_type == "video/mp4" and hv.content_length == len(HOSTED)
    assert hv.sha256 == hashlib.sha256(HOSTED).hexdigest() and hv.identity_ok
    assert [c[0] for c in s.calls] == ["HEAD", "GET"]
    assert all(c[2]["allow_redirects"] is False for c in s.calls)
    assert all("headers" not in c[2] for c in s.calls)            # no per-request auth/cookie headers
    assert not any((tmp_path / "tmp").iterdir())                  # temp copy deleted


def test_anonymous_session_has_no_credentials():
    s = anonymous_session()
    assert s.trust_env is False                                   # no .netrc / proxy creds
    assert not any(k.lower() in ("authorization", "cookie") for k in s.headers)
    assert len(s.cookies) == 0


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_refused_not_followed(tmp_path, status):
    s = HostSession(head=HostResp(status, b"", {"Location": "https://evil.example/x.mp4"}))
    with pytest.raises(HostedVideoError, match="redirect"):
        inspect(tmp_path, s)
    assert len(s.calls) == 1


@pytest.mark.parametrize("status,why", [(403, "authentication"), (401, "authentication"), (404, "HTTP 404"),
                                        (500, "HTTP 500")])
def test_http_errors(tmp_path, status, why):
    with pytest.raises(HostedVideoError, match=why):
        inspect(tmp_path, HostSession(head=HostResp(status)))


@pytest.mark.parametrize("ctype", ["text/html; charset=utf-8", "application/octet-stream", "video/quicktime",
                                   None])
def test_wrong_content_type(tmp_path, ctype):
    with pytest.raises(HostedVideoError, match="Content-Type"):
        inspect(tmp_path, HostSession(head=HostResp(headers={"Content-Type": ctype})))


def test_html_body_with_video_content_type(tmp_path):
    page = b"<!DOCTYPE html><html><body>Not found</body></html>"
    s = HostSession(get=HostResp(body=page))
    with pytest.raises(HostedVideoError, match="web page"):
        inspect(tmp_path, s)


def test_non_mp4_bytes(tmp_path):
    with pytest.raises(HostedVideoError, match="ftyp"):
        inspect(tmp_path, HostSession(get=HostResp(body=b"\x00" * 100)))


@pytest.mark.parametrize("headers,why", [({"Content-Length": None}, "Content-Length"),
                                         ({"Content-Length": "abc"}, "Content-Length"),
                                         ({"Content-Length": str(400 * 1024 * 1024)}, "300 MB"),
                                         ({"Content-Encoding": "gzip"}, "Content-Encoding")])
def test_bad_length_or_encoding(tmp_path, headers, why):
    with pytest.raises(HostedVideoError, match=why):
        inspect(tmp_path, HostSession(head=HostResp(headers=headers)))


def test_truncated_and_oversized_downloads(tmp_path):
    short = HostResp(headers={"Content-Length": str(len(HOSTED) + 10)})
    with pytest.raises(HostedVideoError, match="Content-Length said"):
        inspect(tmp_path, HostSession(get=short))
    long = HostResp(headers={"Content-Length": str(len(HOSTED) - 10)})
    with pytest.raises(HostedVideoError, match="more than the advertised"):
        inspect(tmp_path, HostSession(get=long))


def test_moov_at_end_refused(tmp_path):
    body = fake_mp4(moov_first=False)
    with pytest.raises(HostedVideoError, match="moov"):
        inspect(tmp_path, HostSession(get=HostResp(body=body), head=HostResp(body=body)))


def test_identity_mismatch_and_unchecked(tmp_path):
    vals = iter(["aa" * 32, "bb" * 32])
    hv = inspect(tmp_path, source_path=tmp_path / "o.mov", ffmpeg="ff", hasher=lambda p, f: next(vals))
    assert hv.identity == "MISMATCH" and not hv.identity_ok
    hv = inspect(tmp_path, source_path=tmp_path / "o.mov", ffmpeg=None)
    assert hv.identity == "NOT CHECKED" and not hv.identity_ok


def test_video_stream_hash_parses_ffmpeg_output(tmp_path):
    class P:
        returncode, stdout, stderr = 0, "SHA256=ABCDEF\n", ""
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return P()
    assert video_stream_sha256(tmp_path / "x.mp4", "ffmpeg", run=run) == "abcdef"
    assert seen["cmd"][seen["cmd"].index("-map") + 1] == "0:v:0" and "copy" in seen["cmd"]
    assert video_stream_sha256(tmp_path / "x.mp4", None) is None


# ------------------------------------------------------------------ Meta request shape
def test_url_container_body_exact():
    assert url_reel_container_body("", "TRIAL", "MANUAL", URL) == {
        "media_type": "REELS", "video_url": URL, "trial_params": {"graduation_strategy": "MANUAL"}}
    with pytest.raises(ValueError):
        url_reel_container_body("", "TRIAL", None, URL)
    with pytest.raises(ValueError):
        url_reel_container_body("", "TRIAL", "MANUAL", "")


def test_create_container_from_url_sends_json_without_upload_type(make_config):
    s = happy_session()
    c = MetaClient(make_config(), session=s, sleep=lambda x: None)
    cont = c.create_reel_container_from_url("", "TRIAL", "MANUAL", URL)
    assert cont.id == "900" and cont.upload_uri == ""
    (call,) = s.calls
    assert call["method"] == "POST" and call["url"] == f"https://graph.facebook.com/v27.0/{IG_ID}/media"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(call["data"])
    assert body == {"media_type": "REELS", "video_url": URL, "trial_params": {"graduation_strategy": "MANUAL"}}
    assert "upload_type" not in call["data"] and TOKEN not in call["data"] and call["params"] is None


def test_create_container_from_url_not_retried_and_trial_rejection(make_config):
    s = happy_session()
    s.routes.insert(0, ["POST", f"/{IG_ID}/media", [FakeResponse(500, {"error": {"message": "x", "code": 2}})]])
    with pytest.raises(MetaApiError):
        MetaClient(make_config(), session=s, sleep=lambda x: None).create_reel_container_from_url("", "TRIAL", "MANUAL", URL)
    assert len(s.writes()) == 1
    s = happy_session()
    s.routes.insert(0, ["POST", f"/{IG_ID}/media", [FakeResponse(400, {"error": {
        "message": "does not meet the trial reel follower requirement", "code": 10, "error_subcode": 2207081}})]])
    with pytest.raises(TrialRejectedError):
        MetaClient(make_config(), session=s).create_reel_container_from_url("", "TRIAL", "MANUAL", URL)


def test_url_job_key_is_distinct_but_resumable_key_unchanged(tmp_path):
    p = tmp_path / "a.mov"
    old = make_job_key("1", p, "h", "TRIAL")
    assert old == make_job_key("1", p, "h", "TRIAL", "resumable")
    assert make_job_key("1", p, "h", "TRIAL", "url") != old


# ------------------------------------------------------------------ CLI flows
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
    return dataclasses.replace(make_config(), profile="personal")


def hosted_ok(url=URL, identity="MATCH"):
    calls = []

    def inspector(u, *, tmp_root, source_path, ffmpeg):
        calls.append(u)
        return HostedVideo(url=u, host="pub-x.r2.dev", addresses=[PUBLIC_IP], content_type="video/mp4",
                           content_length=len(HOSTED), sha256=hashlib.sha256(HOSTED).hexdigest(),
                           report=VideoReport(path=source_path, size_bytes=len(HOSTED), sha256="x"),
                           stream_sha256="ab" * 32,
                           source_stream_sha256="ab" * 32 if identity == "MATCH" else "cd" * 32,
                           identity=identity)
    inspector.calls = calls
    return inspector


def run(cfg, session, *argv, answer="testacct", inspector=None, upload_url=True):
    factory = lambda c: MetaClient(c, session=session, sleep=lambda x: None)  # noqa: E731
    base = ["--account", "personal"]
    if upload_url:
        base += ["--upload-method", "url", "--video-url", URL]
    return cli.main(base + list(argv), config=cfg, client_factory=factory, input_fn=lambda p: answer,
                    remuxer_factory=lambda c: Remuxer(c.tmp_dir, ffmpeg=None),
                    hosted_inspector=inspector or hosted_ok())


def writes(s):
    return [c for c in s.calls if c["method"] == "POST"]


def store(cfg):
    return StateStore(cfg.state_path)


def seed_unknown(cfg, account=IG_ID):
    st = store(cfg)
    st.insert(job_key="j3", account_id=account, source_path="/v/IMG_3813.MOV", relative_path="IMG_3813.MOV",
              content_hash="original-of-another-run", size_bytes=1, graduation_strategy="MANUAL")
    st.update("j3", "UNKNOWN", container_id="18657792691044336", last_error="code=-1 subcode=2207085")
    st.conn.execute("UPDATE jobs SET publish_attempts=2 WHERE job_key='j3'")
    return st


def test_dry_run_ready_makes_no_writes(cfg, video, capsys):
    s = happy_session()
    assert run(cfg, s, "--dry-run") == 0
    assert writes(s) == [] and all(c["method"] == "GET" for c in s.calls)
    out = capsys.readouterr().out
    assert "@testacct" in out and f"IG ID {IG_ID}" in out
    assert '"trial_params": {"graduation_strategy": "MANUAL"}' in out
    assert "trial_params:  INCLUDED" in out and "upload_type:   OMITTED" in out
    assert "Identity:    MATCH" in out and "Publishing lock: none" in out and "Ready:" in out
    assert "rupload.facebook.com" not in out


def test_dry_run_reports_lock_from_unknown_job(cfg, video, capsys):
    seed_unknown(cfg)
    s = happy_session()
    assert run(cfg, s, "--dry-run") == 2 and writes(s) == []
    out = capsys.readouterr().out
    assert "Publishing lock: PAUSED" in out and "LIVE TEST BLOCKED" in out and "18657792691044336" in out


def test_dry_run_identity_mismatch_or_bad_url(cfg, video, capsys):
    s = happy_session()
    assert run(cfg, s, "--dry-run", inspector=hosted_ok(identity="MISMATCH")) == 2
    assert "NOT READY" in capsys.readouterr().out

    def bad(u, **kw):
        raise HostedVideoError("HEAD: HTTP 302 redirect to x")
    assert run(cfg, s, "--dry-run", inspector=bad) == 2 and writes(s) == []


def test_successful_url_publication(cfg, video, capsys):
    s = happy_session(statuses=("IN_PROGRESS", "FINISHED"))
    assert run(cfg, s) == 0
    urls = [c["url"] for c in writes(s)]
    assert urls == [f"https://graph.facebook.com/v27.0/{IG_ID}/media",
                    f"https://graph.facebook.com/v27.0/{IG_ID}/media_publish"]           # no rupload
    assert not any("rupload" in c["url"] for c in s.calls)
    body = json.loads(writes(s)[0]["data"])
    assert body["trial_params"] == {"graduation_strategy": "MANUAL"} and "upload_type" not in body
    rec = store(cfg).all()[0]
    assert rec.state == "PUBLISHED" and rec.media_id == "1800" and rec.upload_method == "url"
    assert rec.video_url == URL and rec.publish_mode == "TRIAL" and rec.upload_uri is None
    assert rec.content_hash == hashlib.sha256(video.read_bytes()).hexdigest()          # ORIGINAL identity
    assert rec.upload_sha256 == hashlib.sha256(HOSTED).hexdigest()                     # hosted copy
    assert any("video stream sha256" in (e["detail"] or "") for e in store(cfg).events(rec.job_key))
    out = capsys.readouterr().out
    assert "Requested mode:  TRIAL / MANUAL via url" in out and "Confirmed mode:  NOT VERIFIED" in out
    assert "Trial status is NOT verified" in out


def test_unknown_job_blocks_url_mode_before_any_network(cfg, video):
    seed_unknown(cfg)
    s = happy_session()
    insp = hosted_ok()
    assert run(cfg, s, inspector=insp) == 2
    assert s.calls == [] and insp.calls == []                                          # nothing at all
    assert store(cfg).get("j3").state == "UNKNOWN" and store(cfg).get("j3").publish_attempts == 2


def test_unknown_account_job_also_blocks(cfg, video):
    seed_unknown(cfg, account="")
    s = happy_session()
    assert run(cfg, s) == 2 and s.calls == []


def test_cross_method_duplicate_resumable_then_url(cfg, video):
    s = happy_session()
    assert run(cfg, s, "--yes", upload_url=False) == 0                                 # resumable trial
    s2 = happy_session()
    assert run(cfg, s2) == 2 and writes(s2) == []


def test_cross_method_duplicate_url_then_resumable(cfg, video):
    assert run(cfg, happy_session()) == 0
    for argv in (("--yes",), ("--public",)):
        s = happy_session()
        assert run(cfg, s, *argv, answer="PUBLIC", upload_url=False) in (0, 2)
        assert writes(s) == []


def test_url_rerun_after_success_not_reposted(cfg, video):
    assert run(cfg, happy_session()) == 0
    s = happy_session()
    assert run(cfg, s) == 2 and writes(s) == []


def test_wrong_account_token_refused(cfg, video):
    s = happy_session()
    s.routes.insert(0, ["GET", f"/{IG_ID}", [FakeResponse(200, {"id": "17841400000000002", "username": "other"})]])
    assert run(cfg, s) == 2 and writes(s) == []


@pytest.mark.parametrize("answer", ["post", "", "nashhawkins", "testacct2", "yes"])
def test_confirmation_requires_exact_account_name(cfg, video, answer):
    s = happy_session()
    assert run(cfg, s, answer=answer) == 1 and writes(s) == []


def test_at_prefixed_account_name_accepted(cfg, video):
    assert run(cfg, happy_session(), answer="@testacct") == 0


def test_non_interactive_refused(cfg, video, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("N", (), {"isatty": lambda self: False})())
    s = happy_session()
    assert run(cfg, s) == 2 and s.calls == []


@pytest.mark.parametrize("status", ["ERROR", "EXPIRED"])
def test_processing_error_is_pre_publish_failure(cfg, video, status):
    s = happy_session(statuses=("IN_PROGRESS", status))
    assert run(cfg, s) == 2
    assert not any(c["url"].endswith("media_publish") for c in writes(s))
    rec = store(cfg).all()[0]
    assert rec.state == "FAILED" and rec.failure_phase == "PRE_PUBLISH" and not is_uncertain(rec)


def test_container_creation_failure_is_pre_publish(cfg, video):
    s = happy_session()
    s.routes.insert(0, ["POST", f"/{IG_ID}/media", [FakeResponse(400, {"error": {"message": "Invalid video_url",
                                                                               "code": 9004, "error_subcode": 2207052}})]])
    assert run(cfg, s) == 2
    rec = store(cfg).all()[0]
    assert rec.state == "FAILED" and rec.failure_phase == "PRE_PUBLISH" and len(writes(s)) == 1


def test_ambiguous_publish_is_unknown_and_locks_account(cfg, video, capsys):
    s = happy_session()
    s.routes.insert(0, ["POST", "/media_publish", [FakeResponse(400, REAL_2207085)]])
    assert run(cfg, s) == 3
    rec = store(cfg).all()[0]
    assert rec.state == "UNKNOWN" and rec.upload_method == "url" and is_uncertain(rec)
    assert [b.id for b in store(cfg).account_blockers(IG_ID)] == [rec.id]
    s2 = happy_session()
    assert run(cfg, s2) == 2 and s2.calls == []                                     # no auto-retry
    assert run(cfg, s2, "--yes", upload_url=False) in (2, 3) and writes(s2) == []   # nor via resumable


def test_url_job_never_falls_back_to_public(cfg, video):
    s = happy_session()
    s.routes.insert(0, ["POST", f"/{IG_ID}/media", [FakeResponse(400, {"error": {
        "message": "does not meet the trial reel follower requirement", "code": 10, "error_subcode": 2207081}})]])
    assert run(cfg, s) == 2
    assert len(writes(s)) == 1 and "trial_params" in writes(s)[0]["data"]


def test_resumable_default_unchanged(cfg, video):
    s = happy_session()
    assert run(cfg, s, "--yes", upload_url=False) == 0
    first = writes(s)[0]
    assert first["data"]["upload_type"] == "resumable" and "video_url" not in first["data"]
    assert any("rupload" in c["url"] for c in writes(s))
    assert store(cfg).all()[0].upload_method == "resumable"


def test_credentials_never_stored_or_logged(cfg, video, capsys):
    s = happy_session()
    s.routes.insert(0, ["POST", "/media_publish", [FakeResponse(400, {"error": {
        "message": f"bad token {TOKEN}", "code": -1, "error_subcode": 2207085}})]])
    run(cfg, s)
    st = store(cfg)
    dump = json.dumps([dict(r) for r in st.conn.execute("SELECT * FROM jobs")] +
                      [dict(r) for r in st.conn.execute("SELECT * FROM events")] +
                      [dict(r) for r in st.conn.execute("SELECT * FROM api_calls")])
    assert TOKEN not in dump
    assert TOKEN not in capsys.readouterr().out
    assert TOKEN not in (cfg.log_dir / "publisher.log").read_text()


@pytest.mark.parametrize("argv", [
    ["--account", "personal", "--upload-method", "url"],
    ["--upload-method", "url", "--video-url", URL],
    ["--account", "personal", "--upload-method", "url", "--video-url", URL, "--public"],
    ["--account", "personal", "--upload-method", "url", "--video-url", URL, "--yes"],
    ["--account", "personal", "--upload-method", "url", "--video-url", URL, "--reconcile"],
    ["--account", "personal", "--video-url", URL],
])
def test_cli_argument_rules(argv):
    with pytest.raises(SystemExit) as e:
        cli.parse_args(argv)
    assert e.value.code == 2


# ------------------------------------------------------------------ schema v5 migration
def test_v5_rebuild_from_true_v4_layout(tmp_path):
    """A real v4 table (no upload_method/video_url, old UNIQUE) is rebuilt with every row, id and event kept,
    after a backup; afterwards a URL job for job #3's video can coexist with job #3."""
    import sqlite3
    from trial_reels.state import API_CALLS_DDL, JOBS_DDL, SCHEMA
    v4_jobs = (JOBS_DDL.format(name="jobs")
               .replace("    upload_method TEXT NOT NULL DEFAULT 'resumable' CHECK (upload_method IN ('resumable', 'url')),\n", "")
               .replace("    video_url TEXT,\n", "")
               .replace("    attempt INTEGER NOT NULL DEFAULT 1,\n", "")
               .replace("publish_mode, upload_method, attempt)", "publish_mode)"))
    assert "upload_method" not in v4_jobs
    db = tmp_path / "state.sqlite3"
    c = sqlite3.connect(db)
    c.executescript(v4_jobs + SCHEMA.split(";", 1)[1].split("CREATE TABLE IF NOT EXISTS jobs")[0] + API_CALLS_DDL)
    c.executescript("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT NOT NULL, "
                    "at TEXT NOT NULL, state TEXT NOT NULL, detail TEXT);")
    for i, (k, state, att, cont) in enumerate([("k1", "FAILED", 0, None), ("k2", "PUBLISHED", 1, "c2"),
                                               ("k3", "UNKNOWN", 2, "18657792691044336")], start=1):
        c.execute("INSERT INTO jobs (id, job_key, account_id, source_path, relative_path, content_hash, size_bytes, "
                  "state, container_id, publish_attempts, failure_phase, created_at, updated_at, publish_mode) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (i, k, "17840000000000002" if k == "k3" else "17840000000000001", "/v/IMG_3813.MOV",
                   "IMG_3813.MOV", "H", 1, state, cont, att, "PRE_PUBLISH" if state == "FAILED" else None, "t", "t",
                   "PUBLIC" if k == "k2" else "TRIAL"))
        c.execute("INSERT INTO events (job_key, at, state, detail) VALUES (?, 't', ?, 'x')", (k, state))
    c.execute("PRAGMA user_version = 4")
    c.commit()
    c.close()
    from test_safety import snapshot
    jobs_before, events_before = snapshot(db)
    st = StateStore(db)
    assert st.backup_path and snapshot(st.backup_path) == (jobs_before, events_before)
    jobs, events = snapshot(db)
    assert events == events_before
    assert {i: {k: v for k, v in r.items() if k not in ("upload_method", "video_url", "attempt")} for i, r in jobs.items()} \
        == jobs_before
    assert all(r["upload_method"] == "resumable" and r["attempt"] == 1 for r in jobs.values())
    assert st.conn.execute("PRAGMA user_version").fetchone()[0] == 8
    st.insert(job_key="url", account_id="17840000000000002", source_path="/v/IMG_3813.MOV",
              relative_path="IMG_3813.MOV", content_hash="H", size_bytes=1, graduation_strategy="MANUAL",
              upload_method="url", video_url=URL)
    assert st.get("url").id == 4
    assert [b.id for b in st.account_blockers("17840000000000002")] == [3]
    assert StateStore(db).backup_path is None                 # idempotent: no second backup/rebuild
