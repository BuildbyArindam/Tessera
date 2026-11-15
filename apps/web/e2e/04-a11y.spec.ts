import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { listProposals } from "./helpers";

const PAGES = ["/proposals", "/policies", "/audit"];
const SHOTS = "e2e-artifacts/screens";
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

for (const scheme of ["light", "dark"] as const) {
  for (const path of PAGES) {
    test(`${path} has no axe violations (${scheme})`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await page.goto(path);
      await expect(page.locator("main")).toBeVisible();
      if (path === "/proposals") {
        await page.getByRole("tab", { name: /Decided/ }).click(); // earlier specs decided the escalated ones
        await expect(page.locator("article").first()).toBeVisible();
      }
      if (path === "/audit") await expect(page.locator("tbody tr").first()).toBeVisible();
      const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
    });
  }
}

test("proposal cards stay accessible with a blocked card and the model's own words open", async ({ page }) => {
  await page.goto("/proposals");
  await page.getByRole("tab", { name: /Decided/ }).click();
  await page.locator("details summary").first().click();
  await page.getByRole("tab", { name: /Blocked/ }).click();
  await page.locator("details summary").first().click();
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  expect(results.violations).toEqual([]);
});

test("the proposal tabs work from the keyboard", async ({ page }) => {
  await page.goto("/proposals");
  const tabs = page.getByRole("tab");
  await tabs.first().focus();
  await page.keyboard.press("ArrowRight");
  await expect(tabs.nth(1)).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("End");
  await expect(tabs.last()).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Home");
  await expect(tabs.first()).toHaveAttribute("aria-selected", "true");
});

for (const [name, viewport] of [["desktop", { width: 1280, height: 900 }], ["mobile", { width: 390, height: 844 }]] as const) {
  test(`no horizontal overflow, and screenshots, at ${name} width`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const all = await listProposals();
    expect(all.length).toBeGreaterThan(0);
    for (const path of PAGES) {
      await page.goto(path);
      await expect(page.locator("main")).toBeVisible();
      await page.waitForLoadState("networkidle");
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, `${path} overflows by ${overflow}px`).toBeLessThanOrEqual(0);
      await page.screenshot({ path: `${SHOTS}/${name}${path.replace("/", "-")}.png`, fullPage: true });
    }
  });
}
