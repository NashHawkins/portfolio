// LipSync upload Worker.
//
// The iPhone app never holds the R2 key. It asks this Worker for a link,
// proving itself with UPLOAD_TOKEN, and the Worker answers with a presigned
// PUT link that works for one new file for a few minutes. The app then sends
// the video straight to R2, so the file never passes through the Worker and
// the Worker's request-size limit does not apply.
//
// POST /upload-url
//   Authorization: Bearer <UPLOAD_TOKEN>
//   {"bytes": 18821082}
// -> 200 {"uploadURL", "key", "publicURL", "expiresIn", "contentType"}
//
// GET /list?date=2026-11-12   (or date=today / date=yesterday, by TIMEZONE)
//   Authorization: Bearer <LIST_TOKEN or UPLOAD_TOKEN>
// -> 200 {"date", "videos": [{"key", "size", "uploaded", "publicURL"}, ...]}  oldest first
//   The Trial Reels publisher's daily batch uses LIST_TOKEN; the app uses its
//   UPLOAD_TOKEN to show what is waiting to be posted.
//
// DELETE /video?key=lipsync/2026-11-12/<uuid>.mp4
//   Authorization: Bearer <UPLOAD_TOKEN>
// -> 200 {"deleted": key}   so a take is not posted by the next morning's batch.
//   Only keys of the shape the app uploads can be deleted; LIST_TOKEN can't delete.
//
// The app must send the PUT with exactly `Content-Type: video/mp4`, because
// that header is part of the signature. R2 then serves the file with that
// type, which the Trial Reels publisher (and Meta) require.

import { AwsClient } from "aws4fetch";

const CONTENT_TYPE = "video/mp4";
/** Meta's Reels limit; the publisher refuses anything bigger, so refuse it here first. */
const MAX_BYTES = 300 * 1000 * 1000;
/** How long an upload link stays valid, in seconds. */
const LINK_TTL_SECONDS = 15 * 60;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/list") return listDay(request, env, url);
    if (url.pathname === "/video") return deleteVideo(request, env, url);
    if (url.pathname !== "/upload-url") return json({ error: "Not found." }, 404);
    if (request.method !== "POST") return json({ error: "Use POST." }, 405);

    const configError = missingConfig(env, REQUIRED);
    if (configError) return json({ error: configError }, 500);

    if (!(await isAuthorized(request, env.UPLOAD_TOKEN))) {
      return json({ error: "Wrong or missing upload token." }, 401);
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "The request body must be JSON like {\"bytes\": 12345}." }, 400);
    }
    const bytes = body?.bytes;
    if (!Number.isSafeInteger(bytes) || bytes <= 0) {
      return json({ error: "\"bytes\" must be the file size as a positive whole number." }, 400);
    }
    if (bytes > MAX_BYTES) {
      return json({ error: `The video is ${(bytes / 1e6).toFixed(1)} MB, over the ${MAX_BYTES / 1e6} MB limit.` }, 413);
    }

    const key = newKey(new Date(), crypto.randomUUID());
    const uploadURL = await presignPut(env, key);
    return json({
      uploadURL,
      key,
      publicURL: `${env.PUBLIC_BASE_URL.replace(/\/+$/, "")}/${key}`,
      expiresIn: LINK_TTL_SECONDS,
      contentType: CONTENT_TYPE,
    });
  },
};

/**
 * Videos are filed by the day they were uploaded, as a calendar in this time
 * zone sees it. The Trial Reels publisher posts "yesterday's" folder each
 * morning, so this must match its TIMEZONE.
 */
const TIMEZONE = "America/Denver";

/** lipsync/2026-11-13/<uuid>.mp4, dated in TIMEZONE (daylight saving included). */
function newKey(now, uuid) {
  return `lipsync/${dayIn(now)}/${uuid}.mp4`;
}

