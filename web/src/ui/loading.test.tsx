import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ItemRow, Progress } from "./outputs";
import { SrcRow, type Src } from "./sources";

afterEach(cleanup);

describe("what is on its way, said where it happens", () => {
  it("says why a build is waiting as the list says it now", () => {
    const { container, rerender } = render(<Progress sid="" waiting="Waiting for a worker." />);
    expect(container.textContent).toContain("Waiting for a worker.");
    rerender(<Progress sid="" waiting="Waiting for a free worker." />);
    expect(container.textContent).toContain("Waiting for a free worker.");
    rerender(<Progress sid="" waiting="" />);
    expect(container.textContent).not.toContain("Waiting");
  });

  it("shows a source being removed, with nothing to press", () => {
    const s: Src = { name: "Reefs", detail: "example.org · 10 words", ok: true, url: "", icon: "", file: "reefs.md" };
    const { container, rerender } = render(<SrcRow s={s} onRemove={() => undefined} />);
    expect(screen.getByRole("button", { name: "Remove Reefs" })).toBeTruthy();
    rerender(<SrcRow s={s} onRemove={() => undefined} busy />);
    expect(container.textContent).toContain("Removing…");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("puts a rename on its way where the row's menu was", () => {
    const row = (busy: string) => (
      <ItemRow
        icon="diagram-3"
        what="mind map"
        title="Reefs"
        facts="3 topics"
        whenMs={0}
        onRename={() => undefined}
        onDelete={() => undefined}
        busy={busy}
      />
    );
    const { rerender } = render(row(""));
    expect(screen.queryByRole("status")).toBeNull();
    rerender(row("Renaming…"));
    expect(screen.getByRole("status", { name: "Renaming…" })).toBeTruthy();
  });
});
