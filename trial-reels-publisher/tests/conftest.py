import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trial_reels.config import load_config  # noqa: E402

TOKEN = "EAATESTTOKEN1234567890abcdefghijklmnopqrstuvwxyz"
IG_ID = "17841400000000001"


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def fake_mp4(moov_first: bool = True, payload: bytes = b"x" * 2048) -> bytes:
    ftyp = box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
    moov = box(b"moov", b"\x00" * 64)
    mdat = box(b"mdat", payload)
    return ftyp + (moov + mdat if moov_first else mdat + moov)


@pytest.fixture
def videos_dir(tmp_path):
    d = tmp_path / "Documents" / "Trial-Reels"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def make_config(tmp_path, videos_dir):
    def _make(**overrides):
        env = {
            "IG_USER_ID": IG_ID,
            "IG_ACCESS_TOKEN": TOKEN,
            "GRAPH_API_VERSION": "v27.0",
            "TRIAL_REELS_DIR": str(videos_dir),
            "TRIAL_GRADUATION_STRATEGY": "MANUAL",
            "TRIAL_REELS_DATA_DIR": str(tmp_path / "data"),
            "TRIAL_REELS_LOG_DIR": str(tmp_path / "logs"),
            "POLL_INTERVAL_SECONDS": "0",
            "POLL_TIMEOUT_SECONDS": "30",
            "MAX_RETRIES": "2",
        }
        env.update({k: v for k, v in overrides.items()})
        env = {k: v for k, v in env.items() if v is not None}
        return load_config(env=env)
    return _make


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Routes requests to handlers keyed by (METHOD, url-suffix). Records every call."""

    def __init__(self):
        self.headers = {}
        self.calls = []
        self.routes = []

    def on(self, method, suffix, *responses):
        """Responses are consumed in order; the last one repeats. Items may be exceptions or callables."""
        self.routes.append([method.upper(), suffix, list(responses)])
        return self

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        streamed = hasattr(data, "read")
        body_bytes = None
        if streamed:
            body_bytes = data.read()
        self.calls.append({"method": method.upper(), "url": url, "params": params,
                           "data": None if streamed else data, "headers": dict(headers or {}),
                           "streamed": streamed, "body_bytes": body_bytes, "timeout": timeout})
        for route in self.routes:
            m, suffix, responses = route
            if m == method.upper() and url.split("?")[0].endswith(suffix):
                item = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(item, BaseException):
                    raise item
                if callable(item):
                    return item()
                return item
        raise AssertionError(f"unexpected request {method} {url}")

    def post(self, url, data=None, headers=None, timeout=None):
        return self.request("POST", url, data=data, headers=headers, timeout=timeout)

    def get(self, url, params=None, headers=None, timeout=None):
        return self.request("GET", url, params=params, headers=headers, timeout=timeout)

    # helpers
    def writes(self):
        return [c for c in self.calls if c["method"] == "POST"]

    def count(self, method, suffix):
        return sum(1 for c in self.calls if c["method"] == method and c["url"].endswith(suffix))


UPLOAD_URI = f"https://rupload.facebook.com/ig-api-upload/v27.0/900"


def happy_session(statuses=("IN_PROGRESS", "FINISHED"), media_id="1800", ig_id=IG_ID,
                  username="testacct"):
    s = FakeSession()
    s.on("GET", f"/{ig_id}", FakeResponse(200, {"id": ig_id, "username": username, "name": "Test"}))
    s.on("GET", "/me/permissions", FakeResponse(200, {"data": [
        {"permission": p, "status": "granted"} for p in
        ("instagram_basic", "instagram_content_publish", "pages_read_engagement", "pages_show_list")]}))
    s.on("GET", "/content_publishing_limit", FakeResponse(200, {"data": [
        {"quota_usage": 2, "config": {"quota_total": 100, "quota_duration": 86400}}]}))
    s.on("POST", f"/{ig_id}/media", FakeResponse(200, {"id": "900", "uri": UPLOAD_URI}))
    s.on("GET", f"/{ig_id}/media", FakeResponse(200, {"data": []}))
    s.on("POST", "/ig-api-upload/v27.0/900", FakeResponse(200, {"success": True, "message": "Upload successful."}))
    s.on("GET", "/900", *[FakeResponse(200, {"status_code": st, "status": st}) for st in statuses])
    s.on("POST", "/media_publish", FakeResponse(200, {"id": media_id}))
    s.on("GET", f"/{media_id}", FakeResponse(200, {"id": media_id, "permalink": "https://www.instagram.com/reel/abc/",
                                                   "media_product_type": "REELS"}))
    return s


@pytest.fixture(autouse=True)
def fast_offline_validation(monkeypatch):
    """No 1s stability sleeps, and no dependence on whether ffprobe happens to be installed."""
    import trial_reels.validation as v
    if not hasattr(v, "_orig_wait_for_stable_size"):
        v._orig_wait_for_stable_size = v.wait_for_stable_size
    monkeypatch.setattr(v, "wait_for_stable_size", lambda path, checks=2, interval_s=1.0: path.stat().st_size)
    monkeypatch.setattr(v.shutil, "which", lambda name: None)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Any accidental real HTTP request fails the test."""
    import requests

    def boom(*a, **k):
        raise AssertionError("real network access attempted in tests")
    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", boom)


@pytest.fixture
def legacy_resumable_trial(monkeypatch):
    """Production refuses TRIAL + resumable (Meta ignores trial_params there; see models.py). Older tests
    drive the SHARED machinery (locks, state, polling, remux, overrides) through that request; this
    re-enables it for them only. Tests of the refusal itself must NOT use this fixture."""
    from trial_reels import models
    monkeypatch.setattr(models, "RESUMABLE_TRIAL_ALLOWED", True)
