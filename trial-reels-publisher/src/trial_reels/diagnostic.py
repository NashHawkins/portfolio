"""One-time diagnostic: ONE normal PUBLIC Reel on @nashhawkins while trial jobs #3 and #4 stay UNKNOWN
(``--diagnostic-public-override``).

Purpose: find out whether media_publish fails (code -1 / 2207085) only for Trial Reels or for every Reel
on this account/token. Scope is pinned in APPROVED_DIAGNOSTIC: account, profile, the exact video (sha256),
PUBLIC mode, resumable upload, and the exact set of UNKNOWN jobs it may ignore.

What it does NOT do:
* touch jobs #3/#4 (no state change, no events, no API calls on their behalf other than reading their
  container status);
* relax duplicate protection: if this video is (or may be) on the account already, it is refused;
* tolerate any other unresolved job, or a blocker that is no longer the approved UNKNOWN job;
* reuse the trial override (separate table and kind), retry anything, or send trial_params.
"""
from __future__ import annotations

import os
import sys
import dataclasses
from dataclasses import dataclass
from typing import FrozenSet, Optional

from .config import Config
from .discovery import SingleFileSource
from .errors import MetaApiError, PublisherError
from .models import PUBLIC, RESUMABLE, TRIAL
from .publisher import (build_job, check_auth, describe_video, discover_and_validate, duplicate_message,
                        find_duplicate, publish_job, remaining_quota)
from .state import ACCEPTED_PUBLISHED_DUPLICATE, StateStore, is_uncertain, process_lock

KIND = "PUBLIC_REEL_DIAGNOSTIC_2207085"
CONFIRM_PHRASE = "PUBLIC REEL, JOBS 3 AND 4 STAY UNKNOWN"


@dataclass(frozen=True)
class ApprovedDiagnostic:
    profile: str
    ig_user_id: str
    content_sha256: str
    blocker_job_ids: FrozenSet[int]
    kind: str = KIND
    confirm_phrase: str = CONFIRM_PHRASE
    # Re-attempt of an UNKNOWN job's own video: that ONE job is also skipped by the duplicate check (only
    # while it is UNKNOWN), and the new job gets attempt number ``attempt`` so it has its own key and row.
    allowed_duplicate_job_id: Optional[int] = None
    attempt: int = 1
    duplicate_phrase: Optional[str] = None
    mode: str = PUBLIC
    # A PUBLISHED job of the same video the user has accepted posting over (they deleted that Reel). Pinned
    # by job ID and its IG media ID; it is recorded on the override and excused by the duplicate check only.
    published_duplicate_job_id: Optional[int] = None
    published_duplicate_media_id: Optional[str] = None


# Approved by the user on 2026-11-11 for the 2207085 investigation. Add nothing here without a new,
# explicit approval.
APPROVED_DIAGNOSTIC = ApprovedDiagnostic(
    profile="personal", ig_user_id="17840000000000002",
    content_sha256="395f9eb51ddbedc7d6f49f7ed8404beb7ee07de1170637cd28fddcde6760ff7f",  # IMG_2248.MOV
    blocker_job_ids=frozenset({3, 4}),
)

REATTEMPT_KIND = "PUBLIC_REEL_REATTEMPT_JOB5_AFTER_PAGE_REMOVAL"
REATTEMPT_CONFIRM_PHRASE = "PUBLIC REEL, JOBS 3 4 AND 5 STAY UNKNOWN"
REATTEMPT_DUPLICATE_PHRASE = "I ACCEPT A POSSIBLE DUPLICATE OF JOB 5"

# Approved by the user on 2026-11-11 (after removing the stale Facebook Page and issuing a new token):
# re-attempt job #5's exact video as ONE normal PUBLIC Reel, accepting that job #5 may already be live.
APPROVED_REATTEMPT = ApprovedDiagnostic(
    profile="personal", ig_user_id="17840000000000002",
    content_sha256="395f9eb51ddbedc7d6f49f7ed8404beb7ee07de1170637cd28fddcde6760ff7f",  # IMG_2248.MOV
    blocker_job_ids=frozenset({3, 4, 5}),
    kind=REATTEMPT_KIND, confirm_phrase=REATTEMPT_CONFIRM_PHRASE,
    allowed_duplicate_job_id=5, attempt=2, duplicate_phrase=REATTEMPT_DUPLICATE_PHRASE,
)

