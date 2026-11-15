import { defineConfig } from "@playwright/test";

/**
 * Day 16 end-to-end tests. They drive the real dashboard against the real API and the real worker execution steps
 * (on the MOCK Airwallex adapter), started by `webServer` below from a fresh database every run.
 *
 *   npm run build && npx playwright test
 *
 * Browser: Playwright's own Chromium by default. Where it cannot be downloaded, point CHROME_PATH at an installed
 * Chrome (e.g. CHROME_PATH=/usr/bin/google-chrome). Needs PostgreSQL at TEST_DATABASE_ADMIN_URL (default :5433).
 */
const API_PORT = 8016;
const WEB_PORT = 3016;
const KEY = "e2e-demo-key-0123456789";
const PY = process.env.PYTHON ?? "../../.venv/bin/python";

export default defineConfig({
  testDir: "e2e",
  timeout: 90_000,
  expect: { timeout: 8_000 },
  fullyParallel: false,
  workers: 1, // the specs share one database and run in file order: proposals first, audit after the flows
  retries: 0,
  reporter: [["list"]],
  outputDir: "e2e-artifacts/results",
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "retain-on-failure",
    launchOptions: { executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] },
  },
  webServer: [
    {
      // The worker runs every 10 s so a person-approved proposal visibly waits as "Approved" before it settles.
      command: `${PY} ../../scripts/serve_day16_demo.py --port ${API_PORT} --worker-every 10 --database-name tessera_e2e`,
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
