# lipsync-upload Worker

Gives the LipSync app short-lived links to upload videos straight into R2.
The R2 key lives only here, as a Cloudflare secret. The app knows only
`UPLOAD_TOKEN`, which can be changed at any time without rebuilding the app.

```
app ──POST /upload-url {"bytes": N}, Bearer UPLOAD_TOKEN──▶ Worker
app ◀──{uploadURL (15 min), key, publicURL}──
app ──PUT video (Content-Type: video/mp4)──▶ R2
app ──HEAD publicURL: 200, video/mp4, N bytes?──▶ r2.dev
```

Files land at `lipsync/YYYY-MM-DD/<uuid>.mp4`, dated by the Denver calendar
(`TIMEZONE` in `src/index.js`; it must match the publisher). Each `publicURL` works as the
Trial Reels publisher's `--video-url`.

## One-time setup

Run these in Terminal from this folder (`cd ~/LipSync/worker`). Wrangler is a
project dependency, so `npm install` is the only install step.

1. **Install and log in**
   ```
   npm install
   npx wrangler login
   ```
2. **Fill in `wrangler.toml`.** Run `npx wrangler whoami` to get the account ID
   and `npx wrangler r2 bucket list` to get the bucket name. Then
   `npx wrangler r2 bucket dev-url get <bucket>` must print the same address
   as `PUBLIC_BASE_URL`.
3. **Deploy.** Until the secrets are set, it answers every request with
   "missing settings".
   ```
   npx wrangler deploy
   ```
   Note the `https://lipsync-upload.<subdomain>.workers.dev` address it prints.
4. **Create an R2 API token** in the Cloudflare dashboard. Go to R2, then
   Manage API tokens, then Create API token. Set the permission to *Object
   Read & Write* and apply it to *that one bucket only*. Then store the two
   values it shows:
   ```
   npx wrangler secret put R2_ACCESS_KEY_ID
   npx wrangler secret put R2_SECRET_ACCESS_KEY
   ```
5. **Make the app's password and build it into the app.** This writes it to
   the Worker and to `LipSync/UploadConfig.json` (git-ignored; Xcode copies
   it into the app) without printing it. Run it from `~/LipSync`:
   ```
   T=$(openssl rand -hex 32)
   printf %s "$T" | (cd worker && npx wrangler secret put UPLOAD_TOKEN)
   T="$T" python3 -c 'import json,os; json.dump({"workerURL": "https://lipsync-upload.<subdomain>.workers.dev", "token": os.environ["T"]}, open("LipSync/UploadConfig.json", "w"), indent=2)'
   unset T
   ```
6. **Rebuild the app.** It uploads with no setup on the phone. The gear
   screen can still override the address and token, which are kept in the
   Keychain, and can switch back to the built-in values.

To change the password later, repeat steps 5 and 6. Builds without
`UploadConfig.json` (e.g. a fresh clone) still work; you fill in the gear
screen instead.

## Local testing

`.dev.vars` (git-ignored) holds fake secrets. With them, links can be
generated locally but uploads to them fail:

```
npx wrangler dev --var R2_ACCOUNT_ID:test --var R2_BUCKET:test
curl -X POST localhost:8787/upload-url -H 'Authorization: Bearer test-token' -d '{"bytes":1000}'
```
