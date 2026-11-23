# Trial Reel Publisher

Publishes a video to an Instagram Professional account as a **Trial Reel** (shown to non-followers first)
using only Meta's official Instagram Graph API (Facebook Login, `graph.facebook.com`).

**Working architecture:** Cloudflare R2 (public MP4 URL) → this Python publisher → Instagram Graph API
(`POST /{ig-user-id}/media` with `video_url` + `trial_params`, then `media_publish`) → Trial Reel.

Trial Reels **must** use the R2 / `video_url` method. Meta ignores `trial_params` with resumable uploads, so
the tool refuses that combination. It never falls back to a public Reel, and it never claims trial status is
verified (the API has no field for it). Check the Trial Reels section in the Instagram app afterwards.

## Quick start

**Requirements:** macOS or Linux, Python 3.9+, and `ffmpeg` (for the identity check in normal URL mode;
`brew install ffmpeg`).

```
git clone https://github.com/NashHawkins/portfolio.git
cd portfolio/trial-reels-publisher
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock     # exact tested versions (or requirements.txt for ranges)
```

**Configure** a credentials profile. These files are git-ignored; never commit them.

```
cp .env.example .env.personal          # or .env.test for a second account
chmod 600 .env.personal
# edit: IG_USER_ID (numeric Instagram Professional ID) and IG_ACCESS_TOKEN (Facebook Login token with
# instagram_basic + instagram_content_publish; see "2. One-time Meta setup")
.venv/bin/python publish.py --account personal --check-auth      # read-only: account, permissions, quota
```

**Host the video on Cloudflare R2.** Make an MP4 with the `moov` atom at the front, then upload it to an R2
bucket with public access (r2.dev or a custom domain). The publisher only needs the public `https` URL; it
never needs R2 credentials.

```
ffmpeg -i IMG_1234.MOV -c copy -movflags +faststart IMG_1234_cloud.mp4
# upload IMG_1234_cloud.mp4 to your R2 bucket -> https://pub-<id>.r2.dev/IMG_1234_cloud.mp4
```

**Publish a Trial Reel (normal mode, with full locks and duplicate protection).** Put the original video
(the only file) in `~/Documents/Trial-Reels/` (`TRIAL_REELS_DIR`). The tool proves the R2 copy is a stream
copy of it.

```
URL=https://pub-<id>.r2.dev/IMG_1234_cloud.mp4
.venv/bin/python publish.py --account personal --upload-method url --video-url "$URL" --dry-run   # never posts
.venv/bin/python publish.py --account personal --upload-method url --video-url "$URL"             # asks first
```

**Manual API experiments (`--test-mode`).** The R2 URL itself is the video (no local file). The run ignores
earlier locks and duplicates, records every attempt as a new job, and asks for confirmation every time.

```
.venv/bin/python publish.py --account personal --test-mode --upload-method url --video-url "$URL"
```

**After publishing**, the output shows `Requested mode` separately from `Confirmed mode` (`NOT VERIFIED`, or
`NOT A TRIAL` if Meta reports `is_shared_to_feed=true`). Confirm in the Instagram app.

**Other commands:** `--status` (local history), `--reconcile` (read-only evidence for unresolved attempts),
`--public` (a regular public Reel, with extra confirmation). An unclear `media_publish` reply is never retried:
the job becomes UNKNOWN and pauses normal publishing to that account until it is reconciled.

**Tests (offline, no network):** `.venv/bin/python -m pytest`

**What stays local (git-ignored):** `.env*` (except `.env.example`), `data/` (SQLite state and backups),
`logs/`, videos (`*.mp4`, `*.mov`), `.venv/`.

---

# Detailed reference

The sections below are the full design notes and history (Stage 1). Commands without `--upload-method url`
that publish **Trial** Reels describe the original resumable flow, which is now refused for Trial Reels.

## Compatibility notes (checked against Meta docs, 10 Nov 2026)

| Topic | Finding |
|---|---|
| **`trial_params` + `upload_type=resumable`** | **Update, first live run (10 Nov 2026):** Meta accepted the combination and rejected on *account eligibility* instead: `code=10, error_subcode=2207081`, "does not meet the trial reel follower requirement". So the parameter is understood, and an account needs enough followers before it can make Trial Reels. The tool now shows a specific hint for that error. *Original note:* **Not confirmed by Meta in either direction.** The IG User Media reference lists `trial_params` (`graduation_strategy`: `MANUAL` / `SS_PERFORMANCE`, requires `media_type=REELS`) with no restriction on upload type, and its only example uses `video_url`. The *Resumable Uploads* page lists the optional Reels parameters as `audio_name, caption, collaborators, cover_url, is_ai_generated, location_id, thumb_offset, user_tags` and says *"Other parameters are not supported."* `trial_params` isn't on that list. The official sample (v3.0.0) never sends `trial_params`. **Your first live run is the real compatibility test.** If Meta rejects it, the tool stops with Meta's error and posts nothing. See *Residual risk* below. |
| Login flow / host | Resumable upload is *"only for apps that have implemented Facebook Login for Business."* So this tool uses `graph.facebook.com` + `rupload.facebook.com`, **not** `graph.instagram.com` (Instagram Login). |
| Upload protocol | `POST https://rupload.facebook.com/ig-api-upload/{version}/{container_id}` with headers `Authorization: OAuth <token>`, `offset: 0`, `file_size: <bytes>`, raw bytes as the body. Success is `{"success": true, "message": "Upload successful."}`. Errors come back as a `debug_info` envelope. This matches the sample's `index.js`. |
| Permissions | Facebook Login: `instagram_basic`, `instagram_content_publish`, `pages_read_engagement` (the sample also requests `pages_show_list` and `business_management`). If you have a Business Manager role on the Page, Meta also requires `ads_management` and `ads_read`. |
| API version | Meta's reference lists **v27.0** as latest. That's the default here, and you can change it in `.env`. |
| Video limits | MOV/MP4, H.264/HEVC, AAC ≤48 kHz, 23–60 fps, ≤1920 px wide, aspect 0.01:1–10:1 (9:16 recommended), ≤25 Mbps, 3 s–15 min, **300 MB**. (The sample README still says 1 GB; the tool uses the newer, smaller limit.) |
| Rate limit | The Content Publishing guide says 100 API posts per rolling 24 h (the same page also says 50 for carousels, and the sample README says 25). The tool trusts only `GET /{ig-user-id}/content_publishing_limit` for your account. A general quota doesn't guarantee Trial Reel eligibility or a separate trial limit. |
| Status polling | Container `status_code`: `IN_PROGRESS`, `FINISHED`, `ERROR`, `EXPIRED` (24 h), `PUBLISHED`. The tool polls every 5 s for up to 5 min (configurable). |

