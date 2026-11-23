"""Daily batch: post every LipSync video uploaded on one day as a Trial Reel.

LipSync uploads each take to R2 at ``lipsync/<YYYY-MM-DD>/<uuid>.mp4``, dated by the America/Denver calendar.
Each morning the batch posts the previous day's folder, oldest upload first, via ``video_url`` (the only
method that gives a Trial Reel; see models.RESUMABLE_TRIAL_ALLOWED).

Three actions, all on the same code path, so testing one video at a time tests what 8:00 will run:

  --batch DAY --dry-run   list the day, check every hosted video like Meta will, show what would be posted.
                          Read-only with Meta; nothing is created or published.
  --batch DAY --next      post ONLY the next unposted video, after you type the account name.
  --batch DAY --all       post every unposted video of the day, in order (asks once; --unattended skips that).

Identity is the hosted bytes (sha256), as in --test-mode, because there is no local original. Every normal
guard still applies, inside publish_job: the account pause on any unresolved job, refusal of a video already
posted to this account (same bytes, any day, any method), and one media_publish per container.
The batch stops at the first video that does not publish cleanly.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import urlparse

from .config import Config
from .errors import PublisherError, redact
from .models import TRIAL, URL, VideoJob
from .publisher import (account_block_message, check_auth, duplicate_message, human_size, mode_label,
                        publish_job, remaining_quota)
from .state import StateStore, is_uncertain, process_lock

DRY_RUN, NEXT, ALL = "dry-run", "next", "all"
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class DayVideo:
    key: str
    size: int
    uploaded: str
    url: str

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]


# ------------------------------------------------------------------ which day, which videos
def resolve_day(arg: str, tz_name: str, now: Optional[datetime] = None) -> date:
    """'yesterday', 'today' or YYYY-MM-DD, by the configured time zone's calendar."""
    from zoneinfo import ZoneInfo

    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz_name))
    arg = (arg or "").strip().lower()
    if arg == "yesterday":
        return now.date() - timedelta(days=1)
    if arg == "today":
        return now.date()
    if _DAY.fullmatch(arg):
        try:
            return date.fromisoformat(arg)
        except ValueError:
            pass
    raise PublisherError(f"--batch takes 'yesterday', 'today' or a date like 2026-11-12 (got {arg!r}).")


