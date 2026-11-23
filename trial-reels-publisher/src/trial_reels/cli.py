"""Command-line entry point (called from publish.py)."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import List, Optional

from .config import PROFILES, Config, load_config
from .discovery import SingleFileSource
from .errors import PublisherError, redact
from .meta_client import MetaClient
from .remux import Remuxer
from .hosted import HostedVideo, inspect_hosted_video
from . import models
from .models import PUBLIC, RESUMABLE, TRIAL, UPLOAD_METHODS, URL
from .publisher import (build_job, check_auth, describe_request, describe_video, discover_and_validate,
                        account_block_message, check_account_not_blocked, describe_url_request, human_size,
                        duplicate_message, find_duplicate, mark_not_published, mark_published,
                        publish_job, reconcile, remaining_quota, TRIAL_NEEDS_URL)
from .batch import ALL, DRY_RUN, NEXT, cmd_batch
from .diagnostic import cmd_diagnostic_public
from .override import cmd_override_publish_url
from .retry import retry_publish
from .state import TEST_MODE_PREFIX, StateStore, is_blocking, is_uncertain, process_lock


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(config: Config) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(config.log_dir / "publisher.log", maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, config.log_level, logging.INFO))
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # urllib3 debug would log URLs/headers


def make_out():
    log = logging.getLogger("trial_reels.out")

    def out(msg: str) -> None:
        msg = redact(msg)
        print(msg, flush=True)
        log.info(msg)
    return out


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="publish.py",
                                description="Publish the single .mp4/.mov in ~/Documents/Trial-Reels as an "
                                            "Instagram Trial Reel.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="validate locally; never creates or publishes media")
    g.add_argument("--check-auth", action="store_true", help="read-only token/account/quota check")
    g.add_argument("--status", action="store_true", help="show local publish history (no network)")
    g.add_argument("--reconcile", action="store_true", help="read-only check of an UNKNOWN publish")
    g.add_argument("--mark-published", metavar="MEDIA_ID", nargs="?", const="",
                   help="resolve an unresolved job as published (container must be PUBLISHED, or give the "
                        "media id you found)")
    g.add_argument("--mark-not-published", action="store_true",
                   help="resolve an unresolved job as not published (only when Meta proves it: EXPIRED/ERROR)")
    g.add_argument("--retry-publish", action="store_true",
                   help="re-send media_publish ONCE for an approved UNKNOWN job's existing container "
                        "(needs --account and --job; asks for a separate confirmation; never uploads)")
    p.add_argument("--job", type=int, metavar="ID", help="job number to reconcile when there are several")
    p.add_argument("--account", choices=sorted(PROFILES),
                   help="credentials profile: 'test' reads .env.test, 'personal' reads .env.personal "
                        "(omit to use .env as before)")
    p.add_argument("--public", action="store_true",
                   help="publish a REGULAR public Reel (no trial_params) instead of a Trial Reel; "
                        "use alone or with --dry-run; always asks for an extra confirmation")
    p.add_argument("--upload-method", choices=UPLOAD_METHODS, default=RESUMABLE,
                   help="resumable (default): upload the local file to Meta. url: give Meta --video-url, a "
                        "publicly hosted MP4 copy of the local video, and let Meta fetch it (Trial Reels only; "
                        "needs --account; use alone or with --dry-run)")
    p.add_argument("--video-url", metavar="URL",
                   help="with --upload-method url: public https URL of an MP4 stream copy of the video in "
                        "the Trial-Reels folder")
    p.add_argument("--override-unknown-job", type=int, metavar="ID",
                   help="ONE-TIME: allow a single new URL Trial Reel attempt while approved job ID stays UNKNOWN "
                        "(needs --account personal --upload-method url --video-url; asks for confirmations)")
    p.add_argument("--diagnostic-public-override", action="store_true",
                   help="ONE-TIME: one normal PUBLIC Reel of the approved video on the personal account while "
                        "approved UNKNOWN trial jobs stay UNKNOWN (needs --account personal --public)")
    p.add_argument("--diagnostic-reattempt-override", action="store_true",
                   help="ONE-TIME: re-attempt UNKNOWN job #5's video as one normal PUBLIC Reel on the personal "
                        "account while jobs #3/#4/#5 stay UNKNOWN (needs --account personal --public)")
    p.add_argument("--diagnostic-trial-reattempt-override", action="store_true",
                   help="ONE-TIME: IMG_2248 as one TRIAL Reel (MANUAL) on the personal account despite jobs "
                        "#5/#7 of the same video, while #3/#4/#5 stay UNKNOWN (needs --account personal)")
    p.add_argument("--test-mode", action="store_true",
                   help="MANUAL TESTING ONLY: one new attempt that ignores earlier job locks and duplicate checks "
                        "(history is kept; asks for confirmation every time; never unattended). With "
                        "--upload-method url the --video-url is the video (no local file needed).")
    p.add_argument("--batch", metavar="DAY",
                   help="daily batch: the LipSync videos in R2 lipsync/<DAY>/ ('yesterday', 'today' or "
                        "YYYY-MM-DD, by TIMEZONE). Use with --dry-run, --next or --all; needs --account")
    p.add_argument("--next", action="store_true", help="with --batch: post only the next unposted video")
    p.add_argument("--all", action="store_true", help="with --batch: post every unposted video, in upload order")
    p.add_argument("--unattended", action="store_true",
                   help="with --batch DAY --all: no confirmation prompt (for the scheduled 08:00 run)")
    p.add_argument("--yes", action="store_true",
                   help="skip the 'post' confirmation (never skips the extra --public confirmation)")
    args = p.parse_args(argv)
    marking = args.mark_published is not None or args.mark_not_published
    if args.batch is not None:
        if not args.account:
            p.error("--batch requires an explicit --account <profile>")
        if args.dry_run + args.next + args.all != 1:
            p.error("--batch needs exactly one of --dry-run, --next or --all")
        if args.unattended and not args.all:
            p.error("--unattended is only used with --batch DAY --all")
        if (args.yes or args.public or args.test_mode or args.video_url or args.upload_method != RESUMABLE
                or args.job is not None or args.override_unknown_job is not None or args.check_auth
                or args.status or args.reconcile or marking or args.retry_publish
                or args.diagnostic_public_override or args.diagnostic_reattempt_override
                or args.diagnostic_trial_reattempt_override):
            p.error("--batch always posts Trial Reels via video_url; it takes only --account and one of "
                    "--dry-run, --next, --all (plus --unattended with --all)")
        return args
    if args.next or args.all or args.unattended:
        p.error("--next, --all and --unattended are only used with --batch DAY")
    if args.public and (args.check_auth or args.status or args.reconcile or marking or args.retry_publish):
        p.error("--public can only be used on its own or with --dry-run")
    if args.retry_publish and (args.job is None or not args.account):
        p.error("--retry-publish requires --account <profile> and --job <ID>")
    if args.retry_publish and args.yes:
        p.error("--retry-publish always asks for its own confirmation; --yes is not accepted")
    if args.upload_method == URL:
        if not args.video_url:
            p.error("--upload-method url requires --video-url URL")
        if not args.account:
            p.error("--upload-method url requires an explicit --account <profile>")
        if args.public and not args.test_mode:
            p.error("--upload-method url only publishes Trial Reels; --public is not accepted")
        if args.yes:
            p.error("--upload-method url always asks you to type the account name; --yes is not accepted")
        if args.check_auth or args.status or args.reconcile or marking or args.retry_publish:
            p.error("--upload-method url can only be used on its own or with --dry-run")
    elif args.video_url:
        p.error("--video-url is only used with --upload-method url")
    if (args.diagnostic_public_override + args.diagnostic_reattempt_override
            + args.diagnostic_trial_reattempt_override) > 1:
        p.error("use only one diagnostic override at a time")
    if args.diagnostic_trial_reattempt_override:
        if not args.account or args.public:
            p.error("--diagnostic-trial-reattempt-override requires --account <profile> and never --public")
        if (args.dry_run or args.yes or args.job is not None or args.upload_method != RESUMABLE
                or args.override_unknown_job is not None):
            p.error("--diagnostic-trial-reattempt-override is a live, resumable-only command; it asks before "
                    "posting and accepts no --dry-run, --yes, --job, --upload-method url or other override")
    if args.diagnostic_reattempt_override:
        if not args.account or not args.public:
            p.error("--diagnostic-reattempt-override requires --account <profile> and --public")
        if (args.dry_run or args.yes or args.job is not None or args.upload_method != RESUMABLE
                or args.override_unknown_job is not None):
            p.error("--diagnostic-reattempt-override is a live, resumable-only command; it asks before posting "
                    "and accepts no --dry-run, --yes, --job, --upload-method url or other override")
    if (not args.public and args.upload_method == RESUMABLE and not models.RESUMABLE_TRIAL_ALLOWED
            and not (args.check_auth or args.status or args.reconcile or marking or args.retry_publish)):
        p.error(TRIAL_NEEDS_URL)
    if args.diagnostic_public_override:
        if not args.account or not args.public:
            p.error("--diagnostic-public-override requires --account <profile> and --public")
        if (args.dry_run or args.yes or args.job is not None or args.upload_method != RESUMABLE
                or args.override_unknown_job is not None):
            p.error("--diagnostic-public-override is a live, resumable-only command; it asks before posting and "
                    "accepts no --dry-run, --yes, --job, --upload-method url or other override")
    if args.test_mode:
        if not args.account:
            p.error("--test-mode requires an explicit --account <profile>")
        if (args.yes or args.dry_run or args.job is not None or args.override_unknown_job is not None
                or args.diagnostic_public_override or args.diagnostic_reattempt_override
                or args.diagnostic_trial_reattempt_override or args.check_auth or args.status or args.reconcile
                or marking or args.retry_publish):
            p.error("--test-mode is a live, interactive command on its own: no --yes, --dry-run, --job, "
                    "overrides or maintenance commands")
    if args.override_unknown_job is not None:
        if args.upload_method != URL or not args.account:
            p.error("--override-unknown-job requires --account <profile> --upload-method url --video-url URL")
        if args.dry_run or args.job is not None:
            p.error("--override-unknown-job is a live command only; it shows everything and asks before posting")
    return args


def main(argv: Optional[List[str]] = None, *, config: Optional[Config] = None,
         client_factory=MetaClient, input_fn=input, remuxer_factory=None,
         config_loader=load_config, hosted_inspector=inspect_hosted_video, batch_kwargs=None) -> int:
    batch_kwargs = batch_kwargs or {}
    args = parse_args(argv)
    try:
        config = config or config_loader(profile=args.account)
        if args.account and config.profile != args.account:  # never run against a different account
            raise PublisherError(f"--account {args.account} was requested but credentials came from "
                                 f"{config.account_label}. Nothing was done.")
        setup_logging(config)
        out = make_out()
        remuxer = (remuxer_factory or (lambda c: Remuxer(c.tmp_dir)))(config)

        if args.status:
            return cmd_status(config, out)
        if args.batch is not None:
            action = DRY_RUN if args.dry_run else NEXT if args.next else ALL
            return cmd_batch(config, out, client_factory, input_fn, hosted_inspector, action=action,
                             day_arg=args.batch, unattended=args.unattended, **batch_kwargs)
        mode = PUBLIC if args.public else TRIAL
        if args.test_mode:
            return cmd_test_publish(config, out, client_factory, input_fn, remuxer, hosted_inspector, mode,
                                    args.upload_method, args.video_url)
        if args.diagnostic_public_override:
            return cmd_diagnostic_public(config, out, client_factory, input_fn, remuxer)
        if args.diagnostic_reattempt_override:
            return cmd_diagnostic_public(config, out, client_factory, input_fn, remuxer, reattempt=True)
        if args.diagnostic_trial_reattempt_override:
            return cmd_diagnostic_public(config, out, client_factory, input_fn, remuxer, trial_reattempt=True)
        if args.override_unknown_job is not None:
            return cmd_override_publish_url(config, out, client_factory, input_fn, remuxer, hosted_inspector,
                                            args.video_url, args.override_unknown_job)
        if args.upload_method == URL:
            if args.dry_run:
                return cmd_dry_run_url(config, out, client_factory, remuxer, hosted_inspector, args.video_url)
            return cmd_publish_url(config, out, client_factory, input_fn, remuxer, hosted_inspector,
                                   args.video_url)
        if args.dry_run:
            return cmd_dry_run(config, out, client_factory, remuxer, mode)
        if args.check_auth:
            check_auth(client_factory(config), out)
            out("Auth check passed. No media was created or published.")
            return 0
        if args.reconcile or args.mark_published is not None or args.mark_not_published:
            client = client_factory(config)
            with process_lock(config.lock_path):
                store = StateStore(config.state_path)
                if args.reconcile:
                    reconcile(client, store, out)
                elif args.mark_published is not None:
                    mark_published(client, store, args.mark_published or None, out, job_id=args.job)
                else:
                    mark_not_published(client, store, out, job_id=args.job)
            return 0
        if args.retry_publish:
            client = client_factory(config)
            with process_lock(config.lock_path):
                store = StateStore(config.state_path)
                return retry_publish(client, store, out, job_id=args.job, input_fn=input_fn,
                                     interactive=sys.stdin.isatty())
        return cmd_publish(config, out, client_factory, args.yes, input_fn, remuxer, mode)
    except PublisherError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        if logging.getLogger().handlers:  # only once file logging is set up; avoids a 2nd stderr copy
            logging.getLogger("trial_reels").error("%s", e)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


def cmd_status(config: Config, out) -> int:
    if not config.state_path.exists():
        out("No local state yet; nothing has been published by this tool.")
        return 0
    store = StateStore(config.state_path)
    rows = store.all()
    if not rows:
        out("No jobs recorded.")
    for r in rows:
        who = f"@{r.account_username}" if r.account_username else "@?"
        flag = "  [UNRESOLVED: account paused]" if is_uncertain(r) else ""
        phase = f" ({r.failure_phase})" if r.state == "FAILED" and r.failure_phase else ""
        method = " via url" if r.upload_method == "url" else ""
        if r.state == "PUBLISHED" and r.publish_mode == "TRIAL":
            flag += "  [trial requested; trial status NOT verified by API]"
        out(f"#{r.id} {r.updated_at}  {r.publish_mode:<6} {r.state}{phase}{method}  {r.relative_path}  "
            f"account={r.account_id} {who} profile={r.account_profile or '-'}  "
            f"container={r.container_id or '-'}  media={r.media_id or '-'}  {r.permalink or ''}{flag}")
        if r.last_error and r.state != "PUBLISHED":
            out(f"    last error: {r.last_error.splitlines()[0][:200]}")
    for o in store.overrides():
        new = store.get(o["new_job_key"])
        out(f"override #{o['id']} {o['confirmed_at']}  {o['kind']}  blocker job #{o['blocker_job_id']} (unchanged)  "
            f"-> new job #{new.id if new else '-'}  publish claimed {o['publish_claimed_at'] or 'no'}")
    for o in store.diagnostic_overrides():
        new = store.get(o["new_job_key"])
        held = ", ".join(f"#{b['blocker_job_id']} ({b['blocker_container_status']})" for b in o["blockers"])
        out(f"diagnostic override #{o['id']} {o['confirmed_at']}  {o['kind']}  {o['publish_mode']}/{o['upload_method']}  "
            f"blockers left unchanged: {held or 'none'}  "
            + (f"allowed duplicate of job #{dupe.id}  " if (dupe := store.get(o.get("allowed_duplicate_job_key") or "")) else "")
            + f"-> new job #{new.id if new else '-'}  "
            f"publish claimed {o['publish_claimed_at'] or 'no'}")
    return 0


def cmd_dry_run(config: Config, out, client_factory, remuxer: Remuxer, mode: str = TRIAL) -> int:
    out("DRY RUN: no media container will be created and nothing will be published.")
    path, report = discover_and_validate(config, SingleFileSource(config.videos_dir))
    describe_video(out, config, path, report, ffmpeg_available=remuxer.available, mode=mode)
    describe_request(out, config, mode, "temporary fast-start copy of the original" if report.needs_faststart
                     else "the original file, byte for byte")
    if mode == PUBLIC:
        out("Visibility:    PUBLIC: a real run posts a regular Reel visible to your followers and "
            "potentially other Instagram users.")
    blocked = report.needs_faststart and not remuxer.available
    if not (config.ig_user_id and config.access_token):
        out("Account:       not configured (.env missing IG_USER_ID / IG_ACCESS_TOKEN)")
        if blocked:
            out("NOT READY:     install ffmpeg (or re-export the video with fast start) before publishing.")
            return 2
        out("Ready:         local checks passed; configure .env next.")
        return 0
    job = build_job(config, path, report, mode)
    if config.state_path.exists():
        store = StateStore(config.state_path)
        rec = store.get(job.job_key)
        if rec:
            out(f"Local state:   {rec.publish_mode} {rec.state}" + (f" (media {rec.media_id})" if rec.media_id else ""))
        blockers = [b for b in store.account_blockers(job.account_id) if b.job_key != job.job_key]
        if rec and is_uncertain(rec):
            blockers.insert(0, rec)
        if blockers:
            out("NOT READY:     " + account_block_message(blockers, job.account_id))
            blocked = True
        dup = find_duplicate(store, job)
        if dup is not None and not (rec and is_uncertain(rec)):
            out("NOT READY:     " + duplicate_message(dup))
            blocked = True
    out("Read-only account check:")
    check_auth(client_factory(config), out)
    if blocked:
        if report.needs_faststart and not remuxer.available:
            out("NOT READY:     install ffmpeg (or re-export the video with fast start) before publishing.")
        return 2
    out(f"Ready:         a real run would create a {mode} reel container and publish it once.")
    return 0


def cmd_publish(config: Config, out, client_factory, assume_yes: bool, input_fn, remuxer: Remuxer,
                mode: str = TRIAL) -> int:
    config.require_credentials()
    with process_lock(config.lock_path):
        stale = remuxer.cleanup_stale()  # leftovers from a crashed run; safe under the lock
        if stale:
            logging.getLogger("trial_reels").info("removed %d stale temp upload dir(s)", stale)
        store = StateStore(config.state_path)
        path, report = discover_and_validate(config, SingleFileSource(config.videos_dir))
        job = build_job(config, path, report, mode)
        rec = store.get(job.job_key)
        client = client_factory(config)

        if not (rec and is_blocking(rec)):
            # local checks first: no network call when the account is paused or the video is a duplicate
            check_account_not_blocked(store, job.account_id, own_job_key=job.job_key)
            dup = find_duplicate(store, job)  # local: same video live in the other mode?
            if dup is not None:
                raise PublisherError(duplicate_message(dup))
            if mode == PUBLIC and not sys.stdin.isatty():
                raise PublisherError("--public needs an interactive terminal for its extra confirmation. "
                                     "Nothing was posted.")
            remuxer.ensure_possible(job)    # local: stop before any network call if ffmpeg is needed
            info = check_auth(client, out)  # step B preflight, read-only: shows @username + numeric ID
            left = remaining_quota(info["limit"])
            if left is not None and left <= 0:
                raise PublisherError("Meta reports no remaining publishing quota. Nothing was posted.")
            describe_video(out, config, path, report, ffmpeg_available=remuxer.available, mode=mode)
            username = info["account"].get("username")
            out("")
            out("About to publish:")
            out(f"  Credentials:  {config.account_label}")
            out(f"  Account:      @{username} (IG ID {info['account'].get('id')})  [verified with Meta]")
            out(f"  File:         {path.name}")
            out(f"  Posting mode: {'TRIAL / ' + config.graduation_strategy if mode == TRIAL else 'PUBLIC (regular Reel)'}")
            kind = "a Trial Reel" if mode == TRIAL else "a PUBLIC Reel"
            if not assume_yes and sys.stdin.isatty():
                answer = input_fn(f"Post this as {kind} to @{username}? Type 'post' to continue: ")
                if answer.strip().lower() != "post":
                    out("Cancelled. Nothing was posted.")
                    return 1
            if mode == PUBLIC and not confirm_public(out, input_fn, username, info["account"].get("id")):
                out("Cancelled. Nothing was posted.")
                return 1
        relative = os.path.relpath(path, config.videos_dir)
        result = publish_job(job, client=client, store=store, out=out, relative_path=relative,
                             remuxer=remuxer)
        if result.outcome == "PUBLISHED":
            if mode == TRIAL:  # unreachable while TRIAL needs --upload-method url; kept honest anyway
                out("media_publish succeeded. Trial status is NOT verified (see 'Confirmed mode' above).")
            else:
                out("Posted as a regular PUBLIC Reel (no trial). It is visible to your followers.")
            return 0
        if result.outcome == "ALREADY_PUBLISHED":
            return 0
        return 3


# ------------------------------------------------------------------ URL method
def describe_hosted(out, hv: HostedVideo, source_name: Optional[str]) -> None:
    r = hv.report
    out(f"Hosted video:  {hv.url}")
    out(f"  HTTP:        200 direct (no redirect), anonymous request (no cookies/auth); host {hv.host} -> "
        f"{', '.join(hv.addresses)} (public)")
    out(f"  Type/size:   {hv.content_type}, {hv.content_length} bytes ({human_size(hv.content_length)}); "
        f"MP4 'ftyp' box, moov at the front")
    out(f"  sha256:      {hv.sha256}")
    if hv.headers:
        out("  Headers:     " + ", ".join(f"{k}: {v}" for k, v in sorted(hv.headers.items())))
    if r.metadata_checked:
        dims = f"{r.width}x{r.height}" if r.width else "?"
        dur = f"{r.duration_s:.2f}s" if r.duration_s is not None else "?"
        fps = f"{r.fps:.2f} fps" if r.fps else "? fps"
        out(f"  Video:       {dur}, {dims}, {fps}, {r.video_codec}/{r.audio_codec or 'no audio'}")
    for w in r.warnings:
        out(f"  Warning:     {w}")
    if source_name is None:
        out("  Identity:    test mode: the hosted bytes ARE the video; the sha256 above identifies this attempt.")
    elif hv.identity == "MATCH":
        out(f"  Identity:    MATCH: video stream sha256 {hv.stream_sha256[:16]}… equals {source_name}'s, so this "
            "is a stream copy of the original. State and duplicate checks use the original.")
    elif hv.identity == "MISMATCH":
        out(f"  Identity:    MISMATCH: video stream {hv.stream_sha256[:16]}… differs from {source_name}'s "
            f"{(hv.source_stream_sha256 or '')[:16]}…. This URL is not a stream copy of the local video.")
    else:
        out("  Identity:    NOT CHECKED: ffmpeg is needed to compare the hosted video stream with the original.")


def _url_preflight(config: Config, out, remuxer: Remuxer, inspector, video_url: str):
    path, report = discover_and_validate(config, SingleFileSource(config.videos_dir))
    out(f"Original:      {path}  ({human_size(report.size_bytes)}, sha256 {report.sha256[:12]}…)  [identity]")
    hv = inspector(video_url, tmp_root=config.tmp_dir, source_path=path, ffmpeg=remuxer.ffmpeg)
    describe_hosted(out, hv, path.name)
    return path, report, hv


def _lock_status(store: Optional[StateStore], account_id: str, content_hash: str, own_key: Optional[str]):
    if store is None:
        return [], None
    blockers = [b for b in store.account_blockers(account_id) if b.job_key != own_key]
    own = store.get(own_key) if own_key else None
    if own and is_uncertain(own):
        blockers.insert(0, own)
    dup = next((b for b in store.find_blocking(account_id, content_hash) if b.job_key != own_key), None)
    return blockers, dup


def cmd_dry_run_url(config: Config, out, client_factory, remuxer: Remuxer, inspector, video_url: str) -> int:
    out("DRY RUN (upload method: url): no media container will be created and nothing will be published.")
    out("Read-only account check:")
    check_auth(client_factory(config), out)
    blocked = False
    hv = None
    try:
        path, report, hv = _url_preflight(config, out, remuxer, inspector, video_url)
    except PublisherError as e:
        out(f"NOT READY:     {e}")
        return 2
    if not hv.identity_ok:
        out("NOT READY:     the hosted video could not be tied to the original local video (see Identity).")
        blocked = True
    out(f"Posting mode:  TRIAL / {config.graduation_strategy} via video_url (trial_params included; never "
        "falls back to a public Reel)")
    describe_url_request(out, config, TRIAL, video_url)
    store = StateStore(config.state_path) if config.state_path.exists() else None
    own_key = build_job(config, path, report, TRIAL, URL, hv).job_key if hv.identity_ok else None
    blockers, dup = _lock_status(store, config.ig_user_id, report.sha256, own_key)
    if blockers:
        out("Publishing lock: PAUSED")
        out("LIVE TEST BLOCKED: " + account_block_message(blockers, config.ig_user_id))
        blocked = True
    else:
        out(f"Publishing lock: none (no unresolved publication attempt for IG ID {config.ig_user_id})")
    if dup is not None:
        out("NOT READY:     " + duplicate_message(dup))
        blocked = True
    if blocked:
        return 2
    out("Ready:         a real run would create one TRIAL reel container from video_url and publish it once.")
    return 0


def cmd_publish_url(config: Config, out, client_factory, input_fn, remuxer: Remuxer, inspector,
                    video_url: str) -> int:
    config.require_credentials()
    with process_lock(config.lock_path):
        store = StateStore(config.state_path)
        path, report = discover_and_validate(config, SingleFileSource(config.videos_dir))
        # Local state first: no network call at all while the account is paused or the video is live.
        # The account lock is authoritative regardless of which file/URL/method is used.
        check_account_not_blocked(store, config.ig_user_id)
        dup = store.find_blocking(config.ig_user_id, report.sha256)
        if dup:
            raise PublisherError(duplicate_message(dup[0]))
        if not sys.stdin.isatty():
            raise PublisherError("--upload-method url needs an interactive terminal for its confirmation. "
                                 "Nothing was posted.")
        out(f"Original:      {path}  ({human_size(report.size_bytes)}, sha256 {report.sha256[:12]}…)  [identity]")
        hv = inspector(video_url, tmp_root=config.tmp_dir, source_path=path, ffmpeg=remuxer.ffmpeg)
        describe_hosted(out, hv, path.name)
        if not hv.identity_ok:
            raise PublisherError("The hosted video could not be proven to be a stream copy of the local video "
                                 "(ffmpeg missing, or the video streams differ). Nothing was posted.")
        job = build_job(config, path, report, TRIAL, URL, hv)
        client = client_factory(config)
        info = check_auth(client, out)  # read-only: shows @username + numeric ID
        left = remaining_quota(info["limit"])
        if left is not None and left <= 0:
            raise PublisherError("Meta reports no remaining publishing quota. Nothing was posted.")
        username = info["account"].get("username") or ""
        out("")
        out("About to publish:")
        out(f"  Credentials:   {config.account_label}")
        out(f"  Account:       @{username} (IG ID {info['account'].get('id')})  [verified with Meta]")
        out(f"  Original:      {path.name}")
        out(f"  video_url:     {video_url}")
        out(f"  Posting mode:  TRIAL / {config.graduation_strategy} via video_url (no upload_type, no upload)")
        answer = input_fn(f"Type the account name '{username}' to post this Trial Reel to @{username}: ")
        if not username or answer.strip().lstrip("@") != username:
            out("Cancelled. Nothing was posted.")
            return 1
        result = publish_job(job, client=client, store=store, out=out,
                             relative_path=os.path.relpath(path, config.videos_dir), remuxer=remuxer)
        if result.outcome == "PUBLISHED":
            out("media_publish succeeded. Trial status is NOT verified (see 'Confirmed mode' above); check the "
                "Instagram app before treating this as a Trial Reel.")
            return 0
        return 0 if result.outcome == "ALREADY_PUBLISHED" else 3


def cmd_test_publish(config: Config, out, client_factory, input_fn, remuxer: Remuxer, inspector, mode: str,
                     upload_method: str, video_url: Optional[str]) -> int:
    """--test-mode: ONE new manual attempt that ignores earlier locks and duplicates. History is never changed:
    the attempt gets its own job row (next attempt number) and its own TEST_MODE: confirmation row listing
    what it ignored. media_publish is sent once; an unclear reply leaves the new job UNKNOWN."""
    import dataclasses
    import json
    from urllib.parse import urlparse
    from pathlib import Path
    from .meta_client import reel_container_params, url_reel_container_body
    from .models import VideoJob
    config.require_credentials()
    if not sys.stdin.isatty():
        raise PublisherError("--test-mode needs an interactive terminal; it never runs unattended. Nothing was posted.")
    with process_lock(config.lock_path):
        store = StateStore(config.state_path)
        if upload_method == URL:  # the hosted video IS the video: no local file, identity = downloaded bytes
            hv = inspector(video_url, tmp_root=config.tmp_dir, source_path=None, ffmpeg=None)
            describe_hosted(out, hv, None)
            u = urlparse(video_url)
            path = Path("/remote") / u.netloc / u.path.lstrip("/")
            job = VideoJob(path=path, account_id=config.ig_user_id, caption=config.default_caption,
                           graduation_strategy="MANUAL" if mode == TRIAL else None, sha256=hv.sha256,
                           size_bytes=hv.content_length, publish_mode=mode, upload_method=URL, video_url=video_url,
                           hosted_sha256=hv.sha256, hosted_size_bytes=hv.content_length,
                           identity_evidence=f"test mode: remote video is authoritative (sha256 {hv.sha256})")
            relative = Path(u.path).name or u.netloc
            request = url_reel_container_body(job.caption, mode, job.graduation_strategy, video_url)
        else:
            path, report = discover_and_validate(config, SingleFileSource(config.videos_dir))
            job = build_job(config, path, report, mode)  # TRIAL + resumable is refused here
            remuxer.ensure_possible(job)
            relative = os.path.relpath(path, config.videos_dir)
            request = reel_container_params(job.caption, mode, job.graduation_strategy)
        job = dataclasses.replace(job, attempt=store.next_attempt(
            job.account_id, job.sha256, str(job.path), job.publish_mode, job.upload_method))

        client = client_factory(config)
        info = check_auth(client, out)  # read-only; enforces the configured IG ID
        left = remaining_quota(info["limit"])
        if left is not None and left <= 0:
            raise PublisherError("Meta reports no remaining publishing quota. Nothing was posted.")
        username = info["account"].get("username") or ""
        ignored = {r.id: r for r in store.account_blockers(job.account_id)}
        same = store.find_blocking(job.account_id, job.sha256)
        # also jobs that UPLOADED these exact bytes (e.g. an earlier URL job keyed on its local original)
        same += [r for r in store.all() if r.account_id == job.account_id and r.upload_sha256 == job.sha256
                 and is_blocking(r) and r.id not in {x.id for x in same}]
        ignored.update({r.id: r for r in same})

        out("")
        out("=" * 76)
        out("  TEST MODE: manual experiment. Earlier locks and duplicate checks are IGNORED for this attempt.")
        out(f"  Account:      @{username} (IG ID {info['account'].get('id')})  [verified with Meta]")
        out(f"  Credentials:  {config.account_label}")
        out(f"  Video:        {video_url if upload_method == URL else relative}  sha256 {job.sha256[:12]}…")
        out(f"  Request:      POST /media {json.dumps(request)}")
        if mode == TRIAL:
            out("  Visibility:   TRIAL requested (MANUAL graduation). Never falls back to a normal Reel. The API cannot")
            out("                verify trial status; check the app afterwards.")
        else:
            out("  Visibility:   PUBLIC: shown to your followers and potentially anyone on Instagram.")
        for r in sorted(ignored.values(), key=lambda r: r.id):
            why = "same video" if r.id in {x.id for x in same} else "unresolved"
            out(f"  Ignored:      job #{r.id} {r.publish_mode} {r.state} ({why}) - not changed")
        out(f"  Duplicate risk: {'YES: this video is or may already be on the account' if same else 'none known'}.")
        out(f"  New attempt:  attempt {job.attempt}; media_publish is sent once and never retried.")
        out("=" * 76)
        a1 = input_fn(f"Type the account name '{username}' to continue: ")
        phrase = f"TEST {mode} REEL, DUPLICATES POSSIBLE"
        a2 = input_fn(f"Type '{phrase}' to post: ") if username and a1.strip().lstrip("@") == username else ""
        if a2.strip() != phrase:
            out("Cancelled. Nothing was posted.")
            return 1
        ov = store.record_diagnostic_override(
            kind=TEST_MODE_PREFIX + job.job_key, account_id=job.account_id, account_profile=config.profile,
            account_username=username, content_hash=job.sha256, publish_mode=mode, upload_method=upload_method,
            new_job_key=job.job_key, confirmation=f"{username} / {phrase}",
            blockers=[(r, f"IGNORED_IN_TEST_MODE:{r.state}") for r in ignored.values()])
        try:
            result = publish_job(job, client=client, store=store, out=out, relative_path=relative,
                                 remuxer=remuxer, test=ov)
        finally:
            store.note(job.job_key, f"test mode #{ov['id']} request: POST /{job.account_id}/media "
                                    f"{json.dumps(request)}")
        rec = store.get(job.job_key)
        out(f"Test attempt recorded as job #{rec.id}: {rec.state}, container {rec.container_id or '-'}, "
            f"media {rec.media_id or '-'}.")
        return 0 if result.outcome == "PUBLISHED" else 3


def confirm_public(out, input_fn, username, ig_id) -> bool:
    """Extra confirmation for --public. Not skippable with --yes."""
    out("")
    out("=" * 72)
    out("  PUBLIC REEL: this is NOT a trial.")
    out(f"  Account: @{username} (IG ID {ig_id})")
    out("  It will be posted as a regular Reel, visible to your FOLLOWERS and potentially")
    out("  other Instagram users (Reels tab, Explore, your profile grid).")
    out("  It will not be limited to non-followers, and this tool cannot undo it.")
    out("=" * 72)
    answer = input_fn(f"Type PUBLIC to post to @{username} for everyone to see: ")
    return answer.strip() == "PUBLIC"
