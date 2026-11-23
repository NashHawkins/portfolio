from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


TRIAL = "TRIAL"
PUBLIC = "PUBLIC"
PUBLISH_MODES = (TRIAL, PUBLIC)
RESUMABLE = "resumable"   # default: bytes go to rupload.facebook.com
URL = "url"               # Meta fetches a publicly hosted copy from video_url
UPLOAD_METHODS = (RESUMABLE, URL)

# Trial Reels must use URL (video_url). Meta documents trial_params only there, and job #8 (resumable +
# trial_params MANUAL, 2026-11-11) went live as a NORMAL Reel. Every layer refuses TRIAL + resumable while this
# is False. Only the test suite flips it (fixture `legacy_resumable_trial`) to exercise shared machinery.
RESUMABLE_TRIAL_ALLOWED = False


def make_job_key(account_id: str, canonical_path: Path, sha256: str, mode: str = TRIAL,
                 upload_method: str = RESUMABLE, attempt: int = 1) -> str:
    """Deterministic idempotency key: same account + path + bytes + mode => same key.

    TRIAL keeps the original key format so existing state databases still match.
    Cross-mode and cross-method repost protection is NOT done via the key; see
    StateStore.find_blocking(), which matches on (account, ORIGINAL sha256) whatever the method.
    Resumable keys are unchanged; URL-method jobs get their own key (and row) so an earlier
    resumable job's record and container are never overwritten.
    """
    if attempt != 1:
        # A user-approved re-attempt of the same video (diagnostic.py). Attempt 1 keys are unchanged.
        if not isinstance(attempt, int) or attempt < 2:
            raise ValueError(f"invalid attempt {attempt!r}")
        base = make_job_key(account_id, canonical_path, sha256, mode, upload_method)
        return hashlib.sha256(f"v1|attempt={attempt}|{base}".encode("utf-8")).hexdigest()
    if mode not in PUBLISH_MODES:
        raise ValueError(f"unknown publish mode {mode!r}")
    if upload_method not in UPLOAD_METHODS:
        raise ValueError(f"unknown upload method {upload_method!r}")
    if upload_method == URL:
        return hashlib.sha256(f"v1|{mode}|url|{account_id}|{canonical_path}|{sha256}".encode("utf-8")).hexdigest()
    if mode == TRIAL:
        raw = f"v1|{account_id}|{canonical_path}|{sha256}"
    else:
        raw = f"v1|{mode}|{account_id}|{canonical_path}|{sha256}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VideoJob:
    path: Path                  # canonical (resolved) absolute path
    account_id: str
    caption: str
    graduation_strategy: Optional[str]   # MANUAL | SS_PERFORMANCE for TRIAL; None for PUBLIC
    sha256: str
    size_bytes: int
    scheduled_for: Optional[datetime] = None  # Stage 2: target publish time (tz-aware)
    # Upload-preparation hints. NOT part of the identity: the key below uses only the
    # original file, so a temporary fast-start copy can never look like a new video.
    needs_faststart: bool = False
    video_codec: Optional[str] = None
    duration_s: Optional[float] = None
    publish_mode: str = TRIAL   # TRIAL (default) | PUBLIC (explicit --public only)
    upload_method: str = RESUMABLE
    # URL method only: the hosted copy Meta fetches. sha256/size above stay the ORIGINAL's.
    video_url: Optional[str] = None
    hosted_sha256: Optional[str] = None
    hosted_size_bytes: Optional[int] = None
    identity_evidence: Optional[str] = None
    attempt: int = 1   # >1 only for a user-approved re-attempt (diagnostic.py)

    @property
    def job_key(self) -> str:
        return make_job_key(self.account_id, self.path, self.sha256, self.publish_mode, self.upload_method,
                            self.attempt)
