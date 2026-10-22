import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatInput } from "./chat";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const box = () => screen.getByLabelText("Message the studio") as HTMLInputElement;

describe("the chat box", () => {
  it("sends what was typed and clears itself", () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} />);
    fireEvent.change(box(), { target: { value: "What shelters fish?" } });
    fireEvent.keyDown(box(), { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("What shelters fish?");
    expect(box().value).toBe("");
  });

  it("keeps what was typed while a turn is running, and says it is waiting", () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} busy />);
    fireEvent.change(box(), { target: { value: "And what else?" } });
    const send = screen.getByRole("button", { name: "Send" }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    expect(send.getAttribute("aria-busy")).toBe("true");
    expect(box().placeholder).toContain("Working on it");
    fireEvent.keyDown(box(), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    // Nothing lost: it is still there to send when the answer is in.
    expect(box().value).toBe("And what else?");
  });

  it("sends the kept message once the turn is over", () => {
    const onSend = vi.fn();
    const { rerender } = render(<ChatInput onSend={onSend} busy />);
    fireEvent.change(box(), { target: { value: "And what else?" } });
    rerender(<ChatInput onSend={onSend} busy={false} />);
    fireEvent.keyDown(box(), { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("And what else?");
  });
});
