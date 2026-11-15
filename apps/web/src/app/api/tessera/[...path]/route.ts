import type { NextRequest } from "next/server";

/**
 * Read-only proxy to the Tessera API. The API key stays on the server; the browser never holds it.
 * Only GET is exposed: approvals and other writes arrive with the Day 16 screens, behind their own checks.
 */
export const dynamic = "force-dynamic";

const BASE = (process.env.TESSERA_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");
const SAFE_SEGMENT = /^[A-Za-z0-9._~-]+$/;

export async function GET(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  const key = process.env.TESSERA_API_KEY;
  if (!key) return Response.json({ detail: "TESSERA_API_KEY is not set on the web server" }, { status: 500 });
  const { path } = await params;
  if (!path.every((s) => SAFE_SEGMENT.test(s) && s !== "." && s !== "..")) {
    return Response.json({ detail: "bad path" }, { status: 400 });
  }
  const url = `${BASE}/${path.join("/")}${request.nextUrl.search}`;
  try {
    const upstream = await fetch(url, { headers: { "X-API-Key": key, Accept: "application/json" }, cache: "no-store" });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("Content-Type") ?? "application/json", "Cache-Control": "no-store" },
    });
  } catch {
    return Response.json({ detail: "database unavailable" }, { status: 503 });
  }
}