TRIAL_REATTEMPT_KIND = "TRIAL_REEL_MANUAL_IMG_2248_AFTER_PAGE_REMOVAL"
TRIAL_REATTEMPT_CONFIRM_PHRASE = "TRIAL REEL, JOBS 3 4 AND 5 STAY UNKNOWN"
TRIAL_REATTEMPT_DUPLICATE_PHRASE = "I ACCEPT A POSSIBLE DUPLICATE OF JOBS 5 AND 7"

# Approved by the user on 2026-11-11 after job #7 (public re-attempt) PUBLISHED and they deleted that Reel:
# the same video as ONE Trial Reel (MANUAL graduation), accepting that #5 may be live and #7 was posted.
APPROVED_TRIAL_REATTEMPT = ApprovedDiagnostic(
    profile="personal", ig_user_id="17840000000000002",
    content_sha256="395f9eb51ddbedc7d6f49f7ed8404beb7ee07de1170637cd28fddcde6760ff7f",  # IMG_2248.MOV
    blocker_job_ids=frozenset({3, 4, 5}),
    kind=TRIAL_REATTEMPT_KIND, confirm_phrase=TRIAL_REATTEMPT_CONFIRM_PHRASE,
    allowed_duplicate_job_id=5, attempt=1, duplicate_phrase=TRIAL_REATTEMPT_DUPLICATE_PHRASE, mode=TRIAL,
    published_duplicate_job_id=7, published_duplicate_media_id="18084194930405612",
)


class DiagnosticRefused(PublisherError):
    """Raised before anything is written to Meta; nothing was posted."""


def _refuse(msg: str) -> DiagnosticRefused:
    return DiagnosticRefused(f"{msg} Nothing was posted.")


def _check_local(config: Config, store: StateStore, sha256: str, approved: ApprovedDiagnostic):
    if config.profile != approved.profile:
        raise _refuse(f"This diagnostic only works with --account {approved.profile} (got {config.account_label}).")
    if str(config.ig_user_id) != approved.ig_user_id:
        raise _refuse(f"Profile '{config.profile}' is configured for IG ID {config.ig_user_id}, not "
                      f"{approved.ig_user_id}.")
    if sha256 != approved.content_sha256:
        raise _refuse(f"The video in the Trial-Reels folder (sha256 {sha256[:12]}…) is not the video approved for "
                      f"this diagnostic ({approved.content_sha256[:12]}…).")
    used = store.diagnostic_override_by_kind(approved.kind)
    if used is not None:
        raise _refuse(f"The one-time diagnostic override was already used (#{used['id']}, confirmed "
                      f"{used['confirmed_at']} UTC).")
    current = store.account_blockers(approved.ig_user_id)
    ids = {b.id for b in current}
    extra = [b for b in current if b.id not in approved.blocker_job_ids]
    if extra:
        raise _refuse("An unresolved job not covered by this diagnostic exists: "
                      + ", ".join(f"#{b.id} ({b.state})" for b in extra) + ".")
    blockers = []
    for jid in sorted(approved.blocker_job_ids):
        rec = store.get_by_id(jid)
        if rec is None or str(rec.account_id) != approved.ig_user_id:
            raise _refuse(f"Job #{jid} is missing or not on IG ID {approved.ig_user_id}.")
        if jid not in ids:
            # Resolved since approval: it no longer blocks, so it needs no exception.
            continue
        if rec.state != "UNKNOWN" or not is_uncertain(rec):
            raise _refuse(f"Job #{jid} is {rec.state}; this diagnostic only covers UNKNOWN jobs.")
        blockers.append(rec)
    allowed = None
    if approved.allowed_duplicate_job_id is not None:
        jid = approved.allowed_duplicate_job_id
        allowed = next((b for b in blockers if b.id == jid), None)
        if jid not in approved.blocker_job_ids or allowed is None:
            raise _refuse(f"Job #{jid} is no longer UNKNOWN, so this re-attempt no longer applies. Use the normal "
                          "--public path.")
        if (allowed.content_hash != approved.content_sha256 or allowed.publish_mode != PUBLIC
                or allowed.upload_method != RESUMABLE):
            raise _refuse(f"Job #{jid} is not a PUBLIC resumable job of the approved video.")
    published = None
    if approved.published_duplicate_job_id is not None:
        jid = approved.published_duplicate_job_id
        published = store.get_by_id(jid)
        if (published is None or str(published.account_id) != approved.ig_user_id or published.state != "PUBLISHED"
                or published.content_hash != approved.content_sha256
                or published.media_id != approved.published_duplicate_media_id):
            raise _refuse(f"Job #{jid} is not the approved PUBLISHED job of this video (media "
                          f"{approved.published_duplicate_media_id}).")
    return blockers, allowed, published


