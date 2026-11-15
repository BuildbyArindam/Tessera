import type { NextRequest } from "next/server";
import { checkPost, sameOrigin } from "@/lib/proxyPolicy";

/**
 * Proxy to the Tessera API. The API key stays on the server; the browser never holds it.
 * GET is open for reads. POST is limited to the few writes in `checkPost` (decide a proposal, answer a promotion),
 * and only from this dashboard's own origin.
 */
export const dynamic = "force-dynamic";

const BASE = (process.env.TESSERA_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");
const SAFE_SEGMENT = /^[A-Za-z0-9._~-]+$/;
const MAX_BODY_BYTES = 4096;
const json = (body: unknown, status: number) => Response.json(body, { status, headers: { "Cache-Control": "no-store" } });

type Params = { params: Promise<{ path: string[] }> };

async function checkedPath(params: Params["params"]): Promise<string[] | Response> {
  const { path } = await params;
  const clean = path.every((s) => SAFE_SEGMENT.test(s) && s !== "." && s !== "..");
  return clean ? path : json({ detail: "bad path" }, 400);
}

async function forward(url: string, key: string, init: { method: "GET" | "POST"; body?: string }): Promise<Response> {
  try {
    const headers: Record<string, string> = { "X-API-Key": key, Accept: "application/json" };
    if (init.body !== undefined) headers["Content-Type"] = "application/json";
    const upstream = await fetch(url, { method: init.method, headers, body: init.body, cache: "no-store" });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("Content-Type") ?? "application/json", "Cache-Control": "no-store" },
    });
  } catch {
    return json({ detail: "database unavailable" }, 503);
  }
}

export async function GET(request: NextRequest, { params }: Params) {
  const key = process.env.TESSERA_API_KEY;
  if (!key) return json({ detail: "TESSERA_API_KEY is not set on the web server" }, 500);
  const path = await checkedPath(params);
  if (path instanceof Response) return path;
  return forward(`${BASE}/${path.join("/")}${request.nextUrl.search}`, key, { method: "GET" });
}

export async function POST(request: NextRequest, { params }: Params) {
  const key = process.env.TESSERA_API_KEY;
  if (!key) return json({ detail: "TESSERA_API_KEY is not set on the web server" }, 500);
  if (!sameOrigin(request.headers.get("origin"), request.headers.get("host"))) {
    return json({ detail: "cross-origin writes are not allowed" }, 403);
  }
  if (!(request.headers.get("content-type") ?? "").toLowerCase().startsWith("application/json")) {
    return json({ detail: "expected application/json" }, 415);
  }
  const path = await checkedPath(params);
  if (path instanceof Response) return path;

  const raw = await request.text();
  if (new TextEncoder().encode(raw).length > MAX_BODY_BYTES) return json({ detail: "body too large" }, 413);
  let parsed: unknown;
  try {
    parsed = raw === "" ? {} : JSON.parse(raw);
  } catch {
    return json({ detail: "body is not valid JSON" }, 400);
  }
  const checked = checkPost(path, parsed);
  if (!checked.ok) return json({ detail: checked.reason }, 405);
  // Forward what was validated, re-serialised, never the raw bytes.
  return forward(`${BASE}/${path.join("/")}`, key, { method: "POST", body: JSON.stringify(checked.body) });
}
