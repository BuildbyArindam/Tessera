/**
 * Which writes the dashboard's server proxy will forward. Everything not listed here is refused with 405.
 *
 * The browser never holds the API key, so this list is the whole of what a person (or script) at the dashboard can
 * ask the API to change: decide one escalated proposal, or answer one pending autonomy promotion. There is no path
 * to /plan/run, /invoices, or the autonomy `demote` action. The API still validates and re-checks everything; this
 * only keeps the dashboard's reach as small as its screens need.
 */

const PROPOSAL_DECISION = /^proposals\/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\/(approve|reject)$/;
const PROMOTION_ACTIONS = new Set(["confirm_promotion", "reject_promotion"]);
const MAX_NOTE = 500;

export type PostCheck = { ok: true; body: Record<string, unknown> } | { ok: false; reason: string };

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const noteOk = (v: unknown) => v === undefined || v === null || (typeof v === "string" && v.length <= MAX_NOTE);
const hasOnly = (o: Record<string, unknown>, allowed: string[]) => Object.keys(o).every((k) => allowed.includes(k));

export function checkPost(path: string[], body: unknown): PostCheck {
  const joined = path.join("/");
  const value = body ?? {};
  if (!isRecord(value)) return { ok: false, reason: "body must be a JSON object" };

  if (PROPOSAL_DECISION.test(joined)) {
    if (!hasOnly(value, ["note"]) || !noteOk(value.note)) return { ok: false, reason: "invalid decision body" };
    return { ok: true, body: value };
  }

  if (joined === "autonomy") {
    const id = value.promotion_id;
    const valid =
      hasOnly(value, ["action", "promotion_id", "note"]) &&
      typeof value.action === "string" &&
      PROMOTION_ACTIONS.has(value.action) &&
      typeof id === "number" &&
      Number.isSafeInteger(id) &&
      id > 0 &&
      noteOk(value.note);
    return valid ? { ok: true, body: value } : { ok: false, reason: "only confirming or rejecting a promotion is allowed" };
  }

  return { ok: false, reason: "not allowed" };
}

/** A write must come from this dashboard's own pages: the Origin header's host has to be the host we were asked on. */
export function sameOrigin(origin: string | null, host: string | null): boolean {
  if (!origin || !host) return false;
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}
