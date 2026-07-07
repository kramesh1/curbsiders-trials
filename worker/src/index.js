/**
 * Cloudflare Worker: visitor feedback intake for curbsiders_to_trials.
 *
 * POST /feedback  - public, called from docs/app.js. Structured flag (reason
 *                    code + optional comment) on a pearl or a pearl->trial
 *                    evidence link. No PII is collected; the caller's IP is
 *                    hashed (never stored raw) purely to rate-limit abuse.
 * GET  /feedback   - admin-only (Bearer ADMIN_TOKEN), paged by ?since_id=N.
 *                    Used by scripts/import_feedback.py to pull rows into the
 *                    owner-gated review sidecar. Nothing here is ever shown on
 *                    the site directly -- only aggregated, human-approved
 *                    feedback reaches docs/data/pearls.json.
 */

const TARGET_TYPES = new Set(["pearl", "pearl_link"]);
const REASON_CODES = new Set(["inaccurate", "outdated", "wrong_citation", "unclear", "other"]);
const MAX_COMMENT_LENGTH = 500;
const MAX_TEXT_FIELD_LENGTH = 2000;
const MAX_KEY_LENGTH = 300;
const RATE_LIMIT_PER_HOUR = 20;
const ADMIN_PAGE_SIZE = 500;

function corsHeaders(origin, allowedOrigin) {
  const headers = {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    Vary: "Origin",
  };
  if (allowedOrigin && origin === allowedOrigin) {
    headers["Access-Control-Allow-Origin"] = allowedOrigin;
  }
  return headers;
}

function jsonResponse(body, status, extraHeaders) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...extraHeaders },
  });
}

function truncate(value, maxLength) {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed ? trimmed.slice(0, maxLength) : null;
}

async function hashIp(ip, salt) {
  const data = new TextEncoder().encode(`${salt}:${ip}`);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function handleSubmit(request, env, cors) {
  let body;
  try {
    body = await request.json();
  } catch (err) {
    return jsonResponse({ error: "invalid_json" }, 400, cors);
  }

  // Honeypot: a hidden field real visitors never fill in. Report success
  // without writing anything, so a bot doesn't learn its submission failed.
  if (body.website) {
    return jsonResponse({ ok: true }, 201, cors);
  }

  const targetType = body.target_type;
  const pearlKey = truncate(body.pearl_key, MAX_KEY_LENGTH);
  const reasonCode = body.reason_code;
  if (!TARGET_TYPES.has(targetType) || !pearlKey || !REASON_CODES.has(reasonCode)) {
    return jsonResponse({ error: "invalid_fields" }, 400, cors);
  }

  const canonicalKey = targetType === "pearl_link" ? truncate(body.canonical_key, MAX_KEY_LENGTH) : null;
  if (targetType === "pearl_link" && !canonicalKey) {
    return jsonResponse({ error: "missing_canonical_key" }, 400, cors);
  }

  const comment = truncate(body.comment, MAX_COMMENT_LENGTH);
  const pearlTextSnapshot = truncate(body.pearl_text_snapshot, MAX_TEXT_FIELD_LENGTH);
  const episodeUrl = truncate(body.episode_url, 500);

  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const ipHash = await hashIp(ip, env.IP_HASH_SALT || "curbsiders-feedback");

  const rateRow = await env.DB.prepare(
    "SELECT COUNT(*) AS n FROM feedback WHERE client_ip_hash = ? AND submitted_at > datetime('now', '-1 hour')"
  )
    .bind(ipHash)
    .first();
  if ((rateRow?.n || 0) >= RATE_LIMIT_PER_HOUR) {
    return jsonResponse({ error: "rate_limited" }, 429, cors);
  }

  const submittedAt = new Date().toISOString();
  await env.DB.prepare(
    `INSERT INTO feedback
       (submitted_at, target_type, pearl_key, pearl_text_snapshot, canonical_key, reason_code, comment, episode_url, client_ip_hash)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
  )
    .bind(submittedAt, targetType, pearlKey, pearlTextSnapshot, canonicalKey, reasonCode, comment, episodeUrl, ipHash)
    .run();

  return jsonResponse({ ok: true }, 201, cors);
}

async function handleAdminRead(request, env, cors, url) {
  const expected = env.ADMIN_TOKEN ? `Bearer ${env.ADMIN_TOKEN}` : null;
  const authHeader = request.headers.get("Authorization") || "";
  if (!expected || authHeader !== expected) {
    return jsonResponse({ error: "unauthorized" }, 401, cors);
  }

  const sinceId = Number.parseInt(url.searchParams.get("since_id") || "0", 10) || 0;
  const { results } = await env.DB.prepare(
    `SELECT id, submitted_at, target_type, pearl_key, pearl_text_snapshot, canonical_key, reason_code, comment, episode_url
     FROM feedback WHERE id > ? ORDER BY id ASC LIMIT ${ADMIN_PAGE_SIZE}`
  )
    .bind(sinceId)
    .all();

  return jsonResponse({ rows: results }, 200, cors);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin") || "";
    const cors = corsHeaders(origin, env.ALLOWED_ORIGIN || "");

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }
    if (url.pathname === "/feedback" && request.method === "POST") {
      return handleSubmit(request, env, cors);
    }
    if (url.pathname === "/feedback" && request.method === "GET") {
      return handleAdminRead(request, env, cors, url);
    }
    return jsonResponse({ error: "not_found" }, 404, cors);
  },
};
