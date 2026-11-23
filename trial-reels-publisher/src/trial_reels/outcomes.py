"""Conservative classification of media_publish failures.

Once a media_publish request may have reached Meta, the ONLY way a job may become
FAILED is a response that Meta documents as a rejection that happens before publishing.
Everything else -- including HTTP 400 responses that describe server faults, unknown or
undocumented codes/subcodes, malformed bodies, timeouts and dropped connections -- is
UNCERTAIN, and the job becomes UNKNOWN until reconciled.

The HTTP status alone is never enough: Meta returned
HTTP 400 / OAuthException / code -1 / subcode 2207085 "An internal server error occurred"
for a publish attempt (container 18657792691044336, 2026-11-11).

Sources: Meta Instagram Platform error-code reference (fetched 2026-11-10), which lists e.g.
9007/2207027 "media not ready" and 9/2207042 "daily publishing limit", and does NOT list 2207085.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .errors import MetaApiError

NOT_PUBLISHED = "NOT_PUBLISHED"  # reliable evidence Meta rejected the request without publishing
UNCERTAIN = "UNCERTAIN"          # may or may not be live

# Meta server-fault codes: never evidence of rejection.
SERVER_FAULT_CODES = {-2, -1, 1, 2}

# Documented (code, subcode) rejections that happen before anything is published.
DEFINITIVE_REJECTIONS = {
    (9007, 2207027): "media not ready for publishing (documented 9007/2207027)",
    (9, 2207042): "daily publishing limit reached (documented 9/2207042)",
}
# Codes that reject the request at the API gateway (auth / permission / throttling) when they
# carry NO publishing subcode. With a 2207xxx subcode they are publishing-pipeline errors and
# stay UNCERTAIN (e.g. 4/2207051 has been reported to accompany posts that did go live).
# Authentication failures carry subcodes (e.g. 190/463 expired token) and are still rejected
# before the request is processed, so any subcode counts.
AUTH_REJECTION_CODES = {190: "access token invalid or expired", 102: "session invalid"}
GATEWAY_REJECTION_CODES = {
    10: "permission denied",
    200: "permission denied",
    4: "application request limit",
    17: "user request limit",
    32: "page request limit",
    613: "rate limit",
}


@dataclass(frozen=True)
class Classification:
    outcome: str
    reason: str

    @property
    def not_published(self) -> bool:
        return self.outcome == NOT_PUBLISHED


def classify_publish_error(err: Optional[MetaApiError], *, request_may_have_reached_meta: bool = True
                           ) -> Classification:
    if not request_may_have_reached_meta:
        return Classification(NOT_PUBLISHED, "connection was never established; request not sent")
    if err is None:
        return Classification(UNCERTAIN, "no error details available")
    status = err.http_status
    if status is None:
        return Classification(UNCERTAIN, "no HTTP response received after sending")
    if status >= 500:
        return Classification(UNCERTAIN, f"HTTP {status} server error")
    if not 400 <= status < 500:
        return Classification(UNCERTAIN, f"unexpected HTTP {status} without a media id")
    if err.code is None:
        return Classification(UNCERTAIN, "error response without a Meta error code (malformed or unexpected)")
    if err.is_transient is True:
        return Classification(UNCERTAIN, "Meta flagged the error as transient (is_transient=true)")
    if err.code in SERVER_FAULT_CODES:
        return Classification(UNCERTAIN, f"Meta server-fault code {err.code}"
                                         + (f"/{err.subcode}" if err.subcode is not None else ""))
    if (err.code, err.subcode) in DEFINITIVE_REJECTIONS:
        return Classification(NOT_PUBLISHED, DEFINITIVE_REJECTIONS[(err.code, err.subcode)])
    if err.code in AUTH_REJECTION_CODES:
        return Classification(NOT_PUBLISHED, f"{AUTH_REJECTION_CODES[err.code]} (code {err.code}"
                                             + (f"/{err.subcode})" if err.subcode is not None else ")"))
    if err.code in GATEWAY_REJECTION_CODES and err.subcode is None:
        return Classification(NOT_PUBLISHED, f"{GATEWAY_REJECTION_CODES[err.code]} (code {err.code})")
    return Classification(UNCERTAIN, f"code {err.code}/{err.subcode} is not a documented pre-publish "
                                     "rejection")
