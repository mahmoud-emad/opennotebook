import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SrcRow, type Src } from "./sources";

const src = (more: Partial<Src> = {}): Src => ({
  name: "Reefs note",
  detail: "Note · 12 words",
  ok: true,
  url: "",
  icon: "",
  file: "reefs.md",
  ...more,
});

describe("a source's switch", () => {
  it("shows no switch unless the row can be switched", () => {
    const { container, rerender } = render(<SrcRow s={src()} />);
    expect(container.querySelector(".src-use")).toBeNull();
    rerender(<SrcRow s={src({ file: "" })} onUse={() => undefined} />);
    expect(container.querySelector(".src-use")).toBeNull();
    rerender(<SrcRow s={src({ ok: false })} onUse={() => undefined} />);
    expect(container.querySelector(".src-use")).toBeNull();
  });

  it("is on by default and asks to switch the source off", () => {
    const onUse = vi.fn();
    const { container } = render(<SrcRow s={src()} onUse={onUse} />);
    const sw = container.querySelector(".src-use") as HTMLElement;
    expect(sw.getAttribute("aria-checked")).toBe("true");
    expect(container.querySelector(".src.off")).toBeNull();
    fireEvent.click(sw);
    expect(onUse).toHaveBeenCalledWith(expect.objectContaining({ file: "reefs.md" }), false);
  });

  it("dims a source that is off, says so, and asks to switch it on", () => {
    const onUse = vi.fn();
    const { container } = render(<SrcRow s={src({ active: false })} onUse={onUse} />);
    expect(container.querySelector(".src.off")).not.toBeNull();
    expect(container.querySelector(".src-d")?.textContent).toBe("Not used · Note · 12 words");
    const sw = container.querySelector(".src-use") as HTMLElement;
    expect(sw.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(sw);
    expect(onUse).toHaveBeenCalledWith(expect.anything(), true);
  });
});
