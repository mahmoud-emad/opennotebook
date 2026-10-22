import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { said, type Msg } from "./api-studio";
import { SaveAsNote, greeting, noteOf, savable } from "./chat";

const answer = (more: Partial<Msg> = {}): Msg => ({
  ...said("Studio", "Reefs shelter **a quarter** of species [1][2].", false),
  cites: [
    { n: 1, title: "Reefs", url: "https://example.org/reefs", name: "reefs.md", excerpt: "x" },
    { n: 2, title: "A note", url: "", name: "note.md", excerpt: "y" },
  ],
  ...more,
});

afterEach(cleanup);

describe("keeping an answer as a note", () => {
  it("offers it for what the Studio said, but not its greeting, steps or the person's words", () => {
    expect(savable(answer())).toBe(true);
    expect(savable(greeting()[0]!)).toBe(false);
    expect(savable(answer({ kind: "step" }))).toBe(false);
    expect(savable(answer({ me: true }))).toBe(false);
    expect(savable(answer({ text: "  " }))).toBe(false);
  });

  it("writes the answer with the passages it cites, and a plain title", () => {
    const { title, text } = noteOf(answer());
    expect(title).toBe("Reefs shelter a quarter of species.");
    expect(text).toBe(
      "Reefs shelter **a quarter** of species [1][2].\n\nSources:\n\n1. Reefs (https://example.org/reefs)\n2. A note",
    );
    // No citations: the answer as it is.
    expect(noteOf(answer({ cites: [] })).text).toBe("Reefs shelter **a quarter** of species [1][2].");
  });

  it("saves once and then says it is saved", async () => {
    const onSave = vi.fn(() => Promise.resolve());
    render(<SaveAsNote m={answer()} onSave={onSave} />);
    fireEvent.click(screen.getByRole("button", { name: /Save as note/ }));
    await waitFor(() => expect(screen.getByText(/Saved as a note/)).toBeTruthy());
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("says why it could not, and lets the person try again", async () => {
    const onSave = vi.fn(() => Promise.reject(new Error("The collection is read only.")));
    render(<SaveAsNote m={answer()} onSave={onSave} />);
    fireEvent.click(screen.getByRole("button", { name: /Save as note/ }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("It could not be saved."));
    expect(screen.getByRole("alert").textContent).toContain("The collection is read only.");
    expect(screen.getByRole("button", { name: /Save as note/ })).toBeTruthy();
  });
});
