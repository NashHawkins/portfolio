"""One-time, user-confirmed URL Trial Reel attempt while ONE known job is still UNKNOWN
(``--override-unknown-job``).

Scope is deliberately tiny, like retry.py: only the exact (blocker job, container, profile, IG account,
video_url) listed in APPROVED_OVERRIDES is accepted, once ever (UNIQUE in the overrides table).

What it does NOT do:
* change the blocker job: it stays UNKNOWN, keeps its history, and keeps blocking every other job;
* relax any other lock: any other unresolved job, or any other record of this video on this account,
  still refuses the attempt (checked locally, and again atomically in begin_override_publish);
* publish a public Reel, use the resumable upload, or retry anything. An ambiguous media_publish
  leaves the NEW job UNKNOWN, which pauses the account like any other unresolved job.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from .config import Config
from .discovery import SingleFileSource
from .errors import MetaApiError, PublisherError
from .models import TRIAL, URL
from .publisher import (CONTAINER_TTL, _parse_ts, build_job, check_auth, discover_and_validate, human_size,
                        publish_job, remaining_quota)
from .state import StateStore, is_uncertain, process_lock

KIND = "URL_TRIAL_DESPITE_UNKNOWN"
CONFIRM_PHRASE = "I ACCEPT A POSSIBLE DUPLICATE"


@dataclass(frozen=True)
class ApprovedOverride:
    blocker_job_id: int
    blocker_container_id: str
    profile: str
    ig_user_id: str
    video_url: str


# Approved by the user on 2026-11-11: one URL-based Trial Reel attempt on @nashhawkins while job #3
# stays UNKNOWN. Add nothing here without a new, explicit approval.
APPROVED_OVERRIDES = {
    3: ApprovedOverride(blocker_job_id=3, blocker_container_id="18657792691044336", profile="personal",
                        ig_user_id="17840000000000002",
                        video_url="https://pub-example.r2.dev/IMG_3813_cloud.mp4"),
}


class OverrideRefused(PublisherError):
    """Raised before anything is written to Meta; nothing was posted."""


def _refuse(msg: str) -> OverrideRefused:
    return OverrideRefused(f"{msg} Nothing was posted.")


def _check_local(config: Config, store: StateStore, blocker_job_id: int, video_url: str, sha256: str):
    approved = APPROVED_OVERRIDES.get(blocker_job_id)
    if approved is None:
        raise _refuse(f"Job #{blocker_job_id} is not approved for an override (approved: "
                      f"{', '.join('#%d' % j for j in sorted(APPROVED_OVERRIDES)) or 'none'}).")
    if config.profile != approved.profile:
        raise _refuse(f"This override only works with --account {approved.profile} (got {config.account_label}).")
    if str(config.ig_user_id) != approved.ig_user_id:
        raise _refuse(f"Profile '{config.profile}' is configured for IG ID {config.ig_user_id}, but the override "
                      f"is for IG ID {approved.ig_user_id}.")
    if video_url != approved.video_url:
        raise _refuse(f"The override only allows --video-url {approved.video_url}.")
    used = store.override_for_blocker(blocker_job_id)
    if used is not None:
        raise _refuse(f"The one-time override for job #{blocker_job_id} was already used "
                      f"(override #{used['id']}, confirmed {used['confirmed_at']} UTC).")
    blocker = store.get_by_id(blocker_job_id)
    if blocker is None:
        raise _refuse(f"Job #{blocker_job_id} does not exist in the local database.")
    if blocker.state != "UNKNOWN" or not is_uncertain(blocker):
        raise _refuse(f"Job #{blocker_job_id} is {blocker.state}, not UNKNOWN; the override is only for that case. "
                      "Use the normal commands.")
    if blocker.container_id != approved.blocker_container_id or str(blocker.account_id) != approved.ig_user_id:
        raise _refuse(f"Job #{blocker_job_id} no longer matches the approved container/account.")
    if blocker.content_hash != sha256:
        raise _refuse(f"The video in the Trial-Reels folder (sha256 {sha256[:12]}…) is not job #{blocker_job_id}'s "
                      f"video ({blocker.content_hash[:12]}…).")
    others = [b for b in store.account_blockers(approved.ig_user_id) if b.job_key != blocker.job_key]
    if others:
        raise _refuse("Another job on this account is unresolved, and the override does not cover it: "
                      + ", ".join(f"#{b.id} ({b.state})" for b in others) + ".")
    dup = [b for b in store.find_blocking(approved.ig_user_id, sha256) if b.job_key != blocker.job_key]
    if dup:
        raise _refuse(f"This video is already (or may be) live on this account via job #{dup[0].id} "
                      f"({dup[0].state}); the override does not cover it.")
    return approved, blocker


def cmd_override_publish_url(config: Config, out, client_factory, input_fn, remuxer, inspector,
                             video_url: str, blocker_job_id: int) -> int:
    config.require_credentials()
    with process_lock(config.lock_path):
        store = StateStore(config.state_path)
        path, report = discover_and_validate(config, SingleFileSource(config.videos_dir))
        # Local checks first: no network call unless the override applies exactly.
        approved, blocker = _check_local(config, store, blocker_job_id, video_url, report.sha256)
        if not sys.stdin.isatty():
            raise _refuse("--override-unknown-job needs an interactive terminal for its confirmations.")

        out(f"Original:      {path}  ({human_size(report.size_bytes)}, sha256 {report.sha256[:12]}…)  [identity]")
        hv = inspector(video_url, tmp_root=config.tmp_dir, source_path=path, ffmpeg=remuxer.ffmpeg)
        from .cli import describe_hosted  # local import: cli imports this module
        describe_hosted(out, hv, path.name)
        if not hv.identity_ok:
            raise _refuse("The hosted video could not be proven to be a stream copy of the local video.")
        job = build_job(config, path, report, TRIAL, URL, hv)
        if store.get(job.job_key) is not None:
            raise _refuse(f"A URL job for this video already exists (job #{store.get(job.job_key).id}); the "
                          "override only starts a brand-new job.")

        client = client_factory(config)
        info = check_auth(client, out)  # read-only; verify_account enforces the exact IG ID
        username = info["account"].get("username") or ""
        left = remaining_quota(info["limit"])
        if left is not None and left <= 0:
            raise _refuse("Meta reports no remaining publishing quota.")
        try:  # read-only: the override is only for a blocker Meta still reports as FINISHED
            st = client.get_container_status(blocker.container_id)
        except MetaApiError as e:
            raise _refuse(f"Could not read job #{blocker.id}'s container {blocker.container_id} "
                          f"({e.meta_message}); not overriding without a fresh reading.")
        if st.status_code != "FINISHED":
            advice = {
                "PUBLISHED": f"Meta says job #{blocker.id} WAS published. Record it with --mark-published --job "
                             f"{blocker.id}; posting again would be a certain duplicate.",
                "EXPIRED": f"It expired unpublished. Use --mark-not-published --job {blocker.id} and a normal run.",
                "ERROR": f"Meta says it failed. Use --mark-not-published --job {blocker.id} and a normal run.",
            }.get(st.status_code or "", "The override is only for a FINISHED container.")
            raise _refuse(f"Job #{blocker.id}'s container {blocker.container_id} is {st.status_code!r}. {advice}")

        created = next((_parse_ts(e["at"]) for e in store.events(blocker.job_key)
                        if e["state"] == "CONTAINER_CREATED"), None)
        out("")
        out("=" * 76)
        out("  ONE-TIME OVERRIDE: new URL Trial Reel while an earlier attempt is UNKNOWN")
        out(f"  Account:        @{username} (IG ID {info['account'].get('id')})  [verified with Meta]")
        out(f"  Credentials:    {config.account_label}")
        out(f"  Original:       {path.name}  sha256 {report.sha256[:12]}…")
        out(f"  video_url:      {video_url}")
        out(f"  Posting mode:   TRIAL / {config.graduation_strategy} via video_url (never public, no upload)")
        out(f"  Unresolved:     job #{blocker.id} ({blocker.publish_mode}, {blocker.upload_method}), container "
            f"{blocker.container_id}, status FINISHED (read just now), attempts={blocker.publish_attempts}")
        if created:
            out(f"                  expires around {(created + CONTAINER_TTL).isoformat(timespec='minutes')} UTC "
                "if never published")
        out("")
        out(f"  DUPLICATE RISK: Meta has not said whether job #{blocker.id} went live. If it did, this posts")
        out(f"  the SAME video to @{username} a second time. Check the Instagram app first.")
        out(f"  Job #{blocker.id} is NOT changed: it stays UNKNOWN. This override works once, for one new job.")
        out("  If the new media_publish reply is unclear, the new job becomes UNKNOWN too, and nothing retries.")
        out("=" * 76)
        a1 = input_fn(f"Type the account name '{username}' to continue: ")
        if not username or a1.strip().lstrip("@") != username:
            out("Cancelled. Nothing was posted and no override was recorded.")
            return 1
        a2 = input_fn(f"Type '{CONFIRM_PHRASE}' to accept the duplicate-publication risk: ")
        if a2.strip() != CONFIRM_PHRASE:
            out("Cancelled. Nothing was posted and no override was recorded.")
            return 1

        ov = store.record_override(kind=KIND, blocker=blocker, blocker_container_status=st.status_code,
                                   account_profile=config.profile, account_username=username,
                                   upload_method=URL, video_url=video_url, new_job_key=job.job_key,
                                   confirmation=f"{username} / {CONFIRM_PHRASE}")
        out(f"Override #{ov['id']} recorded (job #{blocker.id} unchanged).")
        result = publish_job(job, client=client, store=store, out=out,
                             relative_path=os.path.relpath(path, config.videos_dir), remuxer=remuxer,
                             override=ov)
        if result.outcome == "PUBLISHED":
            out("media_publish succeeded. Trial status is NOT verified (see 'Confirmed mode' above): check the "
                f"Instagram app. Also look for a second copy from job #{blocker.id}.")
            return 0
        return 3
