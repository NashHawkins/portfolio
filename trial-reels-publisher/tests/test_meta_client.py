import json

import pytest
import requests

from conftest import IG_ID, TOKEN, UPLOAD_URI, FakeResponse, FakeSession, fake_mp4, happy_session
from trial_reels.errors import (AmbiguousPublishError, AuthError, MetaApiError, ProcessingError,
                                TrialRejectedError)
from trial_reels.meta_client import MetaClient

pytestmark = pytest.mark.usefixtures("legacy_resumable_trial")  # see conftest


def client(cfg, session, sleeps=None):
    return MetaClient(cfg, session=session, sleep=(sleeps.append if sleeps is not None else lambda s: None))


@pytest.mark.parametrize("strategy", ["MANUAL", "SS_PERFORMANCE"])
def test_container_creation_params(make_config, strategy):
    s = happy_session()
    c = client(make_config(TRIAL_GRADUATION_STRATEGY=strategy), s).create_trial_reel_container("", strategy)
    assert c.id == "900" and c.upload_uri == UPLOAD_URI
    call = s.calls[-1]
    assert call["url"] == f"https://graph.facebook.com/v27.0/{IG_ID}/media"
    assert call["data"]["media_type"] == "REELS"
    assert call["data"]["upload_type"] == "resumable"
    assert json.loads(call["data"]["trial_params"]) == {"graduation_strategy": strategy}
    assert "video_url" not in call["data"] and "caption" not in call["data"]
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in call["url"] and not call["params"]


def test_caption_included_when_set(make_config):
    s = happy_session()
    client(make_config(), s).create_trial_reel_container("hello", "MANUAL")
    assert s.calls[-1]["data"]["caption"] == "hello"


def test_untrusted_upload_uri_rejected(make_config):
    s = FakeSession().on("POST", f"/{IG_ID}/media", FakeResponse(200, {"id": "1", "uri": "https://evil.example/up"}))
    with pytest.raises(MetaApiError, match="untrusted"):
        client(make_config(), s).create_trial_reel_container("", "MANUAL")


def test_missing_uri_means_resumable_unsupported(make_config):
    s = FakeSession().on("POST", f"/{IG_ID}/media", FakeResponse(200, {"id": "1"}))
    with pytest.raises(MetaApiError, match="Resumable upload may not be supported"):
        client(make_config(), s).create_trial_reel_container("", "MANUAL")


def test_trial_params_rejected_is_typed_and_single_request(make_config):
    s = FakeSession().on("POST", f"/{IG_ID}/media", FakeResponse(400, {"error": {
        "message": "Invalid parameter trial_params", "type": "OAuthException", "code": 100,
        "error_subcode": 2207001, "fbtrace_id": "AbC"}}))
    with pytest.raises(TrialRejectedError) as ei:
        client(make_config(), s).create_trial_reel_container("", "MANUAL")
    assert "code=100" in str(ei.value) and "fbtrace_id=AbC" in str(ei.value)
    assert "NOT retry as an ordinary Reel" in str(ei.value)
    assert len(s.calls) == 1  # no retry, no fallback without trial_params


def test_upload_streams_file_with_exact_headers(make_config, tmp_path):
    p = tmp_path / "v.mp4"
    data = fake_mp4()
    p.write_bytes(data)
    s = happy_session()
    client(make_config(), s).upload_video(UPLOAD_URI, p, len(data))
    call = s.calls[-1]
    assert call["url"] == UPLOAD_URI and call["streamed"] and call["body_bytes"] == data
    h = call["headers"]
    assert h["Authorization"] == f"OAuth {TOKEN}" and h["offset"] == "0"
    assert h["file_size"] == str(len(data)) and h["Content-Type"] == "application/octet-stream"


