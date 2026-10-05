// The main path through the studio, without a model: find your way around,
// start a collection, give it a note, share it, find it on Discover, and
// reuse it as a read-only copy. No AI key is assumed, so nothing here asks a
// model for anything.
//
// Everything the test makes is deleted at the end: through the app when the
// test gets that far, and through the api after it whatever happened, found
// by the ids it saw and by the tag in its title.

import { expect, request as http, test, type Page } from "@playwright/test";
import { BASE } from "./base";

const TAG = `e2e-${Date.now().toString(36)}`;
const TITLE = `Shared by the suite ${TAG}`;
const NOTE = `A note the end-to-end suite wrote, ${TAG}, to share and reuse.`;
// A typed note is listed under its first six words.
const NOTE_NAME = NOTE.split(/\s+/).slice(0, 6).join(" ");

// The collections the test opened, by id, for the clean-up below.
const made = new Set<string>();
const remember = (page: Page) => made.add(page.url().split("/c/")[1] ?? "");

test.describe.configure({ mode: "serial" });

test.afterAll(async () => {
  const api = await http.newContext({ baseURL: new URL("/api/", BASE).href });
  const res = await api.get("collections");
  if (res.ok()) {
    const all = (await res.json()) as { id: string; title: string }[];
    for (const c of all.filter((c) => c.title.includes(TAG))) made.add(c.id);
  }
  // A 404 is fine: the test already deleted it.
  for (const id of made) if (id !== "") await api.delete(`collections/${id}`);
  await api.dispose();
});

const nav = (page: Page, name: "Discover" | "My collections") =>
  page.getByRole("navigation", { name: "Main" }).getByRole("link", { name });

test("Discover is the home page", async ({ page }) => {
  await page.goto("");
  await expect(page.getByRole("heading", { name: "Discover", level: 2 })).toBeVisible();
  await expect(nav(page, "Discover")).toHaveAttribute("aria-current", "page");
});

test("My collections opens from the menu, and its address reloads", async ({ page }) => {
  await page.goto("");
  await nav(page, "My collections").click();
  await expect(page).toHaveURL(/\/ui\/my-collections$/);
  await expect(page.getByRole("heading", { name: "My collections", level: 2 })).toBeVisible();
  // A deep link has to survive a reload: the old app lost it.
  await page.reload();
  await expect(page.getByRole("heading", { name: "My collections", level: 2 })).toBeVisible();
});

test("a collection is made, shared, found on Discover and reused read-only", async ({ page }) => {
  // ── make it ──
  await page.goto("my-collections");
  await page.getByRole("button", { name: "New collection" }).first().click();
  await expect(page).toHaveURL(/\/ui\/c\/[\w-]+$/);
  remember(page);
  const original = page.url();

  const name = page.getByRole("textbox", { name: "Collection name" });
  await name.fill(TITLE);
  await name.press("Enter");

  // ── give it a note ──
  await page.getByRole("textbox", { name: "A link, some text, or a topic" }).fill(NOTE);
  await page.getByRole("button", { name: "Add source" }).click();
  const sources = page.getByRole("complementary", { name: "Sources" });
  await expect(sources.getByText(NOTE_NAME)).toBeVisible();
  await expect(page.getByText("1 source", { exact: true })).toBeVisible();

  // The collection's own address reloads to the same collection.
  await page.reload();
  await expect(page.getByRole("textbox", { name: "Collection name" })).toHaveValue(TITLE);
  await expect(sources.getByText(NOTE_NAME)).toBeVisible();

  // ── share it, sources in, edits off ──
  await page.getByRole("button", { name: "Share", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: `Share “${TITLE}”` });
  await expect(dialog.getByRole("switch", { name: "Include sources (1)" })).toHaveAttribute("aria-checked", "true");
  await expect(
    dialog.getByRole("switch", { name: "Let people edit their copy and share it again" }),
  ).toHaveAttribute("aria-checked", "false");
  await dialog.getByLabel("Note (optional)").fill(`Made by ${TAG}`);
  await dialog.getByRole("button", { name: "Share", exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("button", { name: "Shared", exact: true })).toBeVisible();

  // ── find it on Discover ──
  await nav(page, "Discover").click();
  const card = page.getByRole("link", { name: new RegExp(TITLE) });
  await expect(card).toBeVisible();
  await card.click();
  await expect(page).toHaveURL(/\/ui\/shared\/[\w-]+$/);
  await expect(page.getByRole("heading", { name: TITLE, level: 2 })).toBeVisible();

  // ── reuse it: a copy of one's own, read only ──
  await page.getByRole("button", { name: "Reuse collection" }).click();
  await expect(page).toHaveURL(/\/ui\/c\/[\w-]+$/);
  expect(page.url()).not.toBe(original);
  remember(page);
  await expect(page.getByText("Read-only copy of")).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Collection name" })).not.toBeEditable();
  // The note came with it, and cannot be added to.
  await expect(sources.getByText(NOTE_NAME)).toBeVisible();
  await expect(page.getByRole("button", { name: "Add source" })).toHaveCount(0);

  // ── delete both, through the app ──
  await nav(page, "My collections").click();
  const menus = page.getByRole("button", { name: `More actions for ${TITLE}` });
  await expect(menus).toHaveCount(2);
  for (const left of [1, 0]) {
    await menus.first().click();
    await page.getByRole("menuitem", { name: "Delete" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Delete" }).click();
    await expect(menus).toHaveCount(left);
  }

  // The share went with the original, so Discover no longer lists it.
  await nav(page, "Discover").click();
  await expect(page.getByRole("heading", { name: "Discover", level: 2 })).toBeVisible();
  await expect(page.getByRole("link", { name: new RegExp(TITLE) })).toHaveCount(0);
});
