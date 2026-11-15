import { execFileSync } from "node:child_process";
import type { Locator, Page } from "@playwright/test";

export const API = "http://127.0.0.1:8016";
export const KEY = "e2e-demo-key-0123456789";
const HEADERS = { "X-API-Key": KEY, "Content-Type": "application/json" };

export interface ApiProposal {
  id: string;
  action_type: string;
  params: Record<string, unknown>;
  rationale: string;
  confidence: number;
  status: string;
  approval: { decision: string; actor: string; decided_at?: string; note?: string | null } | null;
  decision: {
    verdict: string;
    reasons: string[];
    rule_ids: string[];
    ruleset_hash: string;
    simulation: { expected_saving: { amount: string }; worst_case_loss: { amount: string }; n_paths: number } | null;
  } | null;
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`, { headers: HEADERS });
  if (!res.ok) throw new Error(`GET ${path} -> ${res.status}`);
  return (await res.json()) as T;
}

export const listProposals = () => apiGet<ApiProposal[]>("/proposals?business_id=B0&limit=200");

/** Ask the (scripted) planner for its proposals. Proposal ids are content hashes, so running it twice adds nothing. */
export async function planRun(): Promise<void> {
  const res = await fetch(`${API}/plan/run`, { method: "POST", headers: HEADERS, body: JSON.stringify({ business_id: "B0" }) });
  if (!res.ok) throw new Error(`plan/run -> ${res.status}`);
}

export const card = (page: Page, id: string): Locator => page.locator(`[data-proposal-id="${id}"]`);

const NUMBER = /\d[\d,]*(?:\.\d+)?/g;
export const numbersIn = (text: string): number[] =>
  (text.match(NUMBER) ?? []).map((t) => Math.abs(Number.parseFloat(t.replace(/,/g, ""))));

/** Numbers in a rendered explanation that the API payload for that proposal does not contain (see day16.test.ts). */
export function unexplainedNumbers(text: string, payload: ApiProposal): number[] {
  const known = new Set(numbersIn(JSON.stringify(payload)));
  known.add(Math.round(payload.confidence * 100));
  return numbersIn(text).filter((n) => !known.has(n));
}

const DB_URL = "postgresql://tessera:tessera@127.0.0.1:5433/tessera_e2e";
const PY = process.env.PYTHON ?? "../../.venv/bin/python";

/** Rewrite one audit row's payload the only way possible: a superuser switching the append-only trigger off first. */
export function rewriteAuditPayload(seq: number, payloadJson: string): void {
  const code = [
    "import sys, psycopg",
    "with psycopg.connect(sys.argv[1]) as c:",
    "    c.execute('ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete')",
    "    c.execute('UPDATE audit_events SET payload = %s WHERE seq = %s', (sys.argv[3], int(sys.argv[2])))",
    "    c.execute('ALTER TABLE audit_events ENABLE TRIGGER audit_events_no_update_delete')",
  ].join("\n");
  execFileSync(PY, ["-c", code, DB_URL, String(seq), payloadJson]);
}

export function readAuditPayload(seq: number): string {
  const code = "import sys, psycopg\nwith psycopg.connect(sys.argv[1]) as c:\n    print(c.execute('SELECT payload FROM audit_events WHERE seq = %s', (int(sys.argv[2]),)).fetchone()[0])";
  return execFileSync(PY, ["-c", code, DB_URL, String(seq)]).toString().trim();
}