def test_upload_error_envelope(make_config, tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(fake_mp4())
    s = FakeSession().on("POST", "/900", FakeResponse(400, {"debug_info": {
        "retriable": False, "type": "ProcessingFailedError",
        "message": json.dumps({"error": {"message": "unauthorized user request"}})}}))
    with pytest.raises(MetaApiError, match="unauthorized user request.*ProcessingFailedError"):
        client(make_config(), s).upload_video(UPLOAD_URI, p, p.stat().st_size)
    assert len(s.calls) == 1


def test_poll_in_progress_then_finished(make_config):
    s = happy_session(statuses=("IN_PROGRESS", "IN_PROGRESS", "FINISHED"))
    st = client(make_config(), s).wait_until_finished("900")
    assert st.status_code == "FINISHED" and s.count("GET", "/900") == 3


@pytest.mark.parametrize("status", ["ERROR", "EXPIRED"])
def test_poll_terminal_failure(make_config, status):
    s = happy_session(statuses=(status,))
    with pytest.raises(ProcessingError, match=status):
        client(make_config(), s).wait_until_finished("900")


def test_poll_timeout_is_bounded(make_config):
    s = happy_session(statuses=("IN_PROGRESS",))
    t = {"now": 0.0}
    c = MetaClient(make_config(POLL_TIMEOUT_SECONDS="20", POLL_INTERVAL_SECONDS="5"), session=s,
                   sleep=lambda d: t.__setitem__("now", t["now"] + d), clock=lambda: t["now"])
    with pytest.raises(ProcessingError, match="after 20s"):
        c.wait_until_finished("900")
    assert s.count("GET", "/900") == 5


def test_expired_token_typed(make_config):
    s = FakeSession().on("GET", f"/{IG_ID}", FakeResponse(400, {"error": {
        "message": "Error validating access token: Session has expired", "type": "OAuthException", "code": 190}}))
    with pytest.raises(AuthError, match="invalid or expired"):
        client(make_config(), s).get_account()
    assert len(s.calls) == 1  # auth errors are not retried


def test_missing_permission_typed(make_config):
    s = FakeSession().on("POST", f"/{IG_ID}/media", FakeResponse(403, {"error": {
        "message": "(#10) Application does not have permission for this action", "code": 10}}))
    with pytest.raises(MetaApiError, match="lacks a required permission"):
        client(make_config(), s).create_trial_reel_container("", "MANUAL")
    assert len(s.calls) == 1


def test_429_retries_with_retry_after_then_succeeds(make_config):
    sleeps = []
    s = FakeSession().on("GET", f"/{IG_ID}",
                         FakeResponse(429, {"error": {"message": "rate", "code": 4}}, headers={"Retry-After": "7"}),
                         FakeResponse(200, {"id": IG_ID, "username": "u"}))
    assert client(make_config(), s, sleeps).get_account()["username"] == "u"
    assert sleeps == [7.0]


def test_retry_after_is_capped(make_config):
    sleeps = []
    s = FakeSession().on("GET", f"/{IG_ID}",
                         FakeResponse(429, {"error": {"message": "rate", "code": 4}}, headers={"Retry-After": "99999"}),
                         FakeResponse(200, {"id": IG_ID}))
    client(make_config(), s, sleeps).get_account()
    assert sleeps == [120.0]


def test_5xx_retries_are_bounded(make_config):
    s = FakeSession().on("GET", f"/{IG_ID}", FakeResponse(500, {"error": {"message": "boom", "code": 2}}))
    with pytest.raises(MetaApiError, match="boom"):
        client(make_config(MAX_RETRIES="2"), s).get_account()
    assert len(s.calls) == 3


def test_publish_success(make_config):
    s = happy_session()
    assert client(make_config(), s).publish_container("900") == "1800"
    assert s.calls[-1]["data"] == {"creation_id": "900"}


@pytest.mark.parametrize("exc", [requests.ReadTimeout("t"), requests.ConnectionError("reset")])
def test_publish_network_failure_is_ambiguous_and_not_retried(make_config, exc):
    s = FakeSession().on("POST", "/media_publish", exc)
    with pytest.raises(AmbiguousPublishError):
        client(make_config(), s).publish_container("900")
    assert len(s.calls) == 1


def test_publish_5xx_is_ambiguous(make_config):
    s = FakeSession().on("POST", "/media_publish", FakeResponse(503, {"error": {"message": "unavailable", "code": 2}}))
    with pytest.raises(AmbiguousPublishError):
        client(make_config(), s).publish_container("900")
    assert len(s.calls) == 1


def test_publish_connect_timeout_is_definitive(make_config):
    s = FakeSession().on("POST", "/media_publish", requests.ConnectTimeout("no route"))
    with pytest.raises(MetaApiError) as ei:
        client(make_config(), s).publish_container("900")
    assert not isinstance(ei.value, AmbiguousPublishError)
