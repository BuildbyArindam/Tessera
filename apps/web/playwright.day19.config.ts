import { defineConfig } from "@playwright/test";

/**
 * Day 19 end-to-end test: the whole treasury loop driven through the dashboard, on the MOCK Airwallex adapter.
 *
 *   npm run build && npm run test:e2e:day19        (or `make e2e-demo` from the repository root)
 *
 * `scripts/serve_day19_demo.py` starts the real API, the real worker loop (scripted planner) and a demo-only control
 * app on a fresh database; `next start` serves the dashboard. Separate ports and database from the Day 16 config, so the
 * two suites never share state. Browser: Playwright's Chromium, or CHROME_PATH=/usr/bin/google-chrome.
 * Needs PostgreSQL at TEST_DATABASE_ADMIN_URL (default :5433).
 */
const API_PORT = 8019;
const CONTROL_PORT = 8039;
const WEB_PORT = 3019;
const KEY = "e2e-demo-key-0123456789";
const PY = process.env.PYTHON ?? "../../.venv/bin/python";

export default defineConfig({
  testDir: "e2e",
  testMatch: "19-*.spec.ts",
  timeout: 150_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  outputDir: "e2e-artifacts/results-day19",
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "retain-on-failure",
    launchOptions: { executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] },
  },
  webServer: [
    {
      command: `${PY} ../../scripts/serve_day19_demo.py --port ${API_PORT} --control-port ${CONTROL_PORT} --worker-every 2`,
      url: `http://127.0.0.1:${API_PORT}/docs`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: { TESSERA_API_KEY: KEY },
    },
    {
      command: `npx next start -p ${WEB_PORT}`,
      url: `http://127.0.0.1:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: { TESSERA_API_URL: `http://127.0.0.1:${API_PORT}`, TESSERA_API_KEY: KEY },
    },
  ],
});
