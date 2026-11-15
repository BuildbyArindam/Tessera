import { expect, test } from "@playwright/test";
import { apiGet } from "./helpers";

interface Policy { ruleset_hash: string; rules: { id: string; type: string; version: number; params: Record<string, unknown> }[] }
interface Autonomy { level: string; evidence_available: boolean }

const AUTONOMY = {
  level: "L1",
  since: "2026-11-01T00:00:00Z",
  evidence_available: true,
  pending_promotion: { id: 7, from_level: "L1", to_level: "L2", status: "PENDING", verdict: "ESCALATE", proposed_at: "2026-11-14T00:00:00Z", decided_at: null, actor: null, note: null },
  assessment: {
    kind: "PROPOSE_PROMOTION", from_level: "L1", to_level: "L2", triggers: [], eligible: true,
    config_hash: "cd".repeat(32), assessed_at: "2026-11-15T00:00:00Z",
    criteria: [{ name: "settled_actions", passed: true, observed: "40", required: ">= 30" }, { name: "violations", passed: false, observed: "1", required: "0" }],
  },
};

test("the rule list and ruleset hash are exactly what the API reports, and nothing in them is editable", async ({ page }) => {
  const policy = await apiGet<Policy>("/policies");
  await page.goto("/policies");
  await expect(page.getByTestId("ruleset-hash")).toHaveText(policy.ruleset_hash);
  expect(policy.rules.length).toBeGreaterThan(0);
  for (const rule of policy.rules) await expect(page.locator(`[data-rule-id="${rule.id}"]`)).toBeVisible();
  await expect(page.locator("[data-rule-id]")).toHaveCount(policy.rules.length);
  await expect(page.locator("[data-rule-id] input, [data-rule-id] select, [data-rule-id] textarea, [data-rule-id] button")).toHaveCount(0);
});

test("the autonomy level is the API's, and with no evidence source there is nothing to confirm", async ({ page }) => {
  const autonomy = await apiGet<Autonomy>("/autonomy");
  await page.goto("/policies");
  await expect(page.getByTestId("autonomy-level")).toContainText(autonomy.level);
  expect(autonomy.evidence_available).toBe(false); // the demo API has no evidence source configured
  await expect(page.getByTestId("no-evidence")).toBeVisible();
  await expect(page.getByRole("button", { name: "Confirm promotion" })).toHaveCount(0);
});

test.describe("confirming a promotion (API stubbed: the demo API has no evidence source)", () => {
  test("sends exactly the confirm action, and reports the level only after the API answers", async ({ page }) => {
    let body: unknown = null;
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    await page.route("**/api/tessera/autonomy", async (route) => {
      if (route.request().method() === "GET") return route.fulfill({ json: AUTONOMY });
      body = route.request().postDataJSON();
      await gate;
      return route.fulfill({ json: { ...AUTONOMY, level: "L2", pending_promotion: null } });
    });
    await page.goto("/policies");
    await expect(page.getByRole("row", { name: /settled_actions/ })).toContainText("40"); // criteria table rendered, with text marks and not colour alone
    await page.getByRole("button", { name: "Confirm promotion" }).click();
    await expect(page.getByRole("button", { name: "Confirming…" })).toBeDisabled();
    await expect(page.getByTestId("autonomy-level")).toContainText("L1"); // nothing assumed while the API is thinking
    await expect(page.getByText(/now L2/)).toHaveCount(0);
    release();
    await expect(page.getByText("The API reports the level is now L2.")).toBeVisible();
    await expect(page.getByTestId("autonomy-level")).toContainText("L2");
    expect(body).toEqual({ action: "confirm_promotion", promotion_id: 7 });
  });

  test("a refusal from the API is shown and the level stays where it was", async ({ page }) => {
    await page.route("**/api/tessera/autonomy", (route) =>
      route.request().method() === "GET"
        ? route.fulfill({ json: AUTONOMY })
        : route.fulfill({ status: 409, json: { detail: "evidence no longer meets the criteria" } }),
    );
    await page.goto("/policies");
    await page.getByRole("button", { name: "Confirm promotion" }).click();
    await expect(page.locator("p[role=alert]")).toContainText("evidence no longer meets the criteria");
    await expect(page.getByTestId("autonomy-level")).toContainText("L1");
  });

  test("an unreachable API says nothing was changed", async ({ page }) => {
    await page.route("**/api/tessera/autonomy", (route) =>
      route.request().method() === "GET" ? route.fulfill({ json: AUTONOMY }) : route.fulfill({ status: 503, json: { detail: "database unavailable" } }),
    );
    await page.goto("/policies");
    await page.getByRole("button", { name: "Confirm promotion" }).click();
    await expect(page.locator("p[role=alert]")).toBeVisible();
    await expect(page.getByTestId("autonomy-level")).toContainText("L1");
  });
});
