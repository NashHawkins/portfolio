"""Publish-one orchestration: discovery -> validation -> state -> Meta transport.

Stage 2 reuses ``publish_job`` per scheduled job; only discovery/scheduling change.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .config import GRAPH_HOST, RUPLOAD_HOST, Config
from .discovery import VideoSource
from .errors import (AccountBlockedError, AmbiguousPublishError, MetaApiError, PublisherError,
                     PublishRejectedError, StateError, redact)
from .meta_client import MetaClient, reel_container_params, url_reel_container_body
from . import models as _models
from .models import PUBLIC, RESUMABLE, TRIAL, URL, VideoJob
from .remux import Remuxer
from .state import ACCEPTED_PUBLISHED_DUPLICATE, JobRecord, StateStore, is_blocking, is_uncertain
from .validation import VideoReport, validate_video

log = logging.getLogger(__name__)
Out = Callable[[str], None]


@dataclass
class PublishResult:
    outcome: str                    # PUBLISHED | ALREADY_PUBLISHED | NEEDS_RECONCILE | UNKNOWN
    record: Optional[JobRecord]
    message: str = ""


def human_size(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


TRIAL_NEEDS_URL = ("Trial Reels cannot use resumable upload: Meta documents trial_params only for video_url "
                   "uploads, and job #8 (resumable, TRIAL/MANUAL) went live as a NORMAL Reel. Use "
                   "--upload-method url --video-url <hosted copy>. Nothing was posted.")


def build_job(config: Config, path: Path, report: VideoReport, mode: str = TRIAL,
              upload_method: str = RESUMABLE, hosted=None) -> Optional[VideoJob]:
    """Identity (path, sha256, size) is ALWAYS the original local video. In URL mode the hosted copy is
    only the transport, like the fast-start temp copy in resumable mode."""
    if mode == TRIAL and upload_method == RESUMABLE and not _models.RESUMABLE_TRIAL_ALLOWED:
        raise PublisherError(TRIAL_NEEDS_URL)
    if not config.ig_user_id:
        return None
    url_fields = {}
    if upload_method == URL:
        if hosted is None or not hosted.identity_ok:
            raise PublisherError("URL mode needs a hosted video whose video stream matches the original.")
        from .hosted import identity_evidence
        url_fields = dict(video_url=hosted.url, hosted_sha256=hosted.sha256,
                          hosted_size_bytes=hosted.content_length,
                          identity_evidence=identity_evidence(hosted, path.name))
    return VideoJob(path=path.resolve(), account_id=config.ig_user_id, caption=config.default_caption,
                    graduation_strategy=config.graduation_strategy if mode == TRIAL else None,
                    sha256=report.sha256, size_bytes=report.size_bytes,
                    needs_faststart=report.needs_faststart and upload_method == RESUMABLE,
                    video_codec=report.video_codec, duration_s=report.duration_s, publish_mode=mode,
                    upload_method=upload_method, **url_fields)


def mode_label(mode: str, strategy: Optional[str]) -> str:
    return f"TRIAL / {strategy}" if mode == TRIAL else "PUBLIC (regular Reel, no trial_params)"


def find_duplicate(store: StateStore, job: VideoJob, ignore_key: Optional[str] = None,
                   ignore_keys=()) -> Optional[JobRecord]:
    """Another job (other mode, or same bytes at another path) that is or may be live."""
    for rec in store.find_blocking(job.account_id, job.sha256):
        if rec.job_key not in (job.job_key, ignore_key, *ignore_keys):
            return rec
    return None


def account_is_known(account_id: Optional[str]) -> bool:
    return bool(account_id) and str(account_id).strip().isdigit()


def duplicate_message(rec: JobRecord) -> str:
    what = "already published" if rec.state == "PUBLISHED" else f"in state {rec.state} (may be live)"
    media = f", IG media ID {rec.media_id}" if rec.media_id else ""
    link = f" {rec.permalink}" if rec.permalink else ""
    if not account_is_known(rec.account_id):
        return (f"This video was {what} as a {rec.publish_mode} Reel from {rec.relative_path}{media}, but "
                f"that record has no valid Instagram account ID ({rec.account_id!r}).{link}\n"
                "It might belong to this account, so it blocks this video on EVERY account. Nothing was "
                "posted. See README 'Records with an unknown account'.")
    who = f"@{rec.account_username} " if rec.account_username else ""
    return (f"This video was {what} to {who}(IG ID {rec.account_id}) as a {rec.publish_mode} Reel "
            f"from {rec.relative_path}{media}.{link}\n"
            "Refusing to post the same video to the same account again (in either mode). Nothing was "
            "posted. Use a different video, or a different account." +
            (" For an uncertain job, run --reconcile first." if rec.state != "PUBLISHED" else ""))


def verify_account(client: MetaClient, expected_id: str) -> dict:
    """Ask Meta who the token belongs to; abort unless it is exactly the configured IG_USER_ID."""
    acct = client.get_account()
    got = str(acct.get("id", "")).strip()
    if got != str(expected_id):
        raise AccountMismatchError(
            f"Account mismatch: the token for {client.config.account_label} resolves to IG ID {got or '?'} "
            f"(@{acct.get('username', '?')}), but IG_USER_ID is {expected_id}. Nothing was posted and no "
            "media container was created. Fix IG_USER_ID / IG_ACCESS_TOKEN in that file.")
    return acct


class AccountMismatchError(PublisherError):
    pass


def describe_url_request(out: Out, config: Config, mode: str, video_url: str) -> None:
    """URL method: the exact Meta requests a real run would send (token is never shown)."""
    import json
    body = url_reel_container_body(config.default_caption, mode,
                                   config.graduation_strategy if mode == TRIAL else None, video_url)
    ig = config.ig_user_id or "<IG_USER_ID>"
    v = config.api_version
    out("Requests a real run would send (token goes only in the Authorization header, never shown):")
    out(f"  1. POST {GRAPH_HOST}/{v}/{ig}/media")
    out("       Content-Type: application/json")
    out(f"       body: {json.dumps(body)}")
    out("       trial_params:  INCLUDED -> " + json.dumps(body["trial_params"]) if "trial_params" in body
        else "       trial_params:  OMITTED")
    out("       upload_type:   OMITTED (URL mode: Meta fetches video_url itself; no rupload, no bytes sent)")
    out(f"  2. GET  {GRAPH_HOST}/{v}/<container id>?fields=status_code,status   (until FINISHED)")
    out(f"  3. POST {GRAPH_HOST}/{v}/{ig}/media_publish")
    out("       creation_id=<container id>   (sent once, never retried)")


def describe_request(out: Out, config: Config, mode: str, upload_bytes_note: str) -> None:
    """Print the exact Meta requests a real run would send (token is never shown)."""
    params = reel_container_params(config.default_caption, mode,
                                   config.graduation_strategy if mode == TRIAL else None)
    ig = config.ig_user_id or "<IG_USER_ID>"
    v = config.api_version
    out("Requests a real run would send (token goes only in the Authorization header, never shown):")
    out(f"  1. POST {GRAPH_HOST}/{v}/{ig}/media")
    for k, val in params.items():
        out(f"       {k}={val}")
    if mode == PUBLIC:
        out("       trial_params: OMITTED -> regular Reel, visible to followers")
    out(f"  2. POST https://{RUPLOAD_HOST}/ig-api-upload/{v}/<container id>")
    out(f"       headers: offset: 0, file_size: <bytes>; body: {upload_bytes_note}")
    out(f"  3. GET  {GRAPH_HOST}/{v}/<container id>?fields=status_code,status   (until FINISHED)")
    out(f"  4. POST {GRAPH_HOST}/{v}/{ig}/media_publish")
    out("       creation_id=<container id>")


def discover_and_validate(config: Config, source: VideoSource,
                          validator=validate_video) -> Tuple[Path, VideoReport]:
    videos = source.discover()
    if len(videos) != 1:  # Stage 1 contract
        raise PublisherError(f"expected exactly one video, got {len(videos)}")
    path = videos[0]
    return path, validator(path)


def describe_video(out: Out, config: Config, path: Path, report: VideoReport,
                   ffmpeg_available: Optional[bool] = None, mode: str = TRIAL) -> None:
    out(f"Source:        {path}")
    out(f"Size:          {human_size(report.size_bytes)}  sha256 {report.sha256[:12]}…")
    if report.metadata_checked:
        dims = f"{report.width}x{report.height}" if report.width else "?"
        fps = f"{report.fps:.2f} fps" if report.fps else "? fps"
        dur = f"{report.duration_s:.1f}s" if report.duration_s is not None else "?"
        out(f"Video:         {dur}, {dims}, {fps}, {report.video_codec}/{report.audio_codec or 'no audio'}")
    if report.needs_faststart:
        if ffmpeg_available is False:
            out("Upload copy:   NEEDED but ffmpeg is not installed: the 'moov' atom is at the end of the "
                "file and Meta requires it at the front. A real run will stop safely before posting.")
        else:
            out("Upload copy:   'moov' atom is at the end, so a temporary fast-start copy will be made "
                "with ffmpeg (-c copy, no re-encode). The original stays untouched.")
    out(f"Posting mode:  {mode_label(mode, config.graduation_strategy)}")
    out(f"Caption:       {config.default_caption!r}" if config.default_caption else "Caption:       (empty)")
    for w in report.warnings:
        out(f"Warning:       {w}")


# ------------------------------------------------------------------ read-only commands
def check_auth(client: MetaClient, out: Out) -> dict:
    out(f"Credentials:   {client.config.account_label}")
    acct = verify_account(client, client.config.ig_user_id)
    out(f"Account:       @{acct.get('username', '?')} (IG ID {acct.get('id')})"
        + (f" — {acct['name']}" if acct.get("name") else ""))

    perms = client.get_token_permissions()
    if perms is None:
        out("Permissions:   not listable with this token type (Page token?). Check it in Meta's "
            "Access Token Debugger.")
    else:
        granted = sorted(p["permission"] for p in perms if p.get("status") == "granted")
        out(f"Permissions:   {', '.join(granted) or '(none)'}")
        missing = [p for p in ("instagram_basic", "instagram_content_publish") if p not in granted]
        if missing:
            raise PublisherError(f"Token is missing required permission(s): {', '.join(missing)}. "
                                 "Re-generate it with those scopes (README).")
        for p in ("pages_read_engagement", "pages_show_list"):
            if p not in granted:
                out(f"Warning:       {p} not granted; Meta lists it for the Facebook Login flow.")

    limit = client.get_publishing_limit()
    usage = limit.get("quota_usage")
    total = (limit.get("config") or {}).get("quota_total")
    duration = (limit.get("config") or {}).get("quota_duration")
    if usage is not None and total is not None:
        span = f" per {int(duration) // 3600}h" if duration else ""
        out(f"Publish quota: {usage} of {total} used{span} (general API quota; it does not guarantee "
            "Trial Reels eligibility)")
    else:
        out(f"Publish quota: unexpected response {redact(limit)}")
    out("Token expiry:  not checked here; check it in Meta's Access Token Debugger (README setup step 5).")
    return {"account": acct, "limit": limit}


def remaining_quota(limit: dict) -> Optional[int]:
    usage = limit.get("quota_usage")
    total = (limit.get("config") or {}).get("quota_total")
    if usage is None or total is None:
        return None
    return int(total) - int(usage)


# ------------------------------------------------------------------ publish
def account_block_message(blockers: List[JobRecord], account_id: str) -> str:
    lines = [f"Publishing to IG ID {account_id} is PAUSED: this account has an unresolved publication "
             "attempt, so nothing more will be posted to it until that is resolved."]
    for b in blockers:
        owner = b.account_id if b.account_id and str(b.account_id).isdigit() else f"unknown ({b.account_id!r})"
        lines.append(f"  job #{b.id}  {b.relative_path}  {b.publish_mode}  state={b.state}  "
                     f"container={b.container_id or '-'}  publish attempted {b.publish_started_at or '?'} UTC  "
                     f"account={owner}")
    lines.append("Read-only checks still work. Resolve it with:  python publish.py --account <profile> --reconcile")
    return "\n".join(lines)


def check_account_not_blocked(store: StateStore, account_id: str, own_job_key: Optional[str] = None,
                              ignore_key: Optional[str] = None, ignore_keys=()) -> None:
    skip = {own_job_key, ignore_key, *ignore_keys}
    blockers = [b for b in store.account_blockers(account_id) if b.job_key not in skip]
    if blockers:
        raise AccountBlockedError(account_block_message(blockers, account_id))


def publish_job(job: VideoJob, *, client: MetaClient, store: StateStore, out: Out,
                relative_path: str, remuxer: Remuxer, override: Optional[dict] = None,
                diagnostic: Optional[dict] = None, test: Optional[dict] = None) -> PublishResult:
    """``override``: a recorded one-time trial override row (override.py); its single blocker is skipped
    for both the pause and the duplicate check (it is the same video).
    ``diagnostic``: a recorded one-time diagnostic override (diagnostic.py); its blockers are skipped for
    the account pause, plus its single ``allowed_duplicate_job_key`` (if any, and only while that job is
    UNKNOWN) for the duplicate check. Either way the job must be brand new and bound to the override."""
    if job.publish_mode == TRIAL and job.upload_method == RESUMABLE and not _models.RESUMABLE_TRIAL_ALLOWED:
        raise PublisherError(TRIAL_NEEDS_URL)
    ignore = None
    dup_ignore = None
    dup_published = ()
    ignore_keys = ()
    for ov in (override, diagnostic, test):
        if ov is None:
            continue
        if ov.get("new_job_key") != job.job_key or ov.get("publish_claimed_at"):
            raise PublisherError("Override is not bound to this job or was already used. Nothing was posted.")
        if store.get(job.job_key) is not None:
            raise PublisherError("An override may only start a brand-new job. Nothing was posted.")
    if sum(x is not None for x in (override, diagnostic, test)) > 1:
        raise PublisherError("Only one override per attempt. Nothing was posted.")
    if override is not None:
        ignore = override["blocker_job_key"]
    if diagnostic is not None:
        ignore_keys = tuple(diagnostic["blocker_job_keys"])
        # A PUBLISHED job the user explicitly accepted posting over (recorded with that marker).
        dup_published = tuple(b["blocker_job_key"] for b in diagnostic["blockers"]
                              if b["blocker_container_status"] == ACCEPTED_PUBLISHED_DUPLICATE
                              and (r := store.get(b["blocker_job_key"])) is not None and r.state == "PUBLISHED")
        allowed = diagnostic.get("allowed_duplicate_job_key")
        if allowed is not None:
            if allowed not in ignore_keys:
                raise PublisherError("The allowed duplicate is not one of the override's blockers. Nothing was posted.")
            prior = store.get(allowed)
            if prior is None or prior.state != "UNKNOWN":
                raise PublisherError("The job this re-attempt duplicates is no longer UNKNOWN. Nothing was posted.")
            dup_ignore = allowed
    rec = store.get(job.job_key)
    if rec and rec.state == "PUBLISHED":
        out(f"Already published by this tool ({rec.publish_mode}): IG media ID {rec.media_id or 'not provided by Meta'}")
        if rec.permalink:
            out(f"Permalink: {rec.permalink}")
        out("Nothing was posted. (Change the file or account to publish something new.)")
        return PublishResult("ALREADY_PUBLISHED", rec)
    if rec and is_uncertain(rec):
        out(f"A previous publish attempt for this video is unresolved (job #{rec.id}, state {rec.state}, "
            f"container {rec.container_id}, attempted {rec.publish_started_at} UTC).")
        out("It may already be live. Not posting again, and this account is paused until it is resolved.")
        out("Run:  python publish.py --account <profile> --reconcile")
        return PublishResult("NEEDS_RECONCILE", rec)
    if test is None:  # --test-mode: the user confirmed ignoring locks and duplicates for this one attempt
        check_account_not_blocked(store, job.account_id, own_job_key=job.job_key, ignore_key=ignore,
                                  ignore_keys=ignore_keys)
        dup = find_duplicate(store, job, ignore_key=ignore or dup_ignore, ignore_keys=dup_published)
        if dup is not None:
            raise PublisherError(duplicate_message(dup))
    if job.account_id != client.config.ig_user_id:
        raise AccountMismatchError("Job account does not match the configured account; nothing was posted.")
    # Verify identity immediately before any write, even if the CLI already did a preflight.
    acct = verify_account(client, job.account_id)

    if rec is None:
        rec = store.insert(job_key=job.job_key, account_id=job.account_id, source_path=str(job.path),
                           relative_path=relative_path, content_hash=job.sha256,
                           size_bytes=job.size_bytes, graduation_strategy=job.graduation_strategy,
                           publish_mode=job.publish_mode, account_profile=client.config.profile,
                           account_username=acct.get("username"),
                           scheduled_for=job.scheduled_for.isoformat() if job.scheduled_for else None,
                           upload_method=job.upload_method, video_url=job.video_url, attempt=job.attempt)
    else:
        out(f"Previous attempt ended {rec.state} ({rec.failure_phase}); starting a fresh container "
            "(never reusing a partial one).")
        rec = store.update(job.job_key, "VALIDATED", detail="retry with fresh container",
                           expect_state=rec.state, container_id=None, upload_uri=None, last_error=None,
                           account_profile=client.config.profile, account_username=acct.get("username"))

    ctx = _Ctx(job, store, out)
    ctx.override_id = override["id"] if override is not None else None
    ctx.diagnostic_id = diagnostic["id"] if diagnostic is not None else None
    ctx.test_id = test["id"] if test is not None else None

    if job.upload_method == URL:
        # Nothing to prepare or upload: Meta fetches the hosted copy. Record which copy and why it is
        # this video (stream identity), then create the container and go straight to polling.
        store.update(job.job_key, "VALIDATED", upload_sha256=job.hosted_sha256,
                     upload_size_bytes=job.hosted_size_bytes, upload_remuxed=0, video_url=job.video_url,
                     detail=f"url method; identity: {job.identity_evidence}")
        try:
            container = client.create_reel_container_from_url(job.caption, job.publish_mode,
                                                               job.graduation_strategy, job.video_url)
        except Exception as e:
            ctx.fail_pre_publish(e, "create_container")
            raise
        ctx.container_id = container.id
        store.update(job.job_key, "CONTAINER_CREATED", container_id=container.id, upload_uri=None,
                     detail="container created from video_url (no upload)")
        out(f"Container created: {container.id} ({mode_label(job.publish_mode, job.graduation_strategy)}, "
            "video_url)")
        out("Meta is fetching and processing the hosted video…")
        return _poll_and_publish(job, container, client=client, ctx=ctx)

    # Prepare upload bytes BEFORE any API write. Identity stays the original file (job_key).
    try:
        if job.needs_faststart:
            out("Making a temporary fast-start copy for upload (original untouched)…")
        prepared = remuxer.prepare(job)
    except BaseException as e:
        ctx.fail_pre_publish(e, "prepare_upload")
        raise
    store.update(job.job_key, "VALIDATED", upload_sha256=prepared.sha256,
                 upload_size_bytes=prepared.size_bytes, upload_remuxed=int(prepared.remuxed),
                 detail="upload copy prepared" if prepared.remuxed else "uploading original")

    try:
        container = _create_and_upload(job, prepared, client=client, ctx=ctx)
    finally:
        # Meta holds the bytes after upload (or the attempt failed): the temp copy is no longer
        # needed either way. State keeps the original path/hash and the uploaded copy's hash.
        remuxer.cleanup(prepared)

    return _poll_and_publish(job, container, client=client, ctx=ctx)


class _Ctx:
    """Shared failure bookkeeping for one publish attempt."""

    def __init__(self, job: VideoJob, store: StateStore, out: Out):
        self.job, self.store, self.out = job, store, out
        self.container_id: Optional[str] = None
        self.override_id: Optional[int] = None
        self.diagnostic_id: Optional[int] = None
        self.test_id: Optional[int] = None

    def log(self, step, phase, outcome, err=None, **kw):
        self.store.log_api_call(job_key=self.job.job_key, account_id=self.job.account_id,
                                content_hash=self.job.sha256, step=step, phase=phase, outcome=outcome,
                                err=err, container_id=self.container_id, **kw)

    def fail_pre_publish(self, err: BaseException, step: str) -> None:
        """Failure before media_publish was sent: definitively nothing published."""
        self.store.update(self.job.job_key, "FAILED", failure_phase="PRE_PUBLISH", last_error=str(err),
                          detail=f"{type(err).__name__} during {step} (before media_publish)")
        self.log(step, "PRE_PUBLISH", "FAILED", err, classification="media_publish was never sent")


def _create_and_upload(job, prepared, *, client, ctx):
    store, out = ctx.store, ctx.out
    # C. create trial container
    try:
        # Mode is fixed for the whole job: a rejected TRIAL request is never retried as PUBLIC.
        container = client.create_reel_container(job.caption, job.publish_mode, job.graduation_strategy)
    except Exception as e:
        ctx.fail_pre_publish(e, "create_container")
        raise
    ctx.container_id = container.id
    store.update(job.job_key, "CONTAINER_CREATED", container_id=container.id,
                 upload_uri=container.upload_uri)
    out(f"Container created: {container.id} ({mode_label(job.publish_mode, job.graduation_strategy)})")

    # D. upload bytes
    store.update(job.job_key, "UPLOADING")
    out(f"Uploading {human_size(prepared.size_bytes)} to Meta…")
    try:
        client.upload_video(container.upload_uri, prepared.path, prepared.size_bytes)
    except Exception as e:
        ctx.fail_pre_publish(e, "upload")
        raise
    store.update(job.job_key, "UPLOADED")
    return container


def _poll_and_publish(job, container, *, client, ctx):
    store, out = ctx.store, ctx.out
    # E. poll
    seen = []

    def on_status(s):
        if not seen or seen[-1] != s.status_code:
            out(f"Processing: {s.status_code}")
        seen.append(s.status_code)

    try:
        client.wait_until_finished(container.id, on_status=on_status)
    except Exception as e:
        ctx.fail_pre_publish(e, "poll")
        raise
    store.update(job.job_key, "READY")

    # Diagnostics captured right before the attempt (read-only, best effort).
    quota_usage = quota_total = None
    try:
        limit = client.get_publishing_limit()
        quota_usage = limit.get("quota_usage")
        quota_total = (limit.get("config") or {}).get("quota_total")
    except MetaApiError as e:
        log.warning("could not read publishing quota before publish: %s", e)
    diag = dict(container_status_before=seen[-1] if seen else None,
                quota_usage_before=_int(quota_usage), quota_total_before=_int(quota_total))

    # F. publish exactly once
    if ctx.override_id is not None:  # every variant is committed before the request leaves
        started = store.begin_override_publish(job.job_key, ctx.override_id)
    elif ctx.diagnostic_id is not None:
        started = store.begin_diagnostic_publish(job.job_key, ctx.diagnostic_id)
    elif ctx.test_id is not None:
        started = store.begin_test_publish(job.job_key, ctx.test_id)
    else:
        started = store.begin_publish(job.job_key)
    diag["attempted_at"] = started.publish_started_at
    try:
        media_id = client.publish_container(container.id)
    except AmbiguousPublishError as e:
        rec = store.update(job.job_key, "UNKNOWN", last_error=str(e),
                           detail=f"media_publish outcome uncertain: {e.reason}")
        ctx.log("publish", "PUBLISH", "UNCERTAIN", e.meta or e, classification=e.reason, **diag)
        _report_uncertain(out, rec, e)
        return PublishResult("UNKNOWN", rec, str(e))
    except PublishRejectedError as e:
        store.update(job.job_key, "FAILED", failure_phase="PUBLISH_REJECTED", last_error=str(e),
                     detail=f"media_publish rejected: {e.reason}")
        ctx.log("publish", "PUBLISH", "REJECTED", e, classification=e.reason, **diag)
        raise
    except BaseException as e:  # anything unexpected, incl. Ctrl-C mid-request: never assume failure
        rec = store.update(job.job_key, "UNKNOWN",
                           last_error=f"interrupted/unexpected during publish: {type(e).__name__}: {e}",
                           detail="media_publish outcome uncertain: unexpected exception")
        ctx.log("publish", "PUBLISH", "UNCERTAIN", e if isinstance(e, MetaApiError) else None,
                classification=f"unexpected {type(e).__name__}", **diag)
        raise
    rec = store.update(job.job_key, "PUBLISHED", media_id=media_id, last_error=None)
    ctx.log("publish", "PUBLISH", "OK", media_id=media_id, **diag)
    out(f"Published: IG media ID {media_id}")

    media = None
    try:
        media = client.get_media(media_id)
        if media.get("permalink"):
            rec = store.update(job.job_key, "PUBLISHED", permalink=media["permalink"])
            out(f"Permalink: {media['permalink']}")
    except MetaApiError as e:
        out(f"(Could not fetch permalink: {e.meta_message})")
    check = mode_check(job, media)
    rec = store.update(job.job_key, "PUBLISHED", detail=check[0])
    for line in check[1:]:
        out(line)
    out("Saved success to local state. This video will not be reposted on rerun.")
    return PublishResult("PUBLISHED", rec)


def mode_check(job: VideoJob, media: Optional[dict]) -> List[str]:
    """[event detail, *output lines]. Requested mode vs what Meta's API can confirm. A media ID from
    media_publish proves only that SOMETHING was published, never that it is a Trial Reel: no documented
    IG Media field reports trial status. is_shared_to_feed=true is what normal Reels show, so for a TRIAL
    request it is evidence the trial was NOT applied."""
    shared = (media or {}).get("is_shared_to_feed")
    product = (media or {}).get("media_product_type")
    seen = f"media_product_type={product or '?'}, is_shared_to_feed={'?' if shared is None else str(shared).lower()}"
    requested = mode_label(job.publish_mode, job.graduation_strategy)
    if job.publish_mode != TRIAL:
        return [f"mode check: requested {requested}; Meta reports {seen}",
                f"Requested mode:  {requested}", f"Meta reports:    {seen}"]
    if shared is True:
        verdict = "NOT A TRIAL (Meta reports is_shared_to_feed=true, as for a normal Reel)"
        extra = ["WARNING: this looks like a NORMAL Reel, not a Trial Reel. Check your profile grid and the Trial "
                 "Reels section in the Instagram app now."]
    else:
        verdict = "NOT VERIFIED (the API has no field that reports trial status)"
        extra = ["Check the Instagram app: it must appear under Trial Reels and NOT on your profile grid. "
                 + ("is_shared_to_feed=false is consistent with a trial but is not proof." if shared is False
                    else "Meta did not return is_shared_to_feed.")]
    return [f"mode check: requested {requested} via {job.upload_method}; confirmed mode {verdict}; Meta reports {seen}",
            f"Requested mode:  {requested} via {job.upload_method}",
            f"Confirmed mode:  {verdict}", f"Meta reports:    {seen}", *extra]


def _report_uncertain(out: Out, rec: JobRecord, e: AmbiguousPublishError) -> None:
    out(f"UNCERTAIN: {e}")
    out(f"Job #{rec.id} is now UNKNOWN (container {rec.container_id}). It will NOT be retried, and all "
        "publishing to this account is paused until it is resolved.")
    out("Check Instagram, then run:  python publish.py --account <profile> --reconcile")


def _int(v) -> Optional[int]:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ reconciliation
CONFIRMED_PUBLISHED = "CONFIRMED_PUBLISHED"
CONFIRMED_NOT_PUBLISHED = "CONFIRMED_NOT_PUBLISHED"
UNRESOLVED = "UNRESOLVED"
CONTAINER_TTL = timedelta(hours=24)  # documented: unpublished containers expire after 24 hours

# Meta's documented container status_code semantics (Content Publishing guide).
STATUS_MEANING = {
    "PUBLISHED": (CONFIRMED_PUBLISHED, "Meta: 'The container's media object has been published.'"),
    "EXPIRED": (CONFIRMED_NOT_PUBLISHED, "Meta: 'The container was not published within 24 hours and "
                                         "has expired.'"),
    "ERROR": (CONFIRMED_NOT_PUBLISHED, "Meta: 'The container failed to complete the publishing process.'"),
    "FINISHED": (UNRESOLVED, "Meta: 'ready to be published'. FINISHED proves neither publication nor "
                             "failure of the earlier media_publish request."),
    "IN_PROGRESS": (UNRESOLVED, "Meta: 'still in the publishing process'."),
}


@dataclass
class Evidence:
    job: JobRecord
    verdict: str
    reasons: List[str]
    container_status: Optional[str] = None
    container_status_text: Optional[str] = None
    candidate_media: List[dict] = None  # type: ignore[assignment]
    expires_around: Optional[datetime] = None


def unresolved_jobs(store: StateStore, account_id: Optional[str]) -> List[JobRecord]:
    """Uncertain by publish HISTORY, not just label (FAILED after a sent media_publish counts)."""
    return [r for r in store.uncertain_jobs() if account_id is None or r.account_id == account_id]


def _parse_ts(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        v = v.replace("Z", "+00:00")
        if len(v) >= 5 and (v[-5] in "+-") and v[-3] != ":":  # Meta style +0000
            v = v[:-2] + ":" + v[-2:]
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def gather_evidence(client: MetaClient, store: StateStore, rec: JobRecord) -> Evidence:
    """Read-only. Every conclusion carries the evidence it is based on."""
    reasons: List[str] = []
    verdict = UNRESOLVED
    status = status_text = None
    if rec.container_id:
        try:
            st = client.get_container_status(rec.container_id)
            status, status_text = st.status_code, st.status
            verdict, meaning = STATUS_MEANING.get(status or "", (UNRESOLVED, f"undocumented status {status!r}"))
            reasons.append(f"container {rec.container_id} status_code={status} ({status_text or 'no text'}). "
                           f"{meaning}")
        except MetaApiError as e:
            reasons.append(f"could not read container {rec.container_id}: {e.meta_message} "
                           f"(code={e.code}, subcode={e.subcode}). Outcome stays unresolved.")
    else:
        reasons.append("no container id recorded; outcome cannot be checked with Meta.")

    created = next((_parse_ts(ev["at"]) for ev in store.events(rec.job_key) if ev["state"] == "CONTAINER_CREATED"),
                   None)
    expires = created + CONTAINER_TTL if created else None

    calls = [c for c in store.api_calls(rec.job_key) if c["step"] in ("publish", "retry_publish")]
    before = calls[-1] if calls else None
    try:
        limit = client.get_publishing_limit()
        usage = limit.get("quota_usage")
        if before and before.get("quota_usage_before") is not None:
            reasons.append(f"supporting only: general API quota_usage was {before['quota_usage_before']} before "
                           f"the attempt and is {usage} now (a rolling 24h window; Trial Reels may not count).")
        else:
            reasons.append(f"supporting only: general API quota_usage now {usage} (no pre-attempt snapshot recorded).")
    except MetaApiError as e:
        reasons.append(f"could not read publishing quota: {e.meta_message}")

    candidates: List[dict] = []
    started = _parse_ts(rec.publish_started_at)
    try:
        media = client.recent_media(25)
        if started:
            window = started - timedelta(minutes=2)
            candidates = [m for m in media if (_parse_ts(m.get("timestamp")) or window) >= window
                          and _parse_ts(m.get("timestamp")) is not None]
        reasons.append(f"supporting only: {len(candidates)} item(s) in the latest 25 media were posted at or after "
                       "the attempt. Trial Reels are not guaranteed to appear in this listing, so absence "
                       "proves nothing.")
    except MetaApiError as e:
        reasons.append(f"could not list recent media: {e.meta_message}")
    return Evidence(rec, verdict, reasons, status, status_text, candidates, expires)


def _select(jobs: List[JobRecord], job_id: Optional[int]) -> JobRecord:
    if job_id is not None:
        match = [j for j in jobs if j.id == job_id]
        if not match:
            raise StateError(f"Job #{job_id} is not an unresolved job for this account.")
        return match[0]
    if len(jobs) != 1:
        raise StateError(f"Expected exactly one unresolved job, found {len(jobs)}. Use --job <ID>.")
    return jobs[0]


def reconcile(client: MetaClient, store: StateStore, out: Out) -> List[Evidence]:
    """Read-only: report Meta's evidence for each unresolved job and the safe next step."""
    jobs = unresolved_jobs(store, client.config.ig_user_id)
    for r in store.unknown_account_jobs():
        if is_blocking(r):
            out(f"Note: job #{r.id} ({Path(r.source_path).name}, {r.state}) has no valid account ID "
                f"({r.account_id!r}). It is kept, and it blocks that video on every account. This tool won't "
                "guess its owner; see README 'Records with an unknown account'.")
    if not jobs:
        out(f"No unresolved publication attempts for IG ID {client.config.ig_user_id}. Nothing to reconcile.")
        return []
    results = []
    for rec in jobs:
        ev = gather_evidence(client, store, rec)
        results.append(ev)
        label = rec.state + (" (recorded FAILED, but media_publish was sent)" if rec.state == "FAILED" else "")
        out(f"Job #{rec.id}  {rec.relative_path}  mode={rec.publish_mode}  state={label}")
        out(f"  account {rec.account_id} @{rec.account_username or '?'}, container {rec.container_id}, "
            f"publish attempted {rec.publish_started_at} UTC, attempts={rec.publish_attempts}")
        if rec.last_error:
            out(f"  last error: {rec.last_error.splitlines()[0][:300]}")
        out("  Evidence:")
        for line in ev.reasons:
            out(f"    - {line}")
        for m in ev.candidate_media or []:
            out(f"      candidate: {m.get('timestamp')}  {m.get('id')}  {m.get('media_product_type')}  "
                f"{m.get('permalink')}")
        out(f"  Verdict: {ev.verdict}")
        if ev.verdict == CONFIRMED_PUBLISHED:
            out("  Proposed: mark PUBLISHED. Meta does not return the media id from the container, so none "
                "will be invented.")
            out(f"    python publish.py --account <profile> --mark-published --job {rec.id}"
                "            # media id left empty")
            out(f"    python publish.py --account <profile> --mark-published <MEDIA_ID> --job {rec.id}"
                "  # if you identified it")
        elif ev.verdict == CONFIRMED_NOT_PUBLISHED:
            out("  Proposed: mark not published (history kept):")
            out(f"    python publish.py --account <profile> --mark-not-published --job {rec.id}")
        else:
            out("  Stays UNKNOWN. This account remains paused.")
            if ev.container_status == "FINISHED" and ev.expires_around:
                out(f"  The container should expire around {ev.expires_around.isoformat(timespec='minutes')} UTC "
                    "if it was never published; reconcile again after that for a definitive answer.")
            out("  If you find the Reel in the Instagram app, record it with --mark-published <MEDIA_ID> "
                f"--job {rec.id}.")
    return results