def cmd_diagnostic_public(config: Config, out, client_factory, input_fn, remuxer, reattempt: bool = False,
                          trial_reattempt: bool = False) -> int:
    # read at call time
    approved = (APPROVED_TRIAL_REATTEMPT if trial_reattempt else APPROVED_REATTEMPT if reattempt
                else APPROVED_DIAGNOSTIC)
    config.require_credentials()
    with process_lock(config.lock_path):
        remuxer.cleanup_stale()
        store = StateStore(config.state_path)
        path, report = discover_and_validate(config, SingleFileSource(config.videos_dir))
        blockers, allowed, published = _check_local(config, store, report.sha256, approved)
        job = build_job(config, path, report, approved.mode, RESUMABLE)
        if approved.mode == TRIAL and (job.publish_mode != TRIAL or job.graduation_strategy != "MANUAL"):
            raise _refuse(f"This Trial Reel test is approved for graduation_strategy MANUAL only "
                          f"(configured: {job.graduation_strategy}).")
        if approved.attempt != 1:
            job = dataclasses.replace(job, attempt=approved.attempt)
        if store.get(job.job_key) is not None:
            raise _refuse(f"A {approved.mode} job for this video already exists (job #{store.get(job.job_key).id}).")
        # Full duplicate protection; the only exceptions are the approved jobs of this same video.
        dup = find_duplicate(store, job, ignore_key=allowed.job_key if allowed else None,
                             ignore_keys=(published.job_key,) if published else ())
        if dup is not None:
            raise PublisherError(duplicate_message(dup))
        if not sys.stdin.isatty():
            raise _refuse("--diagnostic-public-override needs an interactive terminal for its confirmations.")
        remuxer.ensure_possible(job)  # stop before any network call if ffmpeg is needed but missing

        client = client_factory(config)
        info = check_auth(client, out)  # read-only; verify_account enforces the exact IG ID
        username = info["account"].get("username") or ""
        left = remaining_quota(info["limit"])
        if left is not None and left <= 0:
            raise _refuse("Meta reports no remaining publishing quota.")
        statuses = []
        for rec in blockers:  # read-only, recorded as evidence; a PUBLISHED blocker must be resolved first
            try:
                st = client.get_container_status(rec.container_id) if rec.container_id else None
            except MetaApiError as e:
                raise _refuse(f"Could not read job #{rec.id}'s container {rec.container_id} ({e.meta_message}).")
            code = st.status_code if st else "NO_CONTAINER"
            if code == "PUBLISHED":
                raise _refuse(f"Meta says job #{rec.id}'s container is PUBLISHED. Record it with --mark-published "
                              f"--job {rec.id} first; this diagnostic only covers unresolved jobs.")
            statuses.append((rec, code))

        if published is not None:  # not read from Meta: its Reel was published, then deleted by the user
            statuses.append((published, ACCEPTED_PUBLISHED_DUPLICATE))
        trial = approved.mode == TRIAL
        describe_video(out, config, path, report, ffmpeg_available=remuxer.available, mode=approved.mode)
        out("")
        out("=" * 76)
        out("  ONE-TIME DIAGNOSTIC: TRIAL Reel (MANUAL) while earlier jobs are UNKNOWN" if trial else
            "  ONE-TIME DIAGNOSTIC: normal PUBLIC Reel while trial jobs are UNKNOWN")
        out(f"  Account:       @{username} (IG ID {info['account'].get('id')})  [verified with Meta]")
        out(f"  Credentials:   {config.account_label}")
        out(f"  File:          {path.name}  sha256 {report.sha256[:12]}…")
        if trial:
            out('  Container:     media_type=REELS, upload_type=resumable, trial_params={"graduation_strategy":"MANUAL"}')
            out("  Visibility:    TRIAL: shown to non-followers first. Never falls back to a normal Reel.")
        else:
            out("  Container:     media_type=REELS, upload_type=resumable  (NO trial_params)")
            out("  Visibility:    PUBLIC: shown to your followers and potentially anyone on Instagram.")
        for rec, code in statuses:
            if rec is published:
                out(f"  Posted before: job #{rec.id} PUBLISHED as media {rec.media_id} (you deleted it). Not changed.")
                continue
            out(f"  Stays UNKNOWN: job #{rec.id} ({rec.publish_mode}, {rec.relative_path}), container "
                f"{rec.container_id} status {code} (read just now). Not changed by this run.")
        if published is not None:
            out(f"  DUPLICATE RISK: job #{published.id} already posted this SAME video (deleted by you).")
        if allowed is not None:
            out(f"  DUPLICATE RISK: job #{allowed.id} tried this SAME video; if it actually went live, this posts a")
            out(f"                 second copy. New job is attempt {approved.attempt}; job #{allowed.id} is not changed.")
        out("  This works once. An unclear media_publish reply makes the NEW job UNKNOWN; nothing retries.")
        out("=" * 76)
        a1 = input_fn(f"Type the account name '{username}' to continue: ")
        if not username or a1.strip().lstrip("@") != username:
            out("Cancelled. Nothing was posted and no override was recorded.")
            return 1
        a2 = input_fn(f"Type '{approved.confirm_phrase}' to authorize this {approved.mode} Reel: "
                      if trial else f"Type '{approved.confirm_phrase}' to authorize a publicly visible Reel: ")
        if a2.strip() != approved.confirm_phrase:
            out("Cancelled. Nothing was posted and no override was recorded.")
            return 1
        confirmation = f"{username} / {approved.confirm_phrase}"
        if allowed is not None:
            a3 = input_fn(f"Type '{approved.duplicate_phrase}' to accept the duplicate risk: ")
            if not approved.duplicate_phrase or a3.strip() != approved.duplicate_phrase:
                out("Cancelled. Nothing was posted and no override was recorded.")
                return 1
            confirmation += f" / {approved.duplicate_phrase}"

        ov = store.record_diagnostic_override(
            kind=approved.kind, account_id=approved.ig_user_id, account_profile=config.profile,
            account_username=username, content_hash=report.sha256, publish_mode=approved.mode,
            upload_method=RESUMABLE, new_job_key=job.job_key,
            confirmation=confirmation, blockers=statuses,
            allowed_duplicate_job_key=allowed.job_key if allowed else None)
        out(f"Diagnostic override #{ov['id']} recorded (jobs {', '.join('#%d' % r.id for r, _ in statuses) or 'none'} "
            "unchanged).")
        result = publish_job(job, client=client, store=store, out=out,
                             relative_path=os.path.relpath(path, config.videos_dir), remuxer=remuxer,
                             diagnostic=ov)
        if result.outcome == "PUBLISHED":
            out("media_publish succeeded. Trial status is NOT verified (see 'Confirmed mode' above)." if trial else
                "Posted as a regular PUBLIC Reel. Result: normal publishing works for this account and token.")
            return 0
        return 3
