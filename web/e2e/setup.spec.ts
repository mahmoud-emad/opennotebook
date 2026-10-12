// The first run and the AI providers, in a browser against the whole stack.
//
// The suite's studio is already set up (CI gives it a provider in the
// environment, at an address that never answers), so the tour is reached by
// answering `/api/setup` as a fresh studio would, and connecting a key is
// answered here too: the suite has no real key to test. Everything else is
// the real server: the provider presets, the model rows, and testing the
// environment's provider, which fails readably because nothing answers.

import { expect, request as http, test, type Page } from "@playwright/test";
import { BASE } from "./base";

const FRESH = { ready: false, can_setup: true, providers: [], message: "Connect an AI provider to start." };

async function freshStudio(page: Page) {
  await page.route("**/api/setup", (r) => r.fulfill({ json: FRESH }));
}

test("a fresh studio shows the setup tour, not the studio", async ({ page }) => {
  await freshStudio(page);
  await page.goto("");
  await expect(page.getByRole("heading", { name: "Welcome to OpenNotebook" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(0);
});

test("a refused key says why, and a working one leads to the models and the studio", async ({ page }) => {
  await freshStudio(page);
  let tries = 0;
  await page.route("**/api/ai/providers", async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    tries += 1;
    if (tries === 1)
      return r.fulfill({
        status: 422,
        json: { detail: "OpenRouter refused this key. Check that you copied all of it." },
      });
    return r.fulfill({
      status: 201,
      json: {
        id: "openrouter",
        kind: "openrouter",
        label: "OpenRouter",
        base_url: "https://openrouter.ai/api/v1",
        source: "app",
        key_hint: "…1234",
        primary: true,
        check: {
          status: "ok",
          ok: true,
          sentence: "Connected to OpenRouter: $5.00 left on this key.",
          balance: "$5.00 left on this key",
          models: 300,
          checked_at: "2026-10-12T00:00:00+00:00",
        },
      },
    });
  });
  await page.goto("");
  await page.getByRole("button", { name: /Connect a provider/ }).click();
  // The presets are the server's own.
  await expect(page.getByRole("radio", { name: /Azure OpenAI/ })).toBeVisible();
  await page.getByRole("radio", { name: /OpenRouter/ }).click();
  const go = page.getByRole("button", { name: "Test and connect" });
  await expect(go).toBeDisabled();
  await page.getByPlaceholder("Paste your key").fill("sk-or-not-real");
  await go.click();
  await expect(page.getByRole("alert")).toHaveText("OpenRouter refused this key. Check that you copied all of it.");
  await go.click();
  await expect(page.getByRole("heading", { name: "Choose models" })).toBeVisible();
  await expect(page.getByText("Connected to OpenRouter: $5.00 left on this key.")).toBeVisible();
  // The same model rows as Settings › Models, from the real settings.
  await expect(page.getByRole("combobox", { name: "Chat & Ask model" })).toBeVisible();
  await page.getByRole("button", { name: /Open the studio/ }).click();
  await expect(page.getByRole("heading", { name: "Discover", level: 2 })).toBeVisible();
});

test("a local server asks for its address, not a key", async ({ page }) => {
  await freshStudio(page);
  await page.goto("");
  await page.getByRole("button", { name: /Connect a provider/ }).click();
  await page.getByRole("radio", { name: /Ollama/ }).click();
  await expect(page.getByPlaceholder("Paste your key")).toHaveCount(0);
  await expect(page.getByPlaceholder("http://localhost:1234/v1")).toHaveValue("http://localhost:11434/v1");
});

test("Settings › AI providers tests the environment's provider for real", async ({ page }) => {
  await page.goto("");
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("tab", { name: "AI providers" }).click();
  await expect(page.getByText("Set in the server's environment")).toBeVisible();
  // Set in the environment: it can be tested, not removed.
  await expect(page.getByRole("button", { name: "Remove" })).toHaveCount(0);
  await page.getByRole("button", { name: "Test again" }).click();
  await expect(page.getByRole("alert")).toContainText("did not answer at");
});

test("the studio answers agents over MCP", async () => {
  const api = await http.newContext({ baseURL: new URL("/api/", BASE).href });
  const res = await api.post("mcp", {
    headers: { Accept: "application/json, text/event-stream", "mcp-protocol-version": "2025-06-18" },
    data: { jsonrpc: "2.0", id: 1, method: "tools/list" },
  });
  expect(res.ok()).toBe(true);
  const names = ((await res.json()) as { result: { tools: { name: string }[] } }).result.tools.map((t) => t.name);
  expect(names).toEqual(expect.arrayContaining(["list_collections", "ask", "add_links"]));
  await api.dispose();
});
