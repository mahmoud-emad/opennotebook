// End-to-end tests: a real browser against a running studio, its api and
// database included. `pnpm e2e` (or `make e2e`) runs them; start the studio
// first with `make dev`. They are kept out of `pnpm test`, which runs vitest
// on src/ only.
import { defineConfig, devices } from "@playwright/test";
import { BASE } from "./e2e/base";

export default defineConfig({
  testDir: "e2e",
  // One flow, in order, on one studio: nothing to gain from running at once.
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  forbidOnly: !!process.env.CI,
  reporter: [["list"]],
  use: {
    baseURL: BASE,
    // A failed run keeps a trace to step through: pnpm exec playwright show-trace.
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
