"""One user-approved re-send of media_publish for an EXISTING container (``--retry-publish``).

Scope is deliberately tiny: only the (job, container, profile, account) combinations listed in
APPROVED_RETRIES can ever be retried, once each. Nothing here creates a container, uploads bytes,
remuxes, or reads the video file. Any outcome other than a media id returned by Meta leaves the job
UNKNOWN, so the account stays paused.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from .errors import AmbiguousPublishError, MetaApiError, PublisherError, PublishRejectedError
from .meta_client import MetaClient
from .publisher import CONTAINER_TTL, AccountMismatchError, _int, _parse_ts, verify_account
from .state import JobRecord, StateStore

log = logging.getLogger(__name__)
Out = Callable[[str], None]


@dataclass(frozen=True)
class ApprovedRetry:
    job_id: int
    container_id: str
    profile: str
    ig_user_id: str


# Approved by the user on 2026-11-10 after the 2207085 investigation. Add nothing here without a new,
# explicit approval for that specific job and container.
APPROVED_RETRIES = {
    3: ApprovedRetry(job_id=3, container_id="18657792691044336", profile="personal",
                     ig_user_id="17840000000000002"),
}
# The original media_publish counts as attempt 1; exactly one manual retry is allowed on top of it.
MAX_ATTEMPTS_BEFORE_RETRY = 1
CONFIRM_WORD = "RETRY"


class RetryRefused(PublisherError):
    """Raised before media_publish is sent; nothing was posted."""


def _refuse(msg: str) -> RetryRefused:
    return RetryRefused(f"{msg} Nothing was sent to Meta's publish endpoint.")


def _check_local(store: StateStore, client: MetaClient, job_id: Optional[int]) -> Tuple[ApprovedRetry, JobRecord]:
    if job_id is None:
        raise _refuse("--retry-publish needs --job <ID>.")
    approved = APPROVED_RETRIES.get(job_id)
    if approved is None:
        raise _refuse(f"Job #{job_id} is not approved for a manual retry (approved: "
                      f"{', '.join('#%d' % j for j in sorted(APPROVED_RETRIES)) or 'none'}).")
    cfg = client.config
    if cfg.profile != approved.profile:
        raise _refuse(f"Job #{job_id} may only be retried with --account {approved.profile} "
                      f"(got {cfg.account_label}).")
    if str(cfg.ig_user_id) != approved.ig_user_id:
        raise _refuse(f"Profile '{cfg.profile}' is configured for IG ID {cfg.ig_user_id}, but job #{job_id} "
                      f"belongs to IG ID {approved.ig_user_id}.")
    rec = store.get_by_id(job_id)
    if rec is None:
        raise _refuse(f"Job #{job_id} does not exist in the local database.")
    if rec.state == "PUBLISHED":
        raise _refuse(f"Job #{job_id} is already PUBLISHED (media {rec.media_id or 'id not provided'}).")
    if rec.state != "UNKNOWN":
        raise _refuse(f"Job #{job_id} is {rec.state}, not UNKNOWN; only an unresolved job can be retried.")
    if rec.container_id != approved.container_id:
        raise _refuse(f"Job #{job_id} is bound to container {rec.container_id}, not the approved "
                      f"container {approved.container_id}.")
    if str(rec.account_id) != approved.ig_user_id:
        raise _refuse(f"Job #{job_id} is recorded for IG ID {rec.account_id}, not {approved.ig_user_id}.")
    if (rec.publish_attempts or 0) > MAX_ATTEMPTS_BEFORE_RETRY:
        raise _refuse(f"Job #{job_id} has already been retried (publish_attempts={rec.publish_attempts}). "
                      "Only one manual retry is allowed; resolve it with --reconcile.")
    others = [b for b in store.account_blockers(rec.account_id) if b.job_key != rec.job_key]
    if others:
        raise _refuse("Another job on this account is also unresolved: "
                      + ", ".join(f"#{b.id} ({b.state})" for b in others) + ".")
    dup = [b for b in store.find_blocking(rec.account_id, rec.content_hash) if b.job_key != rec.job_key]
    if dup:
        raise _refuse(f"This video is already (or may be) live on this account via job #{dup[0].id}.")
    return approved, rec


def _read_status(client: MetaClient, container_id: str) -> str:
    try:
        st = client.get_container_status(container_id)
    except MetaApiError as e:
        raise _refuse(f"Could not read container {container_id} status ({e.meta_message}; code={e.code}, "
                      f"subcode={e.subcode}). Not retrying without a fresh FINISHED reading.")
    status = st.status_code
    if status == "FINISHED":
        return status
    advice = {
        "PUBLISHED": "Meta says it is already published. Record it with --mark-published --job N instead.",
        "EXPIRED": "It expired unpublished. Record it with --mark-not-published --job N.",
        "ERROR": "Meta says it failed. Record it with --mark-not-published --job N.",
    }.get(status or "", "Only FINISHED can be retried.")
    raise _refuse(f"Container {container_id} status is {status!r} ({st.status or 'no text'}). {advice}")


def retry_publish(client: MetaClient, store: StateStore, out: Out, *, job_id: Optional[int],
                  input_fn=input, interactive: bool = True) -> int:
    """Returns 0 when Meta returned a media id, 3 when the job stays UNKNOWN. Raises RetryRefused
    (exit 2) for every refusal before media_publish is sent."""
    approved, rec = _check_local(store, client, job_id)
    if not interactive:
        raise _refuse("--retry-publish needs an interactive terminal for its confirmation.")

    acct = verify_account(client, approved.ig_user_id)  # read-only: who does this token belong to?
    if str(acct.get("id")) != approved.ig_user_id:       # verify_account already enforces this
        raise AccountMismatchError("Authenticated account does not match the approved account.")
    status = _read_status(client, approved.container_id)

    created = next((_parse_ts(ev["at"]) for ev in store.events(rec.job_key) if ev["state"] == "CONTAINER_CREATED"),
                   None)
    original_error = (rec.last_error or "(none recorded)").splitlines()[0][:300]
    out("")
    out("=" * 72)
    out("  MANUAL RETRY of media_publish on an EXISTING container")
    out(f"  Account:          @{acct.get('username', '?')} (IG ID {acct.get('id')})  [verified with Meta]")
    out(f"  Credentials:      {client.config.account_label}")
    out(f"  Job:              #{rec.id}  {rec.relative_path}  mode={rec.publish_mode}")
    out(f"  Container:        {approved.container_id}")
    out(f"  Original attempt: {rec.publish_started_at} UTC")
    out(f"  Original error:   {original_error}")
    out(f"  Current status:   {status} (read from Meta just now)")
    if created:
        out(f"  Expires around:   {(created + CONTAINER_TTL).isoformat(timespec='minutes')} UTC if never published")
    out("  No new container is created and no video is uploaded. This sends media_publish ONCE.")
    out("  If it succeeds, this Reel goes live as the container was created (Trial Reel, MANUAL).")
    out("  Any failure or unclear reply keeps the job UNKNOWN and the account paused.")
    out("  Before continuing, check @%s in the Instagram app for a Reel from the original attempt."
        % acct.get("username", "?"))
    out("=" * 72)
    phrase = f"{CONFIRM_WORD} {rec.id}"
    answer = input_fn(f"Type '{phrase}' to re-send media_publish for container {approved.container_id}: ")
    if answer.strip() != phrase:
        out("Cancelled. Nothing was sent; job stays UNKNOWN.")
        return 1

    # Re-read immediately before sending: the prompt may have been open for a while.
    status = _read_status(client, approved.container_id)
    quota_usage = quota_total = None
    try:
        limit = client.get_publishing_limit()
        quota_usage = limit.get("quota_usage")
        quota_total = (limit.get("config") or {}).get("quota_total")
    except MetaApiError as e:
        log.warning("could not read publishing quota before retry: %s", e)

    started = store.begin_retry_publish(rec.job_key, approved.container_id, MAX_ATTEMPTS_BEFORE_RETRY)
    attempted_at = store.events(rec.job_key)[-1]["at"]
    diag = dict(container_status_before=status, quota_usage_before=_int(quota_usage),
                quota_total_before=_int(quota_total), attempted_at=attempted_at)

    def log_call(outcome, err=None, **kw):
        store.log_api_call(job_key=rec.job_key, account_id=rec.account_id, content_hash=rec.content_hash,
                           step="retry_publish", phase="PUBLISH", outcome=outcome, err=err,
                           container_id=approved.container_id, **diag, **kw)

    def stay_unknown(err_text: str, detail: str) -> JobRecord:
        return store.update(rec.job_key, "UNKNOWN", expect_state="PUBLISHING",
                            last_error=f"manual retry at {attempted_at} UTC: {err_text}\n"
                                       f"original attempt {rec.publish_started_at} UTC: {original_error}",
                            detail=detail)

    out(f"Sending media_publish for container {approved.container_id} (attempt {started.publish_attempts})…")
    try:
        media_id = client.publish_container(approved.container_id)
    except (AmbiguousPublishError, PublishRejectedError) as e:
        # Even a documented rejection of the RETRY says nothing about whether the ORIGINAL attempt went
        # live, so the job stays UNKNOWN either way.
        kind = "UNCERTAIN" if isinstance(e, AmbiguousPublishError) else "REJECTED"
        stay_unknown(str(e), f"manual retry {kind.lower()}: {e.reason}; original attempt still unresolved")
        log_call(kind, getattr(e, "meta", None) or e, classification=e.reason)
        out(f"Retry did not confirm publication: {e}")
        out(f"Job #{rec.id} stays UNKNOWN and @{acct.get('username', '?')} stays paused. No further retry is "
            "allowed. Run --reconcile; after the container expires, --mark-not-published becomes possible.")
        return 3
    except BaseException as e:  # incl. Ctrl-C mid-request: never assume failure
        stay_unknown(f"interrupted/unexpected: {type(e).__name__}: {e}",
                     "manual retry outcome uncertain: unexpected exception")
        log_call("UNCERTAIN", e if isinstance(e, MetaApiError) else None,
                 classification=f"unexpected {type(e).__name__}")
        raise

    done = store.update(rec.job_key, "PUBLISHED", expect_state="PUBLISHING", media_id=media_id, last_error=None,
                        detail=f"manual retry succeeded: Meta returned media id {media_id}")
    log_call("OK", media_id=media_id)
    out(f"Published: IG media ID {media_id}")
    try:
        media = client.get_media(media_id)
        if media.get("permalink"):
            done = store.update(rec.job_key, "PUBLISHED", permalink=media["permalink"])
            out(f"Permalink: {media['permalink']}")
    except MetaApiError as e:
        out(f"(Could not fetch permalink: {e.meta_message})")
    out(f"Job #{done.id} is PUBLISHED; @{acct.get('username', '?')} is no longer paused by it.")
    out("Open the Reel in the Instagram app and confirm it is labelled as a trial.")
    return 0
