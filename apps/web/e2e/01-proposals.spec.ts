import { expect, test } from "@playwright/test";
import { KEY, apiGet, card, listProposals, planRun, unexplainedNumbers, type ApiProposal } from "./helpers";

// One database, one story, in order: look at the cards, approve one slowly, reject another, then try to misuse the proxy.
test.describe.configure({ mode: "serial" });

let pending: ApiProposal[] = [];
let blocked!: ApiProposal;
let settled!: ApiProposal;

test.beforeAll(async () => {
  await planRun();
  // The policy engine approved the conversion by itself; the worker (every 10 s) re-checks it and executes it.
  await expect.poll(async () => (await listProposals()).filter((p) => p.status === "APPROVED").length, { timeout: 40_000 }).toBe(0);
  const all = await listProposals();
  pending = all.filter((p) => p.status === "PENDING_APPROVAL").sort((a, b) => a.id.localeCompare(b.id));
  blocked = all.find((p) => p.status === "BLOCKED" && p.decision!.rule_ids.includes("beneficiary_allowlist"))!;
  settled = all.find((p) => p.status === "SETTLED")!;
  expect(pending).toHaveLength(2); // two conversions above the L1 cap
  expect(all.filter((p) => p.status === "BLOCKED")).toHaveLength(2); // two payouts, which L1 does not permit
});

const money = (text: string) => Number(text.replace(/[^0-9.+-]/g, ""));

test("an escalated card shows the figures the decision was made on, and offers approve and reject", async ({ page }) => {
  const p = pending[0]!;
  await page.goto("/proposals");
  await expect(page.getByRole("tab", { name: /Needs approval \(2\)/ })).toHaveAttribute("aria-selected", "true");
  const c = card(page, p.id);
  await expect(c).toBeVisible();
  await expect(c.getByTestId("status")).toHaveText("Needs your approval");
  await expect(c.getByTestId("explanation")).toContainText("Convert");
  await expect(c.getByTestId("verdict")).toHaveText("ESCALATE");
  await expect(c.getByTestId("rule-ids")).toContainText("autonomy_level_gate");

  // Forecast interval: this conversion sells EUR, so the EUR 30 / 60 / 90 day intervals are shown.
  const table = c.getByRole("table", { name: /EUR net cash flow forecast intervals/ });
  for (const days of ["30 days", "60 days", "90 days"]) await expect(table.getByRole("row", { name: new RegExp(days) })).toBeVisible();
  await expect(table).toContainText("80% target coverage");

  // Simulation: the screen shows exactly what the API stored (checked as numbers, so formatting cannot hide a mismatch).
  const sim = p.decision!.simulation!;
  expect(money((await c.getByTestId("expected-saving").textContent())!)).toBeCloseTo(Number(sim.expected_saving.amount), 2);
  expect(money((await c.getByTestId("worst-case-loss").textContent())!)).toBeCloseTo(Number(sim.worst_case_loss.amount), 2);

  // The model's own words are tucked away and labelled; the figures above are what a person decides from.
  const words = c.locator("details", { hasText: "The agent's own words (unverified)" });
  await expect(words).not.toHaveAttribute("open", "");
  await words.locator("summary").click();
  await expect(words.locator(".untrusted")).toHaveText(p.rationale);

  await expect(c.getByRole("button", { name: "Approve" })).toBeEnabled();
  await expect(c.getByRole("button", { name: "Reject" })).toBeEnabled();
});

test("a blocked proposal sits in its own tab, names the rule that blocked it, and has no way to approve it", async ({ page }) => {
  await page.goto("/proposals");
  await expect(card(page, blocked.id)).toHaveCount(0); // not in the approval tab
  await page.getByRole("tab", { name: /Blocked \(2\)/ }).click();
  const c = card(page, blocked.id);
  await expect(c).toBeVisible();
  await expect(c.getByTestId("status")).toHaveText("Blocked by policy");
  await expect(c.getByTestId("verdict")).toHaveText("BLOCK");
  await expect(c.getByTestId("rule-ids")).toContainText("beneficiary_allowlist");
  await expect(page.getByRole("tabpanel").getByRole("button", { name: /approve|reject/i })).toHaveCount(0);
});

test("every number in every explanation is a number in that proposal's API payload", async ({ page }) => {
  const all = await listProposals();
  await page.goto("/proposals");
  let checked = 0;
  for (const tab of [/Needs approval/, /Decided/, /Blocked/]) {
    await page.getByRole("tab", { name: tab }).click();
    for (const el of await page.getByRole("tabpanel").locator("article").all()) {
      const id = (await el.getAttribute("data-proposal-id"))!;
      const text = (await el.getByTestId("explanation").textContent())!;
      expect(text.length).toBeGreaterThan(20);
      expect(unexplainedNumbers(text, all.find((p) => p.id === id)!), `numbers in "${text}"`).toEqual([]);
      checked++;
    }
  }
  expect(checked).toBe(all.length);
});