async function presignPut(env, key) {
  const r2 = new AwsClient({
    accessKeyId: env.R2_ACCESS_KEY_ID,
    secretAccessKey: env.R2_SECRET_ACCESS_KEY,
    service: "s3",
    region: "auto",
  });
  const target = new URL(`https://${env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com/${env.R2_BUCKET}/${key}`);
  target.searchParams.set("X-Amz-Expires", String(LINK_TTL_SECONDS));
  const signed = await r2.sign(
    new Request(target, { method: "PUT", headers: { "Content-Type": CONTENT_TYPE } }),
    // allHeaders: aws4fetch leaves Content-Type out of query signatures by default.
    { aws: { signQuery: true, allHeaders: true } },
  );
  return signed.url;
}

const REQUIRED = ["R2_ACCOUNT_ID", "R2_BUCKET", "PUBLIC_BASE_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "UPLOAD_TOKEN"];
const REQUIRED_FOR_LIST = ["BUCKET", "PUBLIC_BASE_URL", "LIST_TOKEN"];

async function listDay(request, env, url) {
  if (request.method !== "GET") return json({ error: "Use GET." }, 405);
  const configError = missingConfig(env, REQUIRED_FOR_LIST);
  if (configError) return json({ error: configError }, 500);
  if (!(await isAuthorized(request, env.LIST_TOKEN)) && !(await isAuthorized(request, env.UPLOAD_TOKEN))) {
    return json({ error: "Wrong or missing token." }, 401);
  }
  const date = resolveDate(url.searchParams.get("date") ?? "", new Date());
  if (!date) {
    return json({ error: "Give ?date=YYYY-MM-DD, today or yesterday." }, 400);
  }
  const base = env.PUBLIC_BASE_URL.replace(/\/+$/, "");
  const objects = [];
  let cursor;
  do {
    const page = await env.BUCKET.list({ prefix: `lipsync/${date}/`, cursor });
    objects.push(...page.objects);
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  const videos = objects
    .filter((o) => o.key.endsWith(".mp4"))
    .map((o) => ({ key: o.key, size: o.size, uploaded: o.uploaded.toISOString(), publicURL: `${base}/${o.key}` }))
    .sort((a, b) => a.uploaded.localeCompare(b.uploaded) || a.key.localeCompare(b.key));
  return json({ date, videos });
}

const KEY_PATTERN = /^lipsync\/\d{4}-\d{2}-\d{2}\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.mp4$/;

async function deleteVideo(request, env, url) {
  if (request.method !== "DELETE") return json({ error: "Use DELETE." }, 405);
  const configError = missingConfig(env, ["BUCKET", "UPLOAD_TOKEN"]);
  if (configError) return json({ error: configError }, 500);
  if (!(await isAuthorized(request, env.UPLOAD_TOKEN))) {
    return json({ error: "Wrong or missing upload token." }, 401);
  }
  const key = url.searchParams.get("key") ?? "";
  if (!KEY_PATTERN.test(key)) return json({ error: "That isn't a LipSync video key." }, 400);
  if (!(await env.BUCKET.head(key))) return json({ error: "That video is already gone." }, 404);
  await env.BUCKET.delete(key);
  return json({ deleted: key });
}

/** "today", "yesterday" (by TIMEZONE's calendar) or a literal YYYY-MM-DD; null if none of those. */
function resolveDate(value, now) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const today = dayIn(now);
  if (value === "today") return today;
  if (value === "yesterday") {
    const [y, m, d] = today.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d - 1)).toISOString().slice(0, 10);
  }
  return null;
}

function dayIn(now) {
  // en-CA formats dates as YYYY-MM-DD.
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: TIMEZONE, year: "numeric", month: "2-digit", day: "2-digit",
  }).format(now);
}

function missingConfig(env, required) {
  const missing = required.filter((name) => !env[name] || env[name] === "REPLACE_ME");
  return missing.length ? `The Worker is missing settings: ${missing.join(", ")}.` : null;
}

/** Compares the bearer token in constant time. */
async function isAuthorized(request, expected) {
  const header = request.headers.get("Authorization") ?? "";
  const match = /^Bearer (.+)$/.exec(header);
  if (!match || !expected) return false;
  // Hash both sides so the comparison has equal lengths and leaks nothing about the token's length.
  const [a, b] = await Promise.all([sha256(match[1]), sha256(expected)]);
  return crypto.subtle.timingSafeEqual(a, b);
}

async function sha256(text) {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}
