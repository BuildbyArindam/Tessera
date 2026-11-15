import { expect, test } from "@playwright/test";
import { KEY, apiGet, readAuditPayload, rewriteAuditPayload } from "./helpers";

interface AuditEvent { seq: number; event_type: string; hash: string; prev_hash: string; payload: unknown }
interface AuditPage { events: AuditEvent[]; next_after_seq: number | null }

test.describe.configure({ mode: "serial" });

const EVENT = (seq: number) => ({ seq, timestamp: "2026-11-15T09:00:00Z", event_type: "policy_decision", payload: { n: seq }, prev_hash: "aa", hash: `h${seq}` });

test("events are paged 20 at a time, forwards and back (API pages stubbed so the page boundaries are known)", async ({ page }) => {
  const full = Array.from({ length: 20 }, (_, i) => EVENT(i + 1));
  const rest = [EVENT(21), EVENT(22)];
  await page.route("**/api/tessera/audit?*", (route) => {
    const after = Number(new URL(route.request().url()).searchParams.get("after_seq") ?? 0);
    return route.fulfill({ json: after === 0 ? { events: full, next_after_seq: 20 } : { events: rest, next_after_seq: null } });
  });
  await page.goto("/audit");
  await expect(page.locator("tbody tr")).toHaveCount(20);
  await expect(page.getByRole("button", { name: "Previous" })).toBeDisabled();
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.locator("tbody tr")).toHaveCount(2);
  await expect(page.getByRole("button", { name: "View event 21" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Next" })).toBeDisabled();
  await page.getByRole("button", { name: "Previous" }).click();
  await expect(page.getByRole("button", { name: "View event 1", exact: true })).toBeVisible();
});

test("a last page that is exactly full leads to an empty page that says so, and Previous still works", async ({ page }) => {
  const full = Array.from({ length: 20 }, (_, i) => EVENT(i + 1));
  await page.route("**/api/tessera/audit?*", (route) => {
    const after = Number(new URL(route.request().url()).searchParams.get("after_seq") ?? 0);
    return route.fulfill({ json: after === 0 ? { events: full, next_after_seq: 20 } : { events: [], next_after_seq: null } });
  });
  await page.goto("/audit");
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByTestId("no-more-events")).toBeVisible();
  await page.getByRole("button", { name: "Previous" }).click();
  await expect(page.locator("tbody tr")).toHaveCount(20);
});

test("the real API's cursor resumes after the last event of the previous page", async () => {
  const first = await apiGet<AuditPage>("/audit?limit=5");
  const second = await apiGet<AuditPage>(`/audit?limit=5&after_seq=${first.next_after_seq}`);
  expect(second.events[0]!.seq).toBeGreaterThan(first.events[first.events.length - 1]!.seq);
});

test("an event's detail shows its real hashes and payload", async ({ page }) => {
  const first = await apiGet<AuditPage>("/audit?limit=20");
  const e = first.events[1]!;
  await page.goto("/audit");
  await page.getByRole("button", { name: `View event ${e.seq}`, exact: true }).click();
  const detail = page.getByTestId("audit-detail");
  await expect(detail).toContainText(e.hash);
  await expect(detail).toContainText(e.prev_hash);
  expect(JSON.parse((await detail.getByLabel("Event payload").textContent())!)).toEqual(e.payload);
});

test("Verify chain says OK on an untouched chain", async ({ page }) => {
  await page.goto("/audit");
  await page.getByRole("button", { name: "Verify chain" }).click();
  await expect(page.getByTestId("chain-ok")).toContainText("Chain OK");
  await expect(page.getByTestId("chain-broken")).toHaveCount(0);
});

test("tampering with one stored row is caught, and the screen names the broken event", async ({ page }) => {
  const target = 3;
  const original = readAuditPayload(target);
  try {
    const tampered = JSON.stringify({ ...(JSON.parse(original) as object), tampered: true });
    rewriteAuditPayload(target, tampered);
    await page.goto("/audit");
    await page.getByRole("button", { name: "Verify chain" }).click();
    const broken = page.getByTestId("chain-broken");
    await expect(broken).toContainText(`Chain broken at event #${target}`);
    await expect(page.getByTestId("chain-ok")).toHaveCount(0);
    // Agrees with the API's own answer, not just with what we expected.
    const api = (await (await fetch("http://127.0.0.1:8016/audit/verify", { headers: { "X-API-Key": KEY } })).json()) as { ok: boolean; broken_seq: number };
    expect(api).toMatchObject({ ok: false, broken_seq: target });
    await broken.getByRole("button", { name: `Show event #${target}` }).click();
    await expect(page.locator("tr.broken")).toContainText("chain broken here");
    await expect(page.locator("tr.broken").getByRole("button", { name: `View event ${target}`, exact: true })).toBeVisible();
  } finally {
    rewriteAuditPayload(target, original);
  }
  await page.goto("/audit");
  await page.getByRole("button", { name: "Verify chain" }).click();
  await expect(page.getByTestId("chain-ok")).toBeVisible();
});