**Residual risk to know before the first live run:** if Meta *silently ignored* `trial_params` on
a resumable container instead of rejecting it, the result would be a normal Reel shown to followers.
The API gives no way to read trial status back, so the tool can't detect this. That's why you have
to check the first post in the Instagram app. If it isn't marked as a trial, stop using this upload
path and switch to the hosted `video_url` route (Meta's documented trial example), which is
the planned fallback.

---

## 1. Local setup (once)

```bash
cd ~/trial-reels-publisher
python3 -m venv .venv            # already done by Claude Code
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q    # offline tests, no network, no posting
brew install ffmpeg              # optional but recommended: enables codec/duration checks (needs Homebrew)
```

Use `.venv/bin/python publish.py …`, or run `source .venv/bin/activate` first and then use `python publish.py …`.
This Mac currently has only Apple's Python 3.9. That works fine. Python 3.11+ from python.org is optional.

**Supported files:** `.mp4` and `.mov`, in any letter case (`.MOV`, `.Mp4` …). Exactly one of them
must sit directly in `Trial-Reels/`. A `.MOV` and a converted `.mp4` of the same clip count as two
videos, and the tool refuses to guess between them. Subfolders and other file types are ignored.

### MOV files and "fast start"

Meta requires the video's index (the `moov` atom) to be at the **front** of the file. iPhone
`.MOV` files usually have it at the end. The tool checks this on every run:

* **Already at the front:** the original file is uploaded byte-for-byte.
* **At the end:** on a real run, the tool makes a temporary copy with
  `ffmpeg -i <original> -map 0:v:0 -map 0:a:0? -c copy -movflags +faststart <copy>.mp4`.
  This is a stream copy: nothing is re-encoded and quality is identical. The extra iPhone
  data/timecode tracks are left out. The copy goes in `data/tmp/`, never in `Trial-Reels/`. It's
  validated (structure, 300 MB limit, and ffprobe rules if installed) and checked to have the same
  duration as the original. It's uploaded and then **deleted as soon as the upload step ends**,
  whether the upload succeeded or failed. Any leftovers from a crashed run are removed at the start
  of the next run.
* **At the end, and ffmpeg is not installed:** `--dry-run` reports `NOT READY`. A real run stops
  **before any network request**, explains why, and posts nothing. The tool never installs software.
  Fix it by installing ffmpeg yourself (e.g. `brew install ffmpeg` with Homebrew, or a static build
  from ffmpeg.org with `FFMPEG_PATH=/path/to/ffmpeg` in `.env`). Or re-export the video with fast start
  turned on.

The original file is only ever read. The tool checks that its size, modification time and inode
are unchanged after the copy is made.

**Duplicate protection always follows the original file.** The job identity is
*(account, original file path, SHA-256 of the original file)*, never the temporary copy, so
re-running on the same `.MOV` reports "Already published" without running ffmpeg. The state database
also records the SHA-256 and size of the bytes actually uploaded (`upload_sha256`, `upload_remuxed=1`)
for auditing. After an uncertain (`UNKNOWN`) publish, the copy is still deleted, because Meta already
has the bytes and reconciliation only needs the container ID, which stays in state.
Caveat: if *you* replace the file with a different file (for example your own converted `.mp4`), that
counts as a new video and could be posted again.

### Public mode (`--public`, for testing)

Default runs are always Trial Reels, and `--public` never changes that default. With `--public`:

* **Request:** the container request is `media_type=REELS`, `upload_type=resumable` (plus `caption` if
  set). **`trial_params` is left out entirely.** It isn't sent with a different strategy. Without
  it, Meta creates a regular Reel. Everything else is identical: validation, `.mov`/fast-start copy,
  upload, polling, publish-once, state, error handling and redaction.
* **Preview:** `--public --dry-run` prints the exact requests (URL and every form field, token never
  shown) and posts nothing. A plain `--dry-run` prints the same thing for trial mode, including the
  `trial_params` value.
* **Account check:** before any prompt, the tool checks with Meta that the token belongs to `IG_USER_ID` and
  shows `@username (IG ID …)`.
* **Confirmations:** you type `post` (skippable with `--yes`), then a warning box saying the Reel will be
  visible to your **followers and potentially other Instagram users**, then you type `PUBLIC`
  exactly. `--yes` **never** skips the second confirmation. Without an interactive terminal (cron,
  pipes), `--public` refuses before any network request.
* **No fallback:** a Trial Reel request that Meta rejects (for example `TrialRejectedError`) is never
  retried as public. The mode is fixed for the whole job.

**One video, one post, across both modes.** Each job is stored with its `publish_mode`
(`TRIAL`/`PUBLIC`), and `--status` shows it. Before posting in either mode, the tool looks for any
job on the same account with the **same video bytes** (same SHA-256, at any path, in either mode)
that is `PUBLISHED`, `PUBLISHING` or `UNKNOWN`. If it finds one, it refuses with the earlier mode and
media ID, and posts nothing. This also blocks a renamed copy of a video that was already posted.
A job that `FAILED` (never published) doesn't block. There is intentionally **no override**: to test
public mode, use a different video from the one you trial-posted. For an `UNKNOWN` job, run
`--reconcile` first.

Existing state databases are upgraded automatically: earlier jobs are marked `TRIAL`, and trial job
IDs are unchanged, so earlier "already published" records still apply.

### Account profiles (`--account test` / `--account personal`)

Keep one credentials file per Instagram account in the project root:

| Command | Credentials file | Used for |
|---|---|---|
| *(no `--account`)* | `.env` | unchanged legacy behaviour |
| `--account test` | `.env.test` | your test account |
| `--account personal` | `.env.personal` | your personal account |

Each file uses the same names as `.env`: `IG_USER_ID` and `IG_ACCESS_TOKEN` (any other `.env.example`
setting is optional). **Set up once:**

```bash
cd ~/trial-reels-publisher
cp -n .env .env.test             # your existing test-account credentials; -n never overwrites
cp -n .env.example .env.personal # template for the personal account
chmod 600 .env.test .env.personal
open -e .env.personal            # paste the personal account's IG_USER_ID + IG_ACCESS_TOKEN, save
```

For the personal account, repeat *One-time Meta setup* steps 1–5 while logged in as that account. It
must be a Professional account linked to a Facebook Page, and your Meta app needs access to it (a
development-mode app only works for people with a role on the app). Then check both:

```bash
.venv/bin/python publish.py --account test --check-auth
.venv/bin/python publish.py --account personal --check-auth
```

Rules the tool enforces:

* **No silent switching.** With `--account X`, credentials come **only** from `.env.X`. `.env` isn't
  read, and `IG_USER_ID` / `IG_ACCESS_TOKEN` exported in your shell are ignored. A missing file,
  missing values, placeholders, a non-numeric ID, a symlink, or an unknown profile name stops the
  run before any network request.
* **Identity check before every publication.** Meta is asked which account the token belongs to. If
  the returned ID isn't exactly that file's `IG_USER_ID`, the run aborts with `Account mismatch`
  before a media container is created. It checks twice: once in the preflight, and again
  immediately before the container request.
* **Summary before you confirm:** credentials file, `@username (IG ID …)`, file name and posting mode,
  then the usual `post` prompt (plus `PUBLIC` for `--public`). `--status` shows the account, @username
  and profile for every job.
* **File permissions:** `.env`, `.env.test` and `.env.personal` are tightened to `600` (owner only) if
  they're readable by others, with a warning. The contents are never rewritten. You also get a
  warning if both profiles point at the same `IG_USER_ID`.
* **Secrets:** tokens are never printed, logged or stored in `data/state.sqlite3`. All credential
  files (`.env`, `.env.*`) are git-ignored, and only `.env.example` is meant to be committed.

**Duplicate protection is per account:** the scope is *(Instagram user ID, SHA-256 of the original
video)*. The same video can be posted once to the test account and once to the personal account.
A second post to the *same* account is refused, whether it's the same mode, the other mode
(`--public` vs trial), or a renamed copy. `UNKNOWN` jobs block only their own account, until
`--reconcile` resolves them (run it with the same `--account`). The database itself repeats this
check in the same statement that marks a job `PUBLISHING`.

**Records with an unknown account:** any job whose stored account ID is empty or not numeric is
kept as-is and never assigned to either account. If it's `PUBLISHED`, `PUBLISHING` or `UNKNOWN`, it
blocks that video on **every** account, and `--reconcile` lists it. (Earlier versions always stored a
numeric ID, so this shouldn't happen. If it does, use a different video or ask for a manual fix.)

**Database upgrade:** opening an older `data/state.sqlite3` first writes a backup copy
`data/state.sqlite3.bak-<UTC time>`, then adds the `account_profile` / `account_username` columns.
No rows or events are changed or deleted. Older jobs keep their numeric account ID, so they still
count against the right account, and their profile shows as `-`.

### Publishing outcomes, UNKNOWN jobs and account pauses

The rule is **fail closed**. Once `media_publish` may have reached Meta, a job can only become
`FAILED` on documented proof that nothing was published. Everything else is `UNKNOWN`.

**How a `media_publish` failure is classified** (`src/trial_reels/outcomes.py`). The decision uses the
full reply: HTTP status, Meta `code`, `error_subcode`, `is_transient`, type and message, plus whether
the request could have been sent. The HTTP status alone is never enough: Meta sent
`HTTP 400 / code -1 / subcode 2207085 "An internal server error occurred"` for a server fault.

| Reply | Result |
|---|---|
| Connection never opened (`ConnectTimeout`) | `FAILED` (`PUBLISH_REJECTED`): request not sent |
| Documented pre-publish rejection: `9007/2207027` (media not ready), `9/2207042` (daily limit) | `FAILED` (`PUBLISH_REJECTED`) |
| Auth `190`/`102` (any subcode). Permission `10`/`200` or rate limit `4`/`17`/`32`/`613` **with no subcode** | `FAILED` (`PUBLISH_REJECTED`) |
| Server-fault codes `-2`/`-1`/`1`/`2` (incl. **-1/2207085**), any 5xx, `is_transient: true`, missing or malformed code, non-JSON reply, success without a media id, read timeout, reset, cut-off reply, any undocumented code/subcode (e.g. `4/2207051`, `100/2207089`, `24/2207008`) | **`UNKNOWN`** |

Failures **before** `media_publish` (preparing the copy, creating the container, upload, processing)
are always definite: `FAILED` with `failure_phase = PRE_PUBLISH`. Every `FAILED` row carries a
`failure_phase` (`PRE_PUBLISH`, `PUBLISH_REJECTED` or `RECONCILED_NOT_PUBLISHED`). A `FAILED` row that had a
publish attempt but no such phase (written by older versions) is treated as unresolved.

**Account pause.** If *any* job on an account has an unresolved publication attempt (`PUBLISHING`,
`UNKNOWN`, or `FAILED` after a publish attempt without proof), the tool refuses **every** new post to
that account. That covers any video and either mode, now and for the future scheduler. It names the
job, its container and its state. `--check-auth`, `--dry-run`, `--status` and `--reconcile` keep
working, and other accounts aren't affected. The pause comes from the database, so it survives
restarts. The same check runs inside the single SQL statement that marks a job `PUBLISHING`. There
is no override flag.

**Reconcile** (`--reconcile`, read-only, GET requests only) reads the container's `status_code`, the
quota (supporting evidence only), and the latest 25 media (supporting only, because Trial Reels may not
be listed). It then gives a verdict:

| Container status (Meta's documented meaning) | Verdict | Allowed transition |
|---|---|---|
| `PUBLISHED`: "media object has been published" | `CONFIRMED_PUBLISHED` | `--mark-published [--job N]` (media id left **empty**, since the container doesn't expose it), or `--mark-published <MEDIA_ID>` if you found it |
| `EXPIRED`: not published within 24 h | `CONFIRMED_NOT_PUBLISHED` | `--mark-not-published [--job N]` |
| `ERROR`: "failed to complete the publishing process" | `CONFIRMED_NOT_PUBLISHED` | `--mark-not-published [--job N]` |
| `FINISHED`, `IN_PROGRESS`, unreadable, or anything else | `UNRESOLVED` | none: stays `UNKNOWN`. It shows the expected expiry time. `--mark-published <MEDIA_ID>` is accepted only for a media item that exists and was posted after the attempt |

`FINISHED` is never treated as proof either way. Each transition writes the evidence it relied on
into the job's event history, and no history is deleted.

**Diagnostics.** Every API step that fails, and every `media_publish` attempt, adds a row to the
`api_calls` table in `data/state.sqlite3`. Each row records:
- HTTP status, Meta code/subcode/type/message, `is_transient` and `fbtrace_id`;
- container ID, account ID and source SHA-256;
- attempt time, the container status before publishing, and the quota before the attempt;
- the outcome and the reason for its classification.

Messages are redacted; tokens are never stored.

**Database v4 migration** (automatic, with a `data/state.sqlite3.bak-<UTC>` backup first):
- adds `failure_phase` and `api_calls`;
- re-labels `FAILED` rows whose publish was never sent as `PRE_PUBLISH`;
- reclassifies `FAILED` rows whose publish *was* sent (`publish_attempts > 0`) to `UNKNOWN`.

It runs in one transaction, keeps every ID, timestamp and event, appends an explanatory event, and is
idempotent.

No retries: an `UNKNOWN` job is never retried automatically, a replacement container is never made
for it, and the tool never switches to `--public` or to another upload method by itself.

Known limit: Meta's spec also says "no edit lists". The tool can't check for edit lists, and a stream
copy keeps them. If Meta reports `status ERROR` for an iPhone clip, re-export it from Photos/iMovie.

### URL upload method (`--upload-method url`, experimental)

Default is `resumable` (the local file is uploaded to `rupload.facebook.com`). With `url`, Meta fetches a
publicly hosted MP4 itself, so no bytes are sent and `upload_type` is never included:

```bash
.venv/bin/python publish.py --account personal --upload-method url \
  --video-url "https://pub-example.r2.dev/IMG_3813_cloud.mp4" --dry-run
```

Request (`POST /{ig-user-id}/media`, `Content-Type: application/json`, token only in the header), shaped
like the trial example in Meta's Content Publishing guide:
`{"media_type":"REELS","video_url":"<url>","trial_params":{"graduation_strategy":"MANUAL"}}`. It's then
polled until `FINISHED` and published once with the same `media_publish` code and the same
UNKNOWN/fail-closed rules as resumable.

Rules:
- **Trial only.** `--account` is required. `--public` and `--yes` are rejected. You confirm by typing the
  account name Meta returns (for example `nashhawkins`).
- **Same identity and state as resumable.** The job's identity is the **original** video in the Trial-Reels
  folder (its path and SHA-256), so duplicate protection works across methods and modes, and the same
  SQLite database is used. URL jobs get their own row (`upload_method=url`, `video_url`), so earlier
  resumable jobs are never overwritten.
- **Proof the hosted file is this video.** Before anything is sent, the hosted file's video-stream packets
  (`ffmpeg -map 0:v:0 -c copy -f hash`) must hash identically to the original's. A stream copy passes and
  any other video fails. This needs ffmpeg.
- **Account pause applies first.** Any unresolved job on the account stops the run before any network
  call, whatever the file, URL or method.
- **Checks on the URL (read-only):**
  - https only; no credentials, port, query or fragment;
  - a DNS name that resolves only to public addresses (SSRF guard, rechecked before the download);
  - a direct HTTP 200 with no redirect followed, fetched without cookies, `.netrc` or proxy credentials;
  - `Content-Type: video/mp4`, a valid `Content-Length` of 300 MB or less, and no compression;
  - an MP4 `ftyp` box (not an HTML page) and exactly the advertised size;
  - moov at the front, then the usual ffprobe rules on a temporary copy in `data/tmp`, which is deleted
    afterwards.
- **Schema v5** added `upload_method` and `video_url`, and rebuilt the jobs table so its uniqueness rule
  includes the method. Every row, id, event and `api_calls` row is copied, after a timestamped backup.

### Manual retry of an existing container (`--retry-publish`)

One-off tool for job #3 (container `18657792691044336`, `@nashhawkins`), approved after the 2207085
investigation. It re-sends `media_publish` **once** for the container Meta already holds. It never creates a
container, uploads, remuxes, or reads the video.

```bash
python publish.py --account personal --retry-publish --job 3
```

Refuses (exit 2, nothing sent) unless all of these hold: the job is listed in `APPROVED_RETRIES`
(`src/trial_reels/retry.py`); the profile is `personal`, its `IG_USER_ID` is `17840000000000002`, and Meta
says the token belongs to that ID; the job is `UNKNOWN` on that container and hasn't been retried; no other
job on the account is unresolved; the terminal is interactive. The container must read `FINISHED` both
before the prompt and again right after you confirm. `PUBLISHED`, `EXPIRED`, `ERROR`, any other status or
an unreadable one stops it. You must type `RETRY 3`; `--yes` is rejected.

Outcomes: a media id from Meta marks the job `PUBLISHED` and lifts the pause. Anything else (2207085 again, a
documented rejection, no id, a dropped connection, Ctrl-C) keeps it `UNKNOWN` with the account paused. Each
attempt is logged in `api_calls` (`step=retry_publish`). `publish_started_at` keeps the original time. A second
retry is always refused; after the container expires, resolve it with `--reconcile` /
`--mark-not-published --job 3`.

## 2. One-time Meta setup

Meta's UI changes often. If a label below has moved, follow the nearest current equivalent.

1. **Instagram account.** In the Instagram app, make sure the account is a Professional account
   (Creator or Business) and that Trial Reels are available to it (try making one manually in the app
   once). Connect it to a **Facebook Page** you manage (Instagram: *Settings → Accounts Center*, or
   the Page's settings → *Linked accounts*).
2. **Meta app.** At developers.facebook.com → *My Apps* → *Create App*, choose the use case for
   managing Instagram content **with Facebook Login** (not the "Instagram Login / API setup with
   Instagram login" option, which doesn't support local-file resumable upload). While the app is in
   development mode, your own Facebook account must have a role on the app (it does if you created it).
   App Review / Advanced Access is only needed to post for accounts *other than* people with app roles.
3. **Token.** Open the **Graph API Explorer** (developers.facebook.com/tools/explorer). Select your app,
   select **User Token**, add permissions `instagram_basic`, `instagram_content_publish`,
   `pages_show_list`, `pages_read_engagement` (and `business_management`; add `ads_management` and
   `ads_read` if your Page access comes through Business Manager), then click **Generate Access Token**
   and approve the dialog, including your Page and Instagram account.
4. **Find your IG user ID.** In the Explorer, run
   `GET me/accounts?fields=name,instagram_business_account{id,username}`.
   The `instagram_business_account.id` (usually 17 digits starting `1784…`) is your **IG_USER_ID**.
   Don't use the Page ID, app ID, or your @username.
5. **Make the token last.** Explorer tokens expire in about 1–2 hours. Paste it into the **Access Token
   Debugger** (developers.facebook.com/tools/debug/accesstoken), check the scopes and the account, and
   click **Extend Access Token** for a long-lived token (about 60 days). Note the expiry date there:
   when it passes, `--check-auth` will fail with code 190 and you'll repeat steps 3–5.
6. **Create `.env`** (it stays in this folder, it's git-ignored, and it must never go inside `Trial-Reels/`):
   ```bash
   cp .env.example .env
   chmod 600 .env
   open -e .env        # paste IG_USER_ID and IG_ACCESS_TOKEN in TextEdit, save
   ```
   Don't paste the token into Claude, a terminal command line, or a screenshot.

## 3. First live test (you run this, not Claude)

```bash
.venv/bin/python publish.py --check-auth   # shows @username + IG ID; confirm it's the right account
.venv/bin/python publish.py --dry-run      # confirms the file, strategy, caption (empty)
.venv/bin/python publish.py                # type 'post' when asked
```

Expected output: account, container ID, upload, `Processing: IN_PROGRESS` → `FINISHED`, then
`Published: IG media ID …` and a permalink. **Then open the Reel in the Instagram app and confirm
it's marked as a trial Reel.** Running `publish.py` again for the same file reports
"Already published" and posts nothing.

## 4. Troubleshooting

| Message | Meaning / fix |
|---|---|
| `No .mp4 or .mov video found` / `Found 2 videos` | Exactly one `.mp4` or `.mov` must be directly inside `Trial-Reels/`. Subfolders are ignored. |
| `NOT READY` / `needs ffmpeg` | The `moov` atom is at the end of the file. Install ffmpeg or re-export with fast start (see *MOV files and "fast start"*). Nothing was posted. |
| `ffmpeg could not make a fast-start copy` | ffmpeg's error is shown. The original is unchanged. Re-export the video. |
| `iCloud has not downloaded` | In Finder, right-click the file → *Download Now*. |
| `Permission denied reading …` | System Settings → Privacy & Security → Files and Folders: allow Terminal to access Documents. |
| `code=190` | The token expired or is invalid. Repeat setup steps 3–5. |
| `code=10` / `code=200` | A permission is missing, or the app has no access to this account. |
| `This video was already published as a … Reel` | The same video (same bytes) is already live, or may be, in trial or public mode. Nothing was posted. Use a different video. |
| `--account X needs .env.X … does not exist` | Create that file (see *Account profiles*). Nothing else was used instead. |
| `Account mismatch` | The token in that profile belongs to a different Instagram account than its `IG_USER_ID`. Fix the file. Nothing was posted. |
| `does not meet the trial reel follower requirement` (subcode 2207081) | This account can't make Trial Reels yet. Nothing was posted, and the tool never switches to public by itself. |
| `--public needs an interactive terminal` | Run it in Terminal yourself. The extra confirmation can't be skipped. |
| `TrialRejectedError` | Meta refused `trial_params` (possibly with resumable upload, or the account isn't eligible). Nothing was posted. See the compatibility notes. |
| `status ERROR` | Meta couldn't process the video (codec, length, bitrate). Install ffmpeg so `--dry-run` can check it first. |
| `UNCERTAIN` / state `UNKNOWN` | `media_publish` may have reached Meta and the reply doesn't prove a rejection. The tool **never** retries it, and the account is paused. Run `--account <profile> --reconcile`. Follow its verdict: `--mark-published` when the container is `PUBLISHED`, `--mark-not-published` only once it's `EXPIRED`/`ERROR`. |
| `Publishing to IG ID … is PAUSED` | Another job on that account is unresolved (listed with its container). Reconcile it first. Nothing was posted. |
| `code -1 / subcode 2207085` | Meta "generic internal error" at `media_publish`. Not in Meta's error table. Treated as `UNKNOWN`. See *Publishing outcomes*. |
| `Another publish.py run is in progress` | Only one run at a time. Wait for it to finish. |

Logs (secrets redacted) are in `logs/publisher.log`. State is in `data/state.sqlite3`. Temporary upload copies (normally empty) are in `data/tmp/`.

## 5. How it works

```
publish.py → cli.py
   discovery.SingleFileSource   exactly one root *.mp4 / *.mov (Stage 2: DatedFolderSource)
   validation.validate_video    stable size, MP4/MOV box structure, SHA-256, optional ffprobe
   remux.Remuxer                temp fast-start copy in data/tmp/ when moov is at the end
   config.load_config           .env (default) or .env.test / .env.personal via --account
   state.StateStore             SQLite job per (account, path, sha256, mode); duplicate scope
                                (account, sha256) across modes; backup-before-migrate; fcntl lock
   publisher.publish_job        orchestration + state transitions
   meta_client.MetaClient       HTTP only: create trial/public container → rupload → poll → publish
```
States: `VALIDATED → CONTAINER_CREATED → UPLOADING → UPLOADED → READY → PUBLISHING → PUBLISHED`,
plus `FAILED` (with a `failure_phase` proving nothing was published; safe to retry with a fresh container)
and `UNKNOWN` (blocks the video and pauses the account until reconciled).
`PUBLISHING` is committed to disk *before* `media_publish` is sent. Tokens are sent only in
`Authorization` headers, never in URLs, and they're redacted from all output, logs and state.

## 6. Stage 2 readiness (not built)

Stage 2 adds a `DatedFolderSource` (`Trial-Reels/YYYY-MM-DD/*.mp4|*.mov`, sorted by name), a slot planner
(07:00–16:00 America/Denver, evenly spaced, with N=1 at 11:30), a launchd job, `scheduled_for` on
`VideoJob`/state (the column already exists), a `MISSED` policy, and token-expiry checks. It
reuses `MetaClient` and `publish_job` unchanged. It only works while the Mac is awake and online.

---

## One-time override: URL Trial Reel while job #3 is UNKNOWN (`--override-unknown-job`)

Approved 11 Nov 2026 for exactly one combination, hard-coded in `src/trial_reels/override.py`:
blocker job **#3** (container `18657792691044336`), profile **personal** (IG ID `17840000000000002`),
`--upload-method url`, and `--video-url https://pub-example.r2.dev/IMG_3813_cloud.mp4`.

```
python publish.py --account personal --upload-method url \
  --video-url https://pub-example.r2.dev/IMG_3813_cloud.mp4 \
  --override-unknown-job 3
```

* Job #3 is never modified. It stays UNKNOWN and keeps blocking everything else.
* The command refuses if any other job on the account is unresolved, if the video is (or may be) live
  through any other job, if the folder's video isn't job #3's, or if Meta says job #3's container is
  anything but FINISHED. PUBLISHED means it went live (a certain duplicate). EXPIRED or ERROR means
  you should use `--mark-not-published` instead.
* It asks you to type the account name, then `I ACCEPT A POSSIBLE DUPLICATE`. Nothing is recorded or
  sent before both answers are correct.
* The override is written to the `overrides` table (schema v6) and then the new job is started. The
  override is single-use: `UNIQUE(blocker_job_id)`, and it is spent even if the attempt fails before
  publishing. The final check, which skips only job #3, runs atomically with the claim in
  `begin_override_publish`.
* It never posts a public Reel and never retries. If media_publish gives an ambiguous reply, the NEW
  job becomes UNKNOWN as well.
* There is no `--dry-run`. The command shows everything before its prompts, and any other answer cancels.

---

## One-time diagnostic: normal PUBLIC Reel while trial jobs are UNKNOWN (`--diagnostic-public-override`)

This tests whether media_publish's `code -1 / 2207085` happens only for Trial Reels. Approved on
11 Nov 2026, and pinned in `src/trial_reels/diagnostic.py`:

* Profile **personal**, IG ID `17840000000000002`.
* The video **IMG_2248.MOV** (sha256 `395f9eb5…`). Any other file is refused.
* **PUBLIC** mode with **resumable** upload: `media_type=REELS`, `upload_type=resumable`, no `trial_params`.
* It may ignore only jobs **#3 and #4** in the account-pause check. They are never modified.

```
python publish.py --account personal --public --diagnostic-public-override
```

* Duplicate protection is unchanged. Any other unresolved job still refuses the run.
* If Meta says a covered job's container is PUBLISHED, the run is refused. Resolve that job first.
* Each covered job's container status is read and recorded as evidence. No job is resolved by this.
* You type the account name, then `PUBLIC REEL, JOBS 3 AND 4 STAY UNKNOWN`.
* The override is recorded in `diagnostic_overrides` / `diagnostic_override_blockers` (schema v7) with
  UNIQUE(kind), so it is single-use, even if the attempt fails before publishing.
* It is separate from the trial override (`overrides` table); neither can stand in for the other.
* It never retries. If media_publish's reply is ambiguous, the new job becomes UNKNOWN.

## One-time re-attempt of job #5's video (`--diagnostic-reattempt-override`)

This tests whether removing the stale, deactivated Facebook Page from @nashhawkins (and the new token) fixed
`2207085`. Approved on 11 Nov 2026, and pinned in `src/trial_reels/diagnostic.py` (`APPROVED_REATTEMPT`):

* Profile **personal**, IG ID `17840000000000002`. The video **IMG_2248.MOV** (sha256 `395f9eb5…`), which is
  the same video as UNKNOWN job #5. The duplicate risk is deliberately accepted.
* **PUBLIC** mode with **resumable** upload, no `trial_params`.
* It may ignore only jobs **#3, #4 and #5** in the account-pause check. In the duplicate check it may ignore
  only **job #5**, and only while #5 is still UNKNOWN. Any other copy of this video on the account is still
  refused. Jobs #3/#4/#5 are never modified.

```
python publish.py --account personal --public --diagnostic-reattempt-override
```

* The new job is **attempt 2** (`jobs.attempt`, schema v8), so it has its own key and row; job #5 is attempt 1.
  Every existing row is attempt 1 and keeps its key.
* You type the account name, then `PUBLIC REEL, JOBS 3 4 AND 5 STAY UNKNOWN`, then
  `I ACCEPT A POSSIBLE DUPLICATE OF JOB 5`.
* It is recorded in `diagnostic_overrides` with `allowed_duplicate_job_key` set to job #5's key. Its kind is
  unique, so it is single-use. It is separate from the earlier diagnostic, which stays spent.
* It is refused if Meta says any covered container is PUBLISHED, if job #5 is no longer UNKNOWN, or if any
  other unresolved job exists.
* It never retries. If media_publish's reply is ambiguous, the new job becomes UNKNOWN.

## One-time Trial Reel of IMG_2248 (`--diagnostic-trial-reattempt-override`)

This checks that Trial Reels work after the Business configuration fix. It's approved in `diagnostic.py`
(`APPROVED_TRIAL_REATTEMPT`) for profile **personal**, IG `17840000000000002`, and IMG_2248.MOV only.

* Mode is **TRIAL** with `trial_params={"graduation_strategy":"MANUAL"}` and resumable upload. Any other
  graduation strategy is refused, and it never falls back to a normal Reel.
* The account pause skips jobs #3, #4 and #5. The duplicate check skips #5 only while it's UNKNOWN, and #7
  only while it's PUBLISHED as media `18084194930405612` (you deleted that Reel). #7 is recorded on the
  override as `ACCEPTED_PUBLISHED_DUPLICATE`. Any other copy is still refused. No job is changed.
* You type the account name, then `TRIAL REEL, JOBS 3 4 AND 5 STAY UNKNOWN`, then
  `I ACCEPT A POSSIBLE DUPLICATE OF JOBS 5 AND 7`. It's single-use, and nothing is retried.

```
python publish.py --account personal --diagnostic-trial-reattempt-override
```

## Trial Reels require `--upload-method url` (since 11 Nov 2026)

Job #8 sent `trial_params={"graduation_strategy":"MANUAL"}` with `upload_type=resumable`. Meta accepted it and
returned a media ID, but the Reel went live as a NORMAL Reel on the profile grid. Meta's docs only cover
`trial_params` for `video_url` uploads, and the resumable-upload page says "Other parameters are not
supported". So the tool now refuses TRIAL + resumable at every layer: the CLI, `build_job`, `publish_job`
and `reel_container_params`. A Trial Reel always needs `--upload-method url --video-url <hosted copy>`.
`--public` is unaffected.

**Requested vs confirmed mode.** A media ID from media_publish proves only that something was published.
No documented IG Media field reports trial status. After every publish the tool reads `media_product_type`
and `is_shared_to_feed`, prints `Requested mode` and `Confirmed mode`, and records both in the job's events:

* `Confirmed mode: NOT A TRIAL` plus a warning if `is_shared_to_feed=true`, which is what normal Reels show.
* `Confirmed mode: NOT VERIFIED` otherwise. `false` is consistent with a trial but isn't proof. Check the
  Trial Reels section in the app.

`--status` marks every published TRIAL job "trial status NOT verified by API". The job keeps
`publish_mode=TRIAL` as the requested mode.

## Manual test mode (`--test-mode`)

For hands-on API experiments only, never for scheduled posting. Each run is ONE new attempt that ignores
earlier account locks and duplicate checks. History is never changed.

```
python publish.py --account personal --test-mode --upload-method url --video-url <https MP4 URL>   # Trial Reel, MANUAL
python publish.py --account personal --test-mode --upload-method url --video-url <URL> --public    # normal Reel
python publish.py --account personal --test-mode --public                                          # resumable, local file
```

* **URL mode:** the URL is the video. It's downloaded anonymously and validated (MP4, `moov` first, public
  host, no redirects). Its sha256 identifies the attempt, and no local file is needed.
* **Trial Reels** need URL mode, always send `trial_params={"graduation_strategy":"MANUAL"}`, and never fall back
  to public. Afterwards the tool prints `Requested mode` separately from `Confirmed mode`, which is never
  claimed as verified.
* **Records:** every test gets its own job row (the next `attempt` number), container ID, events (including the
  exact request body), API-call log and outcome. It also gets a `diagnostic_overrides` row of kind
  `TEST_MODE:<job_key>` listing every job it ignored, which are left unchanged.
* **Confirmation:** an interactive terminal is required every time. Type the account name, then
  `TEST TRIAL REEL, DUPLICATES POSSIBLE` (or `TEST PUBLIC REEL, ...`). `--yes`, `--dry-run` and non-TTY runs are refused.
* **No retry:** media_publish is sent once. An unclear reply leaves the new job UNKNOWN, which still pauses
  NORMAL publishing to the account; later `--test-mode` runs aren't blocked by it.
* **Normal runs:** without `--test-mode`, every lock and duplicate check applies as before.

## Daily batch from LipSync (`--batch`)

LipSync uploads each take to R2 at `lipsync/<YYYY-MM-DD>/<id>.mp4`, dated by the Denver calendar. The batch posts
one day's folder as Trial Reels via `video_url`, oldest upload first, with `DEFAULT_CAPTION`.

`.env.personal` needs these (the list token is the Worker's `LIST_TOKEN`; see `~/LipSync/worker/README.md`):

```dotenv
LIPSYNC_WORKER_URL=https://lipsync-upload.<subdomain>.workers.dev
LIPSYNC_LIST_TOKEN=...
DEFAULT_CAPTION="..."
```

Test ladder. Every step runs the same code as the scheduled run:

```bash
python publish.py --account personal --batch yesterday --dry-run   # list + check every video; posts nothing
python publish.py --account personal --batch yesterday --next      # post ONE video (type the account name)
python publish.py --account personal --batch yesterday --all       # post the rest of the day (asks once)
python publish.py --account personal --batch yesterday --all --unattended   # what 08:00 will run; no prompt
```

`DAY` is `yesterday`, `today` or `YYYY-MM-DD`. Safety:
- Any unresolved job pauses the account, and the batch then posts nothing.
- A video already posted to the account (same bytes, any day or method) is skipped as POSTED or DUPLICATE.
- A video whose hosted copy fails the URL checks is skipped as INVALID.
- The batch stops at the first video that doesn't publish cleanly.
- Rerunning is always safe.

### Scheduled 08:00 run (launchd)

`scripts/install-daily-batch.sh` installs the launchd job `local.trial-reels.daily-batch`. Every day at 08:00 Mac
time (Denver), it runs `scripts/daily-batch.sh`, which runs `--batch yesterday --all --unattended`.

- Output is appended to `logs/daily-batch.log`, and a Mac notification gives a one-line summary.
- Check what it would do, without posting: `scripts/daily-batch.sh --dry-run`
- Uninstall with `scripts/install-daily-batch.sh --uninstall`. Rerun the installer after moving the project.

Limits:
- If the Mac is asleep at 08:00, the job runs when it wakes. If the Mac is shut down at 08:00, that day is skipped.
  If it first runs after midnight, "yesterday" is already the next day, so the missed day is never posted
  automatically. Post it by hand with `--batch YYYY-MM-DD --all`.
- The project must stay outside ~/Documents, ~/Desktop and ~/Downloads. macOS blocks launchd jobs from reading
  those folders, and the run then fails with "Operation not permitted".
