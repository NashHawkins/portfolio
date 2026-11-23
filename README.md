# Portfolio: LipSync and Trial Reel Publisher

Two connected side projects. The iPhone app records lip-sync clips and uploads them to Cloudflare R2.
The publisher picks clips up from R2 and posts them to Instagram as Trial Reels.

| Folder | What it is | Stack |
|---|---|---|
| [`lipsync-app/`](lipsync-app/) | iOS app: plays a song, records silent video in sync, merges the original audio, uploads to R2 | Swift, SwiftUI, AVFoundation; Cloudflare Worker (JS) |
| [`trial-reels-publisher/`](trial-reels-publisher/) | CLI that publishes videos as Instagram Trial Reels through the official Graph API | Python 3.9+, pytest (577 tests) |

## LipSync (iOS)

* The capture session has **no audio input**. The song plays through the speaker while video records
  silently, then the original audio file is merged in afterwards, so mic bleed never reaches the output.
* Playback starts in the `didStartRecordingTo` delegate callback, not on the button tap, to keep the
  audio/video offset small.
* Output is cropped to the preview's exact aspect ratio, 1080p30, with video stabilization. Front-camera takes
  are mirrored to match the preview.
* Uploads use short-lived presigned URLs from a small Cloudflare Worker ([`lipsync-app/worker`](lipsync-app/worker/)).
  The R2 keys stay in the Worker and the app never sees them.

Open `lipsync-app/LipSync.xcodeproj` in Xcode and set your own signing team to build.

## Trial Reel Publisher (Python)

* Uses only Meta's Instagram Graph API (`video_url` + `trial_params`, then `media_publish`).
* Built for safety: duplicate protection by content hash per account, typed confirmations for anything
  public, no silent fallback from a Trial Reel to a public Reel, SSRF checks on hosted video URLs, and
  tokens redacted from logs.
* Run the tests with `pytest` inside `trial-reels-publisher/`. See its [README](trial-reels-publisher/README.md).

---
Account IDs, the Apple team ID and bucket URLs have been replaced with placeholders. No credentials are included.
