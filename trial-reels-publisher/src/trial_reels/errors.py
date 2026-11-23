"""Typed errors and secret redaction.

Every message that can reach the terminal or a log file passes through
``redact`` so that access tokens never leak, even inside a Meta error body.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

_SECRET_PATTERNS = [
    # access_token=... in URLs or form bodies
    re.compile(r"(access_token=)[^&\s\"']+", re.IGNORECASE),
    # Authorization: Bearer xxx / OAuth xxx
    re.compile(r"((?:Bearer|OAuth)\s+)[A-Za-z0-9._\-|%]+", re.IGNORECASE),
    # Long Meta tokens (EAA..., IGAA...) wherever they appear
    re.compile(r"\b(?:EAA|IGAA|IGQ)[A-Za-z0-9_\-]{20,}"),
]

_known_secrets: set = set()


def register_secret(value: Optional[str]) -> None:
    """Remember a secret so ``redact`` removes it verbatim wherever it appears."""
    if value and len(value) >= 8:
        _known_secrets.add(value)


def redact(text: object, extra: Iterable[str] = ()) -> str:
    s = str(text)
    for secret in list(_known_secrets) + [e for e in extra if e]:
        s = s.replace(secret, "[REDACTED]")
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            s = pat.sub(lambda m: m.group(1) + "[REDACTED]", s)
        else:
            s = pat.sub("[REDACTED]", s)
    return s


class PublisherError(Exception):
    """Base class. ``str(err)`` is always safe to print."""

    def __init__(self, message: str):
        super().__init__(redact(message))


class ConfigError(PublisherError):
    pass


class DiscoveryError(PublisherError):
    pass


class VideoValidationError(PublisherError):
    pass


class StateError(PublisherError):
    pass


class LockError(PublisherError):
    pass


class MetaApiError(PublisherError):
    """A definitive error response from Meta (Graph API or rupload)."""

    def __init__(
        self,
        message: str,
        *,
        http_status: Optional[int] = None,
        error_type: Optional[str] = None,
        code: Optional[int] = None,
        subcode: Optional[int] = None,
        fbtrace_id: Optional[str] = None,
        retriable: Optional[bool] = None,
        step: str = "",
        is_transient: Optional[bool] = None,
    ):
        self.http_status = http_status
        self.is_transient = is_transient
        self.error_type = error_type
        self.code = code
        self.subcode = subcode
        self.fbtrace_id = fbtrace_id
        self.retriable = retriable
        self.step = step
        self.meta_message = redact(message)
        super().__init__(self._format())

    def _format(self) -> str:
        parts = [f"{self.step + ': ' if self.step else ''}{self.meta_message}"]
        details = []
        if self.http_status is not None:
            details.append(f"HTTP {self.http_status}")
        if self.error_type:
            details.append(f"type={self.error_type}")
        if self.code is not None:
            details.append(f"code={self.code}")
        if self.subcode is not None:
            details.append(f"error_subcode={self.subcode}")
        if self.fbtrace_id:
            details.append(f"fbtrace_id={self.fbtrace_id}")
        if details:
            parts.append("(" + ", ".join(details) + ")")
        hint = self.hint()
        if hint:
            parts.append("\nHint: " + hint)
        return " ".join(parts)

    @property
    def should_retry(self) -> bool:
        """HTTP-level retry hint for READ/create requests (429/5xx/throttling). Never used to judge
        whether a media_publish happened; see outcomes.classify_publish_error for that."""
        if self.http_status == 429 or (self.http_status or 0) >= 500:
            return True
        # Graph API throttling / temporary codes
        return self.code in (1, 2, 4, 17, 32, 341, 613)

    @property
    def is_auth_error(self) -> bool:
        return self.code in (102, 190) or self.http_status == 401

    @property
    def is_permission_error(self) -> bool:
        return self.code in (3, 10, 200) or (self.code is not None and 200 <= self.code <= 299)

    def hint(self) -> str:
        if self.code == 190:
            return ("The access token is invalid or expired. Generate a new token "
                    "(README section 'Getting a token') and update IG_ACCESS_TOKEN in .env.")
        if self.is_permission_error:
            return ("The token lacks a required permission (instagram_basic, "
                    "instagram_content_publish, pages_read_engagement, pages_show_list) "
                    "or the app is not allowed to act for this account.")
        if self.code == 9 or self.subcode == 2207042:
            return "Meta reports the publishing limit for this account has been reached. Try again later."
        if self.http_status == 429 or self.code in (4, 17, 32, 613):
            return "Rate limited by Meta. Wait a while before retrying."
        return ""


class AuthError(MetaApiError):
    pass


class TrialRejectedError(MetaApiError):
    """Meta rejected the trial Reel request. We never fall back to a normal Reel."""

    def hint(self) -> str:
        if self.subcode == 2207081 or "follower requirement" in self.meta_message.lower():
            return ("This account is not eligible for Trial Reels yet (Meta's follower requirement). "
                    "Nothing was posted. This tool will NOT retry as an ordinary Reel. Use an eligible "
                    "account, or --public if you deliberately want a regular Reel.")
        return ("Meta rejected the Trial Reel container. Nothing was posted. This tool will NOT "
                "retry as an ordinary Reel. See README 'Compatibility notes' for options.")


class ProcessingError(PublisherError):
    """Container ended in ERROR/EXPIRED, or did not finish within the poll window."""


class AmbiguousPublishError(PublisherError):
    """media_publish may or may not have succeeded. The job must become UNKNOWN, never FAILED.

    ``meta`` carries Meta's structured error when there was one; ``reason`` says why the
    outcome was judged uncertain.
    """

    def __init__(self, message: str, *, meta: Optional["MetaApiError"] = None, reason: str = ""):
        self.meta = meta
        self.reason = reason
        super().__init__(message)


class PublishRejectedError(MetaApiError):
    """media_publish was definitively rejected (documented rejection): nothing was published."""

    reason: str = ""


class AccountBlockedError(PublisherError):
    """The account has an unresolved publication attempt; all publishing to it is paused."""
