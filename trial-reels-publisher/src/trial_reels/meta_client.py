"""Instagram Graph API (Facebook Login path) + rupload transport.

This module knows nothing about folders, schedules or local state. It only turns
calls into HTTP requests. Tokens go in the Authorization header only, never in URLs.

Flow (from Meta's docs and fbsamples/reels_publishing_apis v3.0.0):
  POST graph.facebook.com/{v}/{ig_user_id}/media      media_type=REELS, upload_type=resumable, trial_params
       -> {"id": container_id, "uri": "https://rupload.facebook.com/ig-api-upload/{v}/{container_id}"}
  POST {uri}   Authorization: OAuth <token>, offset: 0, file_size: N, body = raw bytes
       -> {"success": true, "message": "Upload successful."}
  GET  graph.facebook.com/{v}/{container_id}?fields=status_code,status    until FINISHED
  POST graph.facebook.com/{v}/{ig_user_id}/media_publish  creation_id=container_id  -> {"id": media_id}
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests

from . import models as _models
from .config import RUPLOAD_HOST, VALID_STRATEGIES, Config
from .errors import (AmbiguousPublishError, AuthError, MetaApiError, ProcessingError, PublishRejectedError,
                     TrialRejectedError, redact)
from .models import PUBLIC, TRIAL
from .outcomes import classify_publish_error

log = logging.getLogger(__name__)

MAX_RETRY_AFTER_S = 120.0
TERMINAL_BAD_STATUSES = ("ERROR", "EXPIRED")


@dataclass
class Container:
    id: str
    upload_uri: str


@dataclass
class ContainerStatus:
    status_code: Optional[str]
    status: Optional[str]


class MetaClient:
    def __init__(self, config: Config, session: Optional[requests.Session] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        config.require_credentials()
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "trial-reels-publisher/1.0"})
        self._sleep = sleep
        self._clock = clock

    # ---------------------------------------------------------------- plumbing
    @property
    def _auth(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.config.access_token}"}

    def _url(self, path: str) -> str:
        return f"{self.config.graph_base}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, *, step: str, params=None, data=None,
                 retry: bool = True, json_body: Optional[dict] = None) -> dict:
        url = self._url(path)
        headers = self._auth
        if json_body is not None:  # application/json body, as in Meta's trial_params example
            if data is not None:
                raise ValueError("pass data or json_body, not both")
            data = json.dumps(json_body, separators=(",", ":"))
            headers = {**headers, "Content-Type": "application/json"}
        attempts = 1 + (self.config.max_retries if retry else 0)
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(method, url, params=params, data=data,
                                            headers=headers, timeout=self.config.http_timeout_s)
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt < attempts:
                    self._backoff(attempt, None, f"{step}: network error, retrying")
                    continue
                raise MetaApiError(f"network error talking to Meta: {type(e).__name__}", step=step,
                                   retriable=True)
            try:
                return self._parse(resp, step)
            except MetaApiError as err:
                if err.should_retry and attempt < attempts:
                    self._backoff(attempt, resp.headers.get("Retry-After"), f"{step}: {err.meta_message}")
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, attempt: int, retry_after: Optional[str], why: str) -> None:
        delay = min(2.0 ** attempt, 30.0)
        if retry_after:
            try:
                delay = min(max(float(retry_after), 0.0), MAX_RETRY_AFTER_S)
            except ValueError:
                pass
        log.warning("%s (waiting %.0fs)", redact(why), delay)
        self._sleep(delay)

    @staticmethod
    def _parse(resp: requests.Response, step: str) -> dict:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "error" in body:
            e = body["error"] if isinstance(body["error"], dict) else {"message": str(body["error"])}
            code, subcode = _int_or_none(e.get("code")), _int_or_none(e.get("error_subcode"))
            cls = AuthError if code in (102, 190) else MetaApiError
            msg = e.get("error_user_msg") or e.get("message") or "unknown Graph API error"
            raise cls(str(msg), http_status=resp.status_code, error_type=_str_or_none(e.get("type")),
                      code=code, subcode=subcode, fbtrace_id=_str_or_none(e.get("fbtrace_id")),
                      is_transient=_bool_or_none(e.get("is_transient")), step=step)
        if isinstance(body, dict) and "debug_info" in body:  # rupload error envelope
            d = body["debug_info"] or {}
            msg = d.get("message", "upload error")
            try:  # message is often an escaped JSON string
                inner = json.loads(msg)
                if isinstance(inner, dict):
                    msg = (inner.get("error") or {}).get("message") or inner.get("message") or msg
            except (TypeError, ValueError):
                pass
            raise MetaApiError(str(msg), http_status=resp.status_code, error_type=d.get("type"),
                               retriable=d.get("retriable"), step=step)
        if resp.status_code >= 400:
            raise MetaApiError(f"unexpected response: {redact(resp.text[:300])}",
                               http_status=resp.status_code, step=step)
        if not isinstance(body, dict):
            raise MetaApiError("Meta returned a non-JSON response", http_status=resp.status_code, step=step)
        return body

    # ------------------------------------------------------------- read-only
    def get_account(self) -> dict:
        return self._request("GET", self.config.ig_user_id, step="read account",
                             params={"fields": "id,username,name"})

    def get_token_permissions(self) -> Optional[List[dict]]:
        """Granted scopes for a User token. Returns None if not applicable (e.g. Page token)."""
        try:
            return self._request("GET", "me/permissions", step="read permissions").get("data", [])
        except MetaApiError as e:
            if e.is_auth_error:
                raise
            return None

    def get_publishing_limit(self) -> dict:
        body = self._request("GET", f"{self.config.ig_user_id}/content_publishing_limit",
                             step="read publishing limit", params={"fields": "config,quota_usage"})
        data = body.get("data") or [{}]
        return data[0] if data else {}

    def get_container_status(self, container_id: str) -> ContainerStatus:
        body = self._request("GET", container_id, step="check container status",
                             params={"fields": "status_code,status"})
        return ContainerStatus(body.get("status_code"), body.get("status"))

    def get_media(self, media_id: str) -> dict:
        return self._request("GET", media_id, step="read published media",
                             params={"fields": "id,permalink,media_product_type,is_shared_to_feed,timestamp"})

    def recent_media(self, limit: int = 10) -> List[dict]:
        body = self._request("GET", f"{self.config.ig_user_id}/media", step="list recent media",
                             params={"fields": "id,permalink,media_product_type,timestamp",
                                     "limit": str(limit)})
        return body.get("data", [])

    # ------------------------------------------------------------------ writes
    def create_trial_reel_container(self, caption: str, graduation_strategy: str) -> Container:
        """Create a *trial* Reel container for a local-file resumable upload."""
        return self.create_reel_container(caption, TRIAL, graduation_strategy)

    def create_reel_container(self, caption: str, mode: str,
                              graduation_strategy: Optional[str] = None) -> Container:
        """Create a Reel container (TRIAL or PUBLIC) for a local-file resumable upload.

        The mode is fixed by the caller. An error here is raised as-is; there is
        deliberately no code path that retries a rejected TRIAL request as PUBLIC.
        """
        data = reel_container_params(caption, mode, graduation_strategy)
        step = "create trial reel container" if mode == TRIAL else "create public reel container"
        # A 5xx/timeout here can leave an orphan container; unpublished containers expire
        # after 24h and are never published by us, so retrying creation is safe.
        try:
            body = self._request("POST", f"{self.config.ig_user_id}/media", step=step, data=data)
        except MetaApiError as e:
            if (mode == TRIAL and not e.should_retry and not e.is_auth_error
                    and _mentions_trial(e.meta_message)):
                raise TrialRejectedError(e.meta_message, http_status=e.http_status,
                                         error_type=e.error_type, code=e.code, subcode=e.subcode,
                                         fbtrace_id=e.fbtrace_id, step=e.step)
            raise
        cid, uri = body.get("id"), body.get("uri")
        if not cid or not uri:
            raise MetaApiError(f"container response missing id/uri (keys: {sorted(body)}). "
                               "Resumable upload may not be supported for this app/login type.",
                               step=step)
        _check_upload_uri(uri)
        return Container(str(cid), uri)

    def create_reel_container_from_url(self, caption: str, mode: str, graduation_strategy: Optional[str],
                                       video_url: str) -> Container:
        """URL method: Meta fetches ``video_url`` itself. No upload_type, no rupload, no bytes sent.

        Sent as an application/json body with trial_params as a nested object, exactly like the trial
        example in Meta's Content Publishing guide. Not retried: a duplicate POST could make a second
        container, and a rejected TRIAL request is never re-sent as PUBLIC.
        """
        body = url_reel_container_body(caption, mode, graduation_strategy, video_url)
        step = "create trial reel container (video_url)" if mode == TRIAL else "create public reel container (video_url)"
        try:
            resp = self._request("POST", f"{self.config.ig_user_id}/media", step=step, json_body=body, retry=False)
        except MetaApiError as e:
            if (mode == TRIAL and not e.is_auth_error and _mentions_trial(e.meta_message)):
                raise TrialRejectedError(e.meta_message, http_status=e.http_status,
                                         error_type=e.error_type, code=e.code, subcode=e.subcode,
                                         fbtrace_id=e.fbtrace_id, step=e.step)
            raise
        cid = resp.get("id")
        if not cid:
            raise MetaApiError(f"container response missing id (keys: {sorted(resp)})", step=step)
        return Container(str(cid), "")

    def upload_video(self, upload_uri: str, path: Path, size_bytes: int) -> dict:
        """Stream the file to Meta's rupload URI from offset 0. Not retried automatically."""
        _check_upload_uri(upload_uri)
        actual = Path(path).stat().st_size
        if actual != size_bytes:
            raise MetaApiError(f"file size changed since validation ({size_bytes} -> {actual}); not uploading",
                               step="upload video")
        headers = {
            "Authorization": f"OAuth {self.config.access_token}",
            "offset": "0",
            "file_size": str(size_bytes),
            "Content-Type": "application/octet-stream",
        }
        with open(path, "rb") as f:  # file object => requests streams it, no full read into memory
            try:
                resp = self.session.post(upload_uri, data=f, headers=headers,
                                         timeout=(self.config.http_timeout_s, self.config.upload_timeout_s))
            except (requests.Timeout, requests.ConnectionError) as e:
                raise MetaApiError(f"network error during upload: {type(e).__name__}", step="upload video",
                                   retriable=True)
        body = self._parse(resp, "upload video")
        if body.get("success") is not True:
            raise MetaApiError(f"upload not acknowledged: {redact(json.dumps(body)[:300])}", step="upload video")
        return body

    def wait_until_finished(self, container_id: str,
                            on_status: Optional[Callable[[ContainerStatus], None]] = None) -> ContainerStatus:
        deadline = self._clock() + self.config.poll_timeout_s
        last: Optional[ContainerStatus] = None
        while True:
            last = self.get_container_status(container_id)
            if on_status:
                on_status(last)
            if last.status_code == "FINISHED":
                return last
            if last.status_code in TERMINAL_BAD_STATUSES:
                raise ProcessingError(f"Meta finished processing with status {last.status_code}: "
                                      f"{last.status or 'no detail'}. Nothing was published.")
            if last.status_code == "PUBLISHED":
                raise ProcessingError("Container is already PUBLISHED; refusing to publish again.")
            if self._clock() >= deadline:
                raise ProcessingError(
                    f"Container {container_id} still {last.status_code or 'unknown'} after "
                    f"{self.config.poll_timeout_s:.0f}s. Nothing was published. Increase "
                    "POLL_TIMEOUT_SECONDS or run again later.")
            self._sleep(self.config.poll_interval_s)

    def publish_container(self, container_id: str) -> str:
        """Send media_publish exactly once. Never retried here.

        Raises PublishRejectedError only for a documented pre-publish rejection (see
        outcomes.classify_publish_error) or when the connection never opened. Every other
        failure -- including HTTP 400 server faults such as code -1 / 2207085 -- raises
        AmbiguousPublishError so the caller marks the job UNKNOWN.
        """
        url = self._url(f"{self.config.ig_user_id}/media_publish")
        try:
            resp = self.session.post(url, data={"creation_id": container_id}, headers=self._auth,
                                     timeout=self.config.http_timeout_s)
        except requests.ConnectTimeout as e:  # never connected => request definitely not sent
            c = classify_publish_error(None, request_may_have_reached_meta=False)
            err = PublishRejectedError(f"could not connect to Meta: {type(e).__name__}", step="publish",
                                       retriable=True)
            err.reason = c.reason
            raise err
        except requests.RequestException as e:  # read timeout, reset, truncated body, SSL, ...
            raise AmbiguousPublishError(f"publish request failed with {type(e).__name__} after it may have "
                                        "reached Meta; the Reel may or may not be live.",
                                        reason=f"{type(e).__name__} after the connection was opened")
        try:
            body = self._parse(resp, "publish")
        except MetaApiError as e:
            c = classify_publish_error(e)
            if c.not_published:
                rej = PublishRejectedError(e.meta_message, http_status=e.http_status, error_type=e.error_type,
                                           code=e.code, subcode=e.subcode, fbtrace_id=e.fbtrace_id,
                                           is_transient=e.is_transient, step="publish")
                rej.reason = c.reason
                raise rej
            raise AmbiguousPublishError(f"Meta's reply to media_publish does not prove the Reel was rejected "
                                        f"({c.reason}); it may or may not be live. Meta said: {e}",
                                        meta=e, reason=c.reason)
        media_id = body.get("id")
        if not media_id:
            raise AmbiguousPublishError(f"publish response had no media id: {redact(json.dumps(body)[:200])}",
                                        reason="success response without a media id")
        return str(media_id)


