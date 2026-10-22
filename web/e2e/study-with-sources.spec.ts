// Working with a collection's sources, in a browser against the whole stack:
// switch a source off, search them, keep an answer as a note, and run
// transformations.
//
// The first group needs no model, so it runs anywhere. The second asks a
// real one, so it runs only when E2E_AI=1 and the studio has a working
// provider (CI's never answers).
//
// Everything the tests make is deleted at the end, through the api, found by
// the tag in its title or name.

import { expect, request as http, test, type Page } from "@playwright/test";
import { BASE } from "./base";

const TAG = `e2e-${Date.now().toString(36)}`;
const REEFS = `Coral reefs cover under one percent of the ocean floor but shelter a quarter of marine species ${TAG}.`;
const KELP = `Kelp forests grow along cold coasts and shelter fish, otters and sea urchins ${TAG}.`;
// A typed note is listed under its first six words.
const nameOf = (note: string) => note.split(/\s+/).slice(0, 6).join(" ");
const REEFS_NAME = nameOf(REEFS);
const KELP_NAME = nameOf(KELP);

test.describe.configure({ mode: "serial" });

test.afterAll(async () => {
  const api = await http.newContext({ baseURL: new URL("/api/", BASE).href });
  const cols = await api.get("collections");
  if (cols.ok()) {
    const all = (await cols.json()) as { id: string; title: string }[];
    for (const c of all.filter((c) => c.title.includes(TAG))) await api.delete(`collections/${c.id}`);
  }
  const tfs = await api.get("transformations");
  if (tfs.ok()) {
    const all = (await tfs.json()) as { id: string; name: string; builtin: boolean }[];
    for (const t of all.filter((t) => !t.builtin && t.name.includes(TAG))) await api.delete(`transformations/${t.id}`);
  }
  await api.dispose();
});

const sourcesPanel = (page: Page) => page.getByRole("complementary", { name: "Sources" });

/** A new collection holding the two notes, opened on its page. */
async function collectionWithNotes(page: Page): Promise<string> {
  await page.goto("my-collections");
  await page.getByRole("button", { name: "New collection" }).first().click();
  await expect(page).toHaveURL(/\/ui\/c\/[\w-]+$/);
  const url = page.url();
  const name = page.getByRole("textbox", { name: "Collection name" });
  await name.fill(`Sources suite ${TAG}`);
  await name.press("Enter");
  for (const [note, listed] of [
    [REEFS, REEFS_NAME],
    [KELP, KELP_NAME],
  ] as const) {
    await page.getByRole("textbox", { name: "A link, some text, or a topic" }).fill(note);
    await page.getByRole("button", { name: "Add source" }).click();
    // The row that says the note is being added has its words too; the
    // switch is only on a source the server holds.
    await expect(sourcesPanel(page).getByRole("switch", { name: `Use ${listed}` })).toBeVisible();
  }
  return url;
}