test("approving never shows 'executed' until the API says so, then follows the worker to settled", async ({ page }) => {
  const p = pending[0]!;
  // Hold the approval request for 3 s so the 'waiting for the API' state can be looked at.
  await page.route("**/api/tessera/proposals/*/approve", async (route) => {
    await new Promise((r) => setTimeout(r, 3000));
    await route.continue();
  });
  await page.goto("/proposals");
  const c = card(page, p.id);
  await c.getByRole("button", { name: "Approve" }).click();

  // In flight: nothing has changed anywhere yet, and the card must not claim otherwise.
  await expect(c.getByRole("button", { name: "Approving…" })).toBeDisabled();
  await expect(c.getByRole("button", { name: "Reject" })).toBeDisabled();
  await expect(c.getByTestId("status")).toHaveText("Needs your approval");
  await expect(c).not.toContainText(/executed|settled|submitted/i);
  expect((await listProposals()).find((x) => x.id === p.id)!.status).toBe("PENDING_APPROVAL");

  // The API answers: now (and only now) the screen says approved, and still not executed.
  await expect(page.getByTestId("notice")).toContainText("Approved · waiting for the worker", { timeout: 15_000 });
  await expect(page.getByTestId("notice")).not.toContainText(/executed|settled/i);
  await expect(card(page, p.id)).toHaveCount(0); // it left the approval tab
  await page.getByRole("tab", { name: /Decided/ }).click();
  const decided = card(page, p.id);
  await expect(decided.getByTestId("status")).toHaveText("Approved · waiting for the worker");
  await expect(decided.getByTestId("approval-record")).toContainText("Approved by");
  expect((await listProposals()).find((x) => x.id === p.id)!.status).toBe("APPROVED");

  // The worker re-checks policy and executes on its next cycle; the screen follows it by polling, never by assuming.
  await expect(decided.getByTestId("status")).toHaveText(/Submitted · not settled yet|Executed · settled/, { timeout: 40_000 });
  await expect(decided.getByTestId("status")).toHaveText("Executed · settled", { timeout: 40_000 });
  expect((await listProposals()).find((x) => x.id === p.id)!.status).toBe("SETTLED");
});

test("rejecting records the decision and never executes", async ({ page }) => {
  const p = pending[1]!;
  await page.goto("/proposals");
  await card(page, p.id).getByRole("button", { name: "Reject" }).click();
  await expect(page.getByTestId("notice")).toContainText("Rejected");
  await page.getByRole("tab", { name: /Decided/ }).click();
  const c = card(page, p.id);
  await expect(c.getByTestId("status")).toHaveText("Rejected");
  await expect(c.getByTestId("approval-record")).toContainText("Rejected by");
  await expect(c.getByRole("button", { name: /approve|reject/i })).toHaveCount(0);
  await page.waitForTimeout(12_000); // longer than one worker cycle: a rejected proposal must still be untouched
  expect((await listProposals()).find((x) => x.id === p.id)!.status).toBe("REJECTED");
});

test("the API refuses to approve an already-settled proposal, and nothing changes", async ({ page }) => {
  await page.goto("/proposals");
  // A settled proposal has no buttons, so provoke the API's own refusal through the proxy from the page.
  const status = await page.evaluate(async (id) => {
    const res = await fetch(`/api/tessera/proposals/${id}/approve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    return res.status;
  }, settled.id);
  expect(status).toBe(409);
  expect((await listProposals()).find((x) => x.id === settled.id)!.status).toBe("SETTLED");
});

test("a proposal the worker could not execute reads Failed, never executed (API response stubbed)", async ({ page }) => {
  const failed: ApiProposal = { ...settled, id: "prp-stubbed-failed", status: "FAILED", approval: { decision: "APPROVED", actor: "operator", decided_at: "2026-11-15T09:00:00Z", note: null } };
  await page.route("**/api/tessera/proposals?*", (route) => route.fulfill({ json: [failed] }));
  await page.goto("/proposals");
  await page.getByRole("tab", { name: /Decided/ }).click();
  const c = card(page, failed.id);
  await expect(c.getByTestId("status")).toHaveText("Failed");
  await expect(c).not.toContainText(/executed|settled/i);
  await expect(c.getByRole("button", { name: /approve|reject/i })).toHaveCount(0);
});

test.describe("the dashboard proxy", () => {
  const post = (page: import("@playwright/test").Page, path: string, body: unknown, origin: string | null = "http://127.0.0.1:3016") =>
    page.request.post(`/api/tessera/${path}`, { data: body as object, headers: origin === null ? {} : { Origin: origin } });

  test("refuses writes from another origin, to other paths, and the demote action", async ({ page }) => {
    await page.goto("/policies");
    expect((await post(page, `proposals/${settled.id}/approve`, {}, "https://evil.example")).status()).toBe(403);
    expect((await post(page, `proposals/${settled.id}/approve`, {}, null)).status()).toBe(403);
    expect((await post(page, "plan/run", { business_id: "B0" })).status()).toBe(405);
    expect((await post(page, "invoices", {})).status()).toBe(405);
    expect((await post(page, "autonomy", { action: "demote", reason: "x" })).status()).toBe(405);
    expect((await post(page, `proposals/${settled.id}/approve`, { actor: "someone-else" })).status()).toBe(405);
  });

  test("never lets the browser see the API key", async ({ page }) => {
    await page.goto("/proposals");
    const html = await page.content();
    expect(html).not.toContain(KEY);
    const sent: string[] = [];
    page.on("request", (r) => sent.push(JSON.stringify(r.headers())));
    await page.reload();
    expect(sent.join("")).not.toContain(KEY);
  });

  test("the API itself still refuses what the proxy would forward: a blocked proposal cannot be approved", async ({ page }) => {
    await page.goto("/policies");
    const res = await post(page, `proposals/${blocked.id}/approve`, {});
    expect(res.status()).toBe(409);
    expect((await apiGet<ApiProposal[]>("/proposals?business_id=B0&limit=200")).find((x) => x.id === blocked.id)!.status).toBe("BLOCKED");
  });
});