def mark_published(client: MetaClient, store: StateStore, media_id: Optional[str], out: Out,
                   job_id: Optional[int] = None) -> JobRecord:
    rec = _select(unresolved_jobs(store, client.config.ig_user_id), job_id)
    ev = gather_evidence(client, store, rec)
    evidence = "; ".join(ev.reasons[:1])
    permalink = None
    if media_id:
        media = client.get_media(media_id)  # must exist and be readable with this account's token
        ts = _parse_ts(media.get("timestamp"))
        started = _parse_ts(rec.publish_started_at)
        if ts and started and ts < started - timedelta(minutes=2):
            raise StateError(f"Media {media_id} was posted at {media.get('timestamp')}, before this publish "
                             f"attempt ({rec.publish_started_at}); it cannot be this job's Reel.")
        permalink = media.get("permalink")
        evidence = f"user supplied media {media_id} (exists, timestamp {media.get('timestamp')}); {evidence}"
    elif ev.verdict != CONFIRMED_PUBLISHED:
        raise StateError(f"Meta does not confirm publication (container status {ev.container_status}). "
                         "Supply the media id you found (--mark-published <MEDIA_ID>) or keep it UNKNOWN.")
    else:
        evidence = f"container status PUBLISHED; media id not provided by Meta, left empty; {evidence}"
    rec = store.update(rec.job_key, "PUBLISHED", expect_state=rec.state, media_id=media_id or None,
                       permalink=permalink, detail=f"reconciled as PUBLISHED. Evidence: {evidence}")
    out(f"Marked job #{rec.id} as published: media {media_id or '(id not provided by Meta)'} {permalink or ''}")
    return rec


def mark_not_published(client: MetaClient, store: StateStore, out: Out, job_id: Optional[int] = None) -> JobRecord:
    rec = _select(unresolved_jobs(store, client.config.ig_user_id), job_id)
    ev = gather_evidence(client, store, rec)
    if ev.verdict != CONFIRMED_NOT_PUBLISHED:
        hint = ""
        if ev.container_status == "FINISHED" and ev.expires_around:
            hint = f" Wait until after ~{ev.expires_around.isoformat(timespec='minutes')} UTC, when an unpublished container expires."
        raise StateError(f"Refusing: Meta does not prove job #{rec.id} was unpublished (container status "
                         f"{ev.container_status}). It stays UNKNOWN.{hint}")
    rec = store.update(rec.job_key, "FAILED", expect_state=rec.state, failure_phase="RECONCILED_NOT_PUBLISHED",
                       last_error=f"reconciled: not published (container {ev.container_status})",
                       detail=f"reconciled as not published. Evidence: {ev.reasons[0]}")
    out(f"Marked job #{rec.id} as not published (container {ev.container_status}). History kept; the account "
        "is no longer paused by this job.")
    return rec