def list_day(config: Config, day: date, session=None) -> List[DayVideo]:
    """Asks the LipSync Worker for lipsync/<day>/*.mp4, oldest upload first."""
    if not config.lipsync_worker_url or not config.lipsync_list_token:
        raise PublisherError(f"--batch needs LIPSYNC_WORKER_URL and LIPSYNC_LIST_TOKEN in "
                             f"{config.credentials_file.name if config.credentials_file else 'the profile file'}.")
    base = config.lipsync_worker_url.rstrip("/")
    if urlparse(base).scheme != "https":
        raise PublisherError("LIPSYNC_WORKER_URL must start with https://")
    if session is None:
        import requests
        session = requests.Session()
    try:
        resp = session.get(f"{base}/list", params={"date": day.isoformat()},
                           headers={"Authorization": f"Bearer {config.lipsync_list_token}"},
                           timeout=config.http_timeout_s)
    except Exception as e:
        raise PublisherError(f"Could not reach the LipSync Worker: {redact(e, [config.lipsync_list_token])}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200:
        raise PublisherError(f"The LipSync Worker refused the listing (HTTP {resp.status_code}): "
                             f"{redact(body.get('error', ''), [config.lipsync_list_token])}")
    if body.get("date") != day.isoformat() or not isinstance(body.get("videos"), list):
        raise PublisherError("The LipSync Worker sent a listing for the wrong day or in an unknown format.")
    videos = []
    prefix = f"lipsync/{day.isoformat()}/"
    for v in body["videos"]:
        key, link = str(v.get("key", "")), str(v.get("publicURL", ""))
        if not key.startswith(prefix) or not key.endswith(".mp4") or not link.endswith("/" + key):
            raise PublisherError(f"The LipSync Worker listed an unexpected object {key!r}; nothing was posted.")
        videos.append(DayVideo(key=key, size=int(v.get("size") or 0), uploaded=str(v.get("uploaded", "")),
                               url=link))
    return sorted(videos, key=lambda v: (v.uploaded, v.key))


# ------------------------------------------------------------------ per-video status
@dataclass
class Item:
    video: DayVideo
    status: str                       # READY | POSTED | DUPLICATE | UNRESOLVED | INVALID
    detail: str = ""
    job: Optional[VideoJob] = None


def _remote_path(url: str) -> Path:
    u = urlparse(url)
    return Path("/remote") / u.netloc / u.path.lstrip("/")


def make_job(config: Config, day: date, hv) -> VideoJob:
    return VideoJob(path=_remote_path(hv.url), account_id=config.ig_user_id, caption=config.default_caption,
                    graduation_strategy=config.graduation_strategy, sha256=hv.sha256,
                    size_bytes=hv.content_length, publish_mode=TRIAL, upload_method=URL, video_url=hv.url,
                    hosted_sha256=hv.sha256, hosted_size_bytes=hv.content_length,
                    identity_evidence=f"daily batch {day.isoformat()}: the hosted video is the original "
                                      f"(sha256 {hv.sha256})")


def assess(config: Config, store: StateStore, day: date, video: DayVideo, inspector) -> Item:
    """Where this video stands. Downloads it (like Meta will) unless it is already known to be posted."""
    path = str(_remote_path(video.url))
    mine = [r for r in store.all() if r.account_id == config.ig_user_id and r.source_path == path]
    done = next((r for r in mine if r.state == "PUBLISHED"), None)
    if done:
        return Item(video, "POSTED", f"job #{done.id}, media {done.media_id or '?'} {done.permalink or ''}".strip())
    stuck = next((r for r in mine if is_uncertain(r)), None)
    if stuck:
        return Item(video, "UNRESOLVED", f"job #{stuck.id} is {stuck.state}; run --reconcile")
    try:
        hv = inspector(video.url, tmp_root=config.tmp_dir, source_path=None, ffmpeg=None)
    except PublisherError as e:
        return Item(video, "INVALID", str(e))
    job = make_job(config, day, hv)
    dup = next((r for r in store.find_blocking(job.account_id, job.sha256) if r.job_key != job.job_key), None)
    if dup is not None:
        return Item(video, "DUPLICATE", duplicate_message(dup).splitlines()[0], job)
    return Item(video, "READY", f"{human_size(hv.content_length)}, sha256 {hv.sha256[:12]}…", job)


# ------------------------------------------------------------------ the command
def cmd_batch(config: Config, out, client_factory, input_fn, inspector, *, action: str, day_arg: str,
              unattended: bool = False, lister: Callable = list_day, now: Optional[datetime] = None) -> int:
    if action not in (DRY_RUN, NEXT, ALL):
        raise ValueError(action)
    if unattended and action != ALL:
        raise PublisherError("--unattended is only used with --all.")
    config.require_credentials()
    if action != DRY_RUN and not unattended and not sys.stdin.isatty():
        raise PublisherError("--next / --all ask you to type the account name, so they need a terminal. "
                             "Scheduled runs use --all --unattended. Nothing was posted.")
    day = resolve_day(day_arg, config.timezone, now)

    with process_lock(config.lock_path):
        store = StateStore(config.state_path)
        title = {DRY_RUN: "DRY RUN (nothing will be created or published)",
                 NEXT: "NEXT VIDEO ONLY", ALL: "ALL VIDEOS" + (" (unattended)" if unattended else "")}[action]
        out(f"Daily batch for {day.isoformat()} ({config.timezone}): {title}")

        # Local state first: a paused account stops everything before any network call.
        blockers = store.account_blockers(config.ig_user_id)
        if blockers and action != DRY_RUN:
            raise PublisherError(account_block_message(blockers, config.ig_user_id))

        videos = lister(config, day)
        client = client_factory(config)
        info = check_auth(client, out)  # read-only; enforces the configured IG account
        username = info["account"].get("username") or ""
        left = remaining_quota(info["limit"])

        items = [assess(config, store, day, v, inspector) for v in videos]
        ready = [i for i in items if i.status == "READY"]
        out("")
        out(f"Account:  @{username} (IG ID {info['account'].get('id')})  [verified with Meta]")
        out(f"Caption:  {config.default_caption!r}" if config.default_caption else "Caption:  (none)")
        out(f"Mode:     {mode_label(TRIAL, config.graduation_strategy)} via video_url")
        out(f"Videos:   {len(items)} in lipsync/{day.isoformat()}/; {len(ready)} ready to post")
        for n, it in enumerate(items, 1):
            out(f"  {n}. {it.status:<10} {it.video.name}  uploaded {it.video.uploaded}  {it.detail}")
        if blockers:
            out("PAUSED:   " + account_block_message(blockers, config.ig_user_id))
        if left is not None:
            out(f"Quota:    {left} posts left in Meta's 24-hour window")

        if action == DRY_RUN:
            if blockers:
                out("NOT READY: the account is paused, so a real run would post nothing.")
                return 2
            out("A real --all run would post the READY videos above, in that order, each once." if ready
                else "Nothing to post.")
            return 0
        if not ready:
            out("Nothing to post.")
            return 0
        todo = ready[:1] if action == NEXT else ready
        if left is not None and left < len(todo):
            if left <= 0:
                raise PublisherError("Meta reports no remaining publishing quota. Nothing was posted.")
            out(f"Only {left} post(s) left in the quota; posting the first {left} now.")
            todo = todo[:left]

        if not unattended:
            what = f"{len(todo)} Trial Reel{'s' if len(todo) != 1 else ''}"
            answer = input_fn(f"Type the account name '{username}' to post {what} to @{username}: ")
            if not username or answer.strip().lstrip("@") != username:
                out("Cancelled. Nothing was posted.")
                return 1

        posted = 0
        for n, it in enumerate(todo, 1):
            out("")
            out(f"[{n}/{len(todo)}] {it.video.name}")
            try:
                result = publish_job(it.job, client=client, store=store, out=out,
                                     relative_path=it.video.key, remuxer=None)
            except PublisherError as e:
                out(f"FAILED: {e}")
                out(f"Stopped after {posted} of {len(todo)}. The rest were not attempted.")
                return 3
            if result.outcome not in ("PUBLISHED", "ALREADY_PUBLISHED"):
                out(f"Stopped after {posted} of {len(todo)}: {it.video.name} ended {result.outcome}. "
                    "The rest were not attempted.")
                return 3
            posted += result.outcome == "PUBLISHED"
        remaining = len(ready) - len(todo)
        out("")
        out(f"Done: {posted} posted." + (f" {remaining} still waiting; run --next again." if remaining else "")
            + " Check the Instagram app: each must appear under Trial Reels, not on your grid.")
        return 0