def reel_container_params(caption: str, mode: str, graduation_strategy: Optional[str] = None) -> Dict[str, str]:
    """The exact form fields sent to POST /{ig-user-id}/media. Also used by --dry-run.

    PUBLIC only. TRIAL is refused: Meta documents trial_params only for video_url uploads ("Other
    parameters are not supported" for resumable), and job #8 (resumable + trial_params MANUAL) went live as
    a normal Reel. Trial Reels must use url_reel_container_body().
    """
    data = {"media_type": "REELS", "upload_type": "resumable"}
    if mode == TRIAL:
        if not _models.RESUMABLE_TRIAL_ALLOWED:
            raise ValueError("Trial Reels cannot use resumable upload (Meta ignores trial_params there); "
                             "use the video_url method")
        if graduation_strategy not in VALID_STRATEGIES:
            raise ValueError("graduation_strategy must be MANUAL or SS_PERFORMANCE")
        data["trial_params"] = json.dumps({"graduation_strategy": graduation_strategy})
    elif mode == PUBLIC:
        if graduation_strategy is not None:
            raise ValueError("a PUBLIC reel has no graduation strategy")
    else:
        raise ValueError(f"unknown publish mode {mode!r}")
    if caption:
        data["caption"] = caption
    assert "video_url" not in data
    return data


