import { expect, test, type Page } from "@playwright/test";

/**
 * Day 19: the same flow as `make demo`, driven through the dashboard.
 *
 * Seed -> ingest a USD invoice -> worker -> an ESCALATED conversion -> approve it in the UI -> the worker re-checks and
 * executes on the mock -> the supplier payout is planned and SUBMITTED -> a signed webhook settles it -> verify the audit
 * chain in the UI. The dashboard has no invoice form and a browser cannot be the customer or the vendor, so the invoice
 * is posted to the API and the customer's payment and the vendor's webhook come from the demo-only control app.
 */
const API = "http://127.0.0.1:8019";
const CONTROL = "http://127.0.0.1:8039";
const HEADERS = { "X-API-Key": "e2e-demo-key-0123456789", "Content-Type": "application/json" };

interface ApiProposal {
  id: string;
  action_type: string;
  status: string;
  params: Record<string, string>;
  decision: { verdict: string; rule_ids: string[] } | null;
  approval: { decision: string; actor: string } | null;
}
interface AuditPage { events: { event_type: string }[] }

const get = async <T>(path: string): Promise<T> => {
  const res = await fetch(`${API}${path}`, { headers: HEADERS });
  if (!res.ok) throw new Error(`GET ${path} -> ${res.status}`);
  return (await res.json()) as T;
};
const control = async <T>(path: string, body?: unknown): Promise<T> => {
  const res = await fetch(`${CONTROL}${path}`, { method: body === undefined ? "GET" : "POST", headers: HEADERS, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!res.ok) throw new Error(`control ${path} -> ${res.status} ${await res.text()}`);
  return (await res.json()) as T;
};
const proposals = () => get<ApiProposal[]>("/proposals?business_id=B0&limit=200");
const wallet = async () => (await control<{ balances: Record<string, string> }>("/state")).balances;
const card = (page: Page, id: string) => page.locator(`[data-proposal-id="${id}"]`);
const find = async (kind: string) => (await proposals()).find((p) => p.action_type === kind);

test("seed, invoice, escalation, approval, execution, webhook and audit chain, through the UI", async ({ page }) => {
  let convertId = "";
  let payoutId = "";

  await test.step("the seeded business has nothing proposed", async () => {
    await page.goto("/proposals");
    await expect(page.locator("[data-proposal-id]")).toHaveCount(0);
    expect(await wallet()).toEqual({ INR: "60,000.00" });
  });

  await test.step("a new USD invoice is ingested through the API", async () => {
    const res = await fetch(`${API}/invoices`, {
      method: "POST",
      headers: HEADERS,
      body: JSON.stringify({ id: "INV-E2E-001", business_id: "B0", customer_id: "CUST-ACME", amount: { amount: "6000.00", currency: "USD" }, issued_on: "2026-03-02", due_on: "2026-03-04", notes: "Net 2 days" }),
    });
    expect(res.status).toBe(201);
  });

  await test.step("the worker runs several cycles and, with no USD to sell, proposes nothing", async () => {
    await page.waitForTimeout(6_000); // three worker cycles
    expect(await proposals()).toHaveLength(0);
  });

  await test.step("the customer pays; the worker proposes a conversion and policy ESCALATES it", async () => {
    await control("/customer-pays", { invoice_id: "INV-E2E-001" });
    await expect.poll(async () => (await find("CONVERT"))?.status, { timeout: 30_000 }).toBe("PENDING_APPROVAL");
    const p = (await find("CONVERT"))!;
    convertId = p.id;
    expect(p.decision?.verdict).toBe("ESCALATE");
    expect(p.decision?.rule_ids).toEqual(["forecast_confidence_gate"]);
    expect(await wallet()).toEqual({ INR: "60,000.00", USD: "6,000.00" }); // escalated, so nothing moved

    await page.goto("/proposals");
    const c = card(page, convertId);
    await expect(c.getByTestId("status")).toHaveText("Needs your approval");
    await expect(c.getByTestId("verdict")).toContainText(/escalate/i);
    await expect(c.getByTestId("rule-ids")).toContainText("forecast_confidence_gate");
    await expect(c.getByTestId("explanation")).toContainText(/1,?750/);
    await expect(c.getByTestId("expected-saving")).toBeVisible();
    await expect(c.getByTestId("worst-case-loss")).toBeVisible();
    await expect(c.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  await test.step("a person approves it in the UI; the screen never claims 'executed' before the API does", async () => {
    const c = card(page, convertId);
    await c.getByRole("button", { name: "Approve" }).click();
    await expect(page.getByTestId("notice")).toContainText("Approved");
    await page.getByRole("tab", { name: /Decided/ }).click();
    const decided = card(page, convertId);
    await expect(decided.getByTestId("approval-record")).toContainText("Approved by");
    // It then follows the worker: re-check policy, execute on the mock, settle.
    await expect(decided.getByTestId("status")).toHaveText("Executed · settled", { timeout: 40_000 });
    expect((await proposals()).find((x) => x.id === convertId)?.status).toBe("SETTLED");
    // USD 1,750 was sold at 80.00. The INR is not asserted here: the worker may already have paid the supplier with it.
    expect((await wallet()).USD).toBe("4,250.00");
  });

  await test.step("the worker plans the supplier payout; policy approves it and it is SUBMITTED, not settled", async () => {
    await expect.poll(async () => (await find("PAY_SUPPLIER"))?.status, { timeout: 40_000 }).toBe("SUBMITTED");
    const p = (await find("PAY_SUPPLIER"))!;
    payoutId = p.id;
    expect(p.decision?.verdict).toBe("APPROVE");
    expect(p.approval).toBeNull(); // no person was needed: inside every limit
    await page.goto("/proposals");
    await page.getByRole("tab", { name: /Decided/ }).click();
    const c = card(page, payoutId);
    await expect(c.getByTestId("status")).toHaveText("Submitted · not settled yet");
    await expect(c).not.toContainText(/executed · settled/i);
    expect(await wallet()).toEqual({ USD: "4,250.00" }); // the INR has left the wallet
  });

  await test.step("the vendor's signed webhook settles it, exactly once", async () => {
    const first = await control<{ http_status: number; status: string }>("/webhook-paid", { proposal_id: payoutId });
    expect(first).toEqual({ http_status: 200, status: "applied" });
    await expect(card(page, payoutId).getByTestId("status")).toHaveText("Executed · settled", { timeout: 30_000 });
    expect((await proposals()).find((x) => x.id === payoutId)?.status).toBe("SETTLED");
  });

  await test.step("the audit chain verifies in the UI and holds the whole story", async () => {
    await page.goto("/audit");
    await page.getByRole("button", { name: "Verify chain" }).click();
    await expect(page.getByTestId("chain-ok")).toContainText("Chain OK");
    await expect(page.getByTestId("chain-broken")).toHaveCount(0);
    const kinds = new Set((await get<AuditPage>("/audit?limit=500")).events.map((e) => e.event_type));
    for (const k of ["proposal_created", "policy_decision", "approved", "execution_submitted", "execution_result", "webhook_received"]) {
      expect(kinds, k).toContain(k);
    }
  });
});