test("a source is switched off, stays off after a reload, and is switched on again", async ({ page }) => {
  await collectionWithNotes(page);
  const reefs = sourcesPanel(page).getByRole("switch", { name: `Use ${REEFS_NAME}` });
  await expect(reefs).toHaveAttribute("aria-checked", "true");

  await reefs.click();
  // The row answers at once and says what it now is.
  await expect(reefs).toHaveAttribute("aria-checked", "false");
  await expect(sourcesPanel(page).getByText(/^Not used · /)).toHaveCount(1);
  // The other one is untouched, and the collection still holds both.
  await expect(sourcesPanel(page).getByRole("switch", { name: `Use ${KELP_NAME}` })).toHaveAttribute(
    "aria-checked",
    "true",
  );

  // The server kept it.
  await page.reload();
  await expect(sourcesPanel(page).getByRole("switch", { name: `Use ${REEFS_NAME}` })).toHaveAttribute(
    "aria-checked",
    "false",
  );

  await sourcesPanel(page).getByRole("switch", { name: `Use ${REEFS_NAME}` }).click();
  await expect(sourcesPanel(page).getByRole("switch", { name: `Use ${REEFS_NAME}` })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  await expect(sourcesPanel(page).getByText(/^Not used · /)).toHaveCount(0);
});

test("the sources are searched, a hit opens its source, and a source that is off is not searched", async ({
  page,
}) => {
  await collectionWithNotes(page);
  const box = page.getByRole("searchbox", { name: "Search the sources" });
  await expect(box).toBeVisible();

  await box.fill("otters");
  const hit = sourcesPanel(page).getByRole("button", { name: /Kelp forests grow/ });
  await expect(hit).toBeVisible();
  // The source list gives way to the passages found.
  await expect(sourcesPanel(page).getByRole("switch")).toHaveCount(0);
  await hit.click();
  const drawer = page.getByRole("dialog", { name: "Source" });
  await expect(drawer).toBeVisible();
  await expect(drawer.locator(".mark")).toContainText("otters");
  await drawer.getByRole("button", { name: /Close/ }).click();
  await expect(drawer).toBeHidden();

  await box.fill("zebra");
  await expect(sourcesPanel(page).getByText(/Nothing in the sources matches “zebra”/)).toBeVisible();

  // Cleared, the list is back.
  await box.fill("");
  await expect(sourcesPanel(page).getByRole("switch")).toHaveCount(2);

  // Off, a source is not found.
  const kelp = sourcesPanel(page).getByRole("switch", { name: `Use ${KELP_NAME}` });
  await kelp.click();
  await expect(kelp).toHaveAttribute("aria-checked", "false");
  await box.fill("otters");
  // Even a search made while the switch was still on its way is made again
  // once it has landed.
  await expect(sourcesPanel(page).getByText(/Nothing in the sources matches “otters”/)).toBeVisible();
});

test("Settings › Transformations lists the built-in ones and keeps your own", async ({ page }) => {
  await page.goto("");
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("tab", { name: "Transformations" }).click();
  const panel = page.getByRole("tabpanel");
  for (const n of ["Summary", "Key claims", "Flashcards", "Timeline", "Open questions"])
    await expect(panel.getByText(n, { exact: true })).toBeVisible();
  // The built-in ones are read, not changed: nothing to change or remove yet.
  await expect(panel.getByRole("button", { name: "Change" })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Remove" })).toHaveCount(0);

  const mine = `Glossary ${TAG}`;
  await panel.getByLabel("Name").fill(mine);
  await panel.getByLabel("Instruction").fill("Define every technical term, one line each.");
  await panel.getByRole("button", { name: "Add", exact: true }).click();
  await expect(panel.getByText(mine, { exact: true })).toBeVisible();
  await expect(panel.getByText("Define every technical term, one line each.")).toBeVisible();

  // A second one with the same name is refused in words.
  await panel.getByLabel("Name").fill(mine);
  await panel.getByLabel("Instruction").fill("Something else.");
  await panel.getByRole("button", { name: "Add", exact: true }).click();
  await expect(panel.getByRole("alert")).toContainText("already have a transformation called");

  // A built-in name is refused too.
  await panel.getByLabel("Name").fill("Summary");
  await panel.getByRole("button", { name: "Add", exact: true }).click();
  await expect(panel.getByRole("alert")).toContainText("built-in transformation");

  // Change it, and it is kept across a reload.
  await panel.getByLabel("Name").fill("");
  await panel.getByLabel("Instruction").fill("");
  await panel
    .locator(".set-row", { hasText: mine })
    .getByRole("button", { name: "Change" })
    .click();
  await panel.getByLabel("Instruction").fill("Define each term, with an example.");
  await panel.getByRole("button", { name: "Save", exact: true }).click();
  await expect(panel.getByText("Define each term, with an example.")).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("tab", { name: "Transformations" }).click();
  await expect(page.getByRole("tabpanel").getByText("Define each term, with an example.")).toBeVisible();

  // Removing asks first.
  const row = page.getByRole("tabpanel").locator(".set-row", { hasText: mine });
  await row.getByRole("button", { name: "Remove" }).click();
  await row.getByRole("button", { name: "Keep it" }).click();
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Remove" }).click();
  await row.getByRole("button", { name: "Remove it" }).click();
  await expect(page.getByRole("tabpanel").getByText(mine, { exact: true })).toHaveCount(0);
});

test.describe("with a real model", () => {
  test.skip(!process.env.E2E_AI, "needs a working AI provider: run with E2E_AI=1");
  test.setTimeout(240_000);

  const send = async (page: Page, text: string) => {
    const input = page.getByRole("textbox", { name: "Message the studio" });
    await input.fill(text);
    await input.press("Enter");
  };

  /** Say something and wait for the Studio's answer to it. A provider that
   * answers "busy" (a rate limit, as a shared key meets) is waited out and
   * asked again: the studio says so in words, and that is not what is tested. */
  const turn = async (page: Page, text: string, expected: RegExp) => {
    const replies = page.locator(".cmsg:not(.me)");
    const busyText = "busy right now";
    for (let attempt = 0; attempt < 4; attempt++) {
      const seen = await page.locator(".step-row.bad", { hasText: busyText }).count();
      await send(page, text);
      const answered = replies.last().filter({ hasText: expected });
      const busy = page.locator(".step-row.bad", { hasText: busyText }).nth(seen);
      await expect(answered.or(busy)).toBeVisible({ timeout: 120_000 });
      if (await answered.isVisible()) return replies.last();
      await page.waitForTimeout(20_000);
    }
    throw new Error("The AI provider stayed busy through four tries.");
  };

  test("an answer is cited from the sources that are on, and kept as a note", async ({ page }) => {
    await collectionWithNotes(page);
    await page.getByRole("tab", { name: "Ask" }).click();
    // Only the kelp note is on.
    await sourcesPanel(page).getByRole("switch", { name: `Use ${REEFS_NAME}` }).click();
    await expect(sourcesPanel(page).getByRole("switch", { name: `Use ${REEFS_NAME}` })).toHaveAttribute(
      "aria-checked",
      "false",
    );

    const answer = await turn(page, "/ask What lives in kelp forests?", /otters|fish|urchins/i);
    await expect(answer.locator(".cite").first()).toBeVisible();
    await expect(answer).toContainText(/otters|fish|urchins/i);
    // Cited to the note that is on, never the one that is off.
    await expect(answer.locator(".cites")).toContainText("Kelp forests grow");
    await expect(answer.locator(".cites")).not.toContainText("Coral reefs");

    await answer.getByRole("button", { name: /Save as note/ }).click();
    await expect(answer.getByText(/Saved as a note in your sources/)).toBeVisible();
    // It is a source now: a third row, led by the answer's first words.
    await expect(sourcesPanel(page).getByRole("switch")).toHaveCount(3);
    await page.reload();
    await expect(sourcesPanel(page).getByRole("switch")).toHaveCount(3);
  });

  test("/transform runs a built-in and a custom transformation, with citations", async ({ page }) => {
    await collectionWithNotes(page);
    const mine = `Glossary ${TAG}`;
    const api = await http.newContext({ baseURL: new URL("/api/", BASE).href });
    const made = await api.post("transformations", {
      data: { name: mine, prompt: "List each animal named in the sources, one bullet each." },
    });
    expect(made.ok()).toBe(true);
    await api.dispose();

    await page.getByRole("tab", { name: "Ask" }).click();
    const first = await turn(page, "/transform summary", /Summary/);
    await expect(first.locator(".cite").first()).toBeVisible();

    const second = await turn(page, `/transform ${mine} only the sea mammals`, new RegExp(mine));
    await expect(second).toContainText(/otter/i);

    // An unknown name lists what there is, and nothing is spent.
    await send(page, "/transform poem");
    await expect(page.locator(".cmsg:not(.me)").last()).toContainText("There is no transformation called “poem”");

    // Shown, not kept: the answer's own button keeps it.
    await expect(sourcesPanel(page).getByRole("switch")).toHaveCount(2);
  });

  test("with every source off, a question says why instead of failing", async ({ page }) => {
    await collectionWithNotes(page);
    for (const n of [REEFS_NAME, KELP_NAME]) await sourcesPanel(page).getByRole("switch", { name: `Use ${n}` }).click();
    await expect(sourcesPanel(page).getByText(/^Not used · /)).toHaveCount(2);
    await page.getByRole("tab", { name: "Ask" }).click();
    await send(page, "/ask What is in the sources?");
    await expect(page.locator(".cmsg:not(.me)").last()).toContainText("switched off", { timeout: 60_000 });
  });
});