def url_reel_container_body(caption: str, mode: str, graduation_strategy: Optional[str],
                            video_url: str) -> Dict[str, object]:
    """The exact JSON body for POST /{ig-user-id}/media in URL mode. Also used by --dry-run.

    TRIAL: {"media_type": "REELS", "video_url": ..., "trial_params": {"graduation_strategy": ...}}
    upload_type is never present: that would switch Meta to the rupload protocol.
    """
    if not video_url:
        raise ValueError("video_url is required for the url upload method")
    body: Dict[str, object] = {"media_type": "REELS", "video_url": video_url}
    if mode == TRIAL:
        if graduation_strategy not in VALID_STRATEGIES:
            raise ValueError("graduation_strategy must be MANUAL or SS_PERFORMANCE")
        body["trial_params"] = {"graduation_strategy": graduation_strategy}
    elif mode == PUBLIC:
        if graduation_strategy is not None:
            raise ValueError("a PUBLIC reel has no graduation strategy")
    else:
        raise ValueError(f"unknown publish mode {mode!r}")
    if caption:
        body["caption"] = caption
    assert "upload_type" not in body
    return body


def _int_or_none(v) -> Optional[int]:
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _str_or_none(v) -> Optional[str]:
    return None if v is None else str(v)


def _bool_or_none(v) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower() == "true"
    return None


def _mentions_trial(msg: str) -> bool:
    m = msg.lower()
    return "trial" in m or "graduation" in m


def _check_upload_uri(uri: str) -> None:
    p = urlparse(uri)
    if p.scheme != "https" or p.hostname != RUPLOAD_HOST:
        raise MetaApiError(f"refusing to upload to untrusted URI host {p.hostname!r} "
                           f"(expected https://{RUPLOAD_HOST})", step="upload video")
