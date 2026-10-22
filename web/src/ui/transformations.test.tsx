import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TransformationsPanel } from "./transformations";

type Row = { id: string; name: string; description: string; prompt: string; builtin: boolean };

const BUILTIN: Row = {
  id: "summary",
  name: "Summary",
  description: "What the sources are about",
  prompt: "Summarize.",
  builtin: true,
};
const MINE: Row = { id: "u1", name: "Glossary", description: "", prompt: "Define the terms.", builtin: false };

function reply(status: number, body: unknown) {
  return Promise.resolve(
    new Response(status === 204 ? null : JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

/** A server holding `rows`, answering the four calls and recording them. */
function server(rows: Row[]) {
  const calls: [string, string, unknown][] = [];
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push([method, url, body]);
    if (method === "GET") return reply(200, rows);
    if (method === "POST") {
      const row = { id: "u2", description: "", builtin: false, ...body };
      rows.push(row);
      return reply(201, row);
    }
    if (method === "PUT") return reply(200, { ...MINE, ...body });
    return reply(204, null);
  });
  return calls;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Settings › Transformations", () => {
  it("lists the built-in ones to read and the person's own to change", async () => {
    server([BUILTIN, MINE]);
    render(<TransformationsPanel />);
    expect(await screen.findByText("Summary")).toBeTruthy();
    expect(screen.getByText("What the sources are about")).toBeTruthy();
    expect(screen.getByText("Glossary")).toBeTruthy();
    expect(screen.getByText("Define the terms.")).toBeTruthy();
    // Only their own can be changed or removed.
    expect(screen.getAllByRole("button", { name: "Change" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Remove" })).toHaveLength(1);
  });

  it("says there are none of their own yet, and adds one", async () => {
    const calls = server([BUILTIN]);
    render(<TransformationsPanel />);
    expect(await screen.findByText("You have none yet. Add one below.")).toBeTruthy();
    const add = screen.getByRole("button", { name: "Add" }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: "Glossary" } });
    fireEvent.change(screen.getByLabelText(/Instruction/), { target: { value: "Define the terms." } });
    expect(add.disabled).toBe(false);
    fireEvent.click(add);
    expect(await screen.findByText("Define the terms.")).toBeTruthy();
    expect(calls.find((c) => c[0] === "POST")?.[2]).toEqual({ name: "Glossary", prompt: "Define the terms." });
    // The form is empty again.
    expect((screen.getByLabelText(/Name/) as HTMLInputElement).value).toBe("");
  });

  it("changes one of their own", async () => {
    const calls = server([BUILTIN, MINE]);
    render(<TransformationsPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Change" }));
    expect((screen.getByLabelText(/Name/) as HTMLInputElement).value).toBe("Glossary");
    fireEvent.change(screen.getByLabelText(/Instruction/), { target: { value: "Define every term." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c[0] === "PUT" && c[1].endsWith("/transformations/u1"))).toBe(true));
    expect(calls.find((c) => c[0] === "PUT")?.[2]).toEqual({ name: "Glossary", prompt: "Define every term." });
  });

  it("asks before removing, and removes only when told to", async () => {
    const calls = server([BUILTIN, MINE]);
    render(<TransformationsPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    expect(calls.some((c) => c[0] === "DELETE")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));
    expect(screen.getByRole("button", { name: "Remove" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove it" }));
    expect(screen.getByRole("status").textContent).toContain("Removing…");
    await waitFor(() => expect(calls.some((c) => c[0] === "DELETE" && c[1].endsWith("/transformations/u1"))).toBe(true));
  });

  it("says why a save was refused, in the server's words", async () => {
    vi.stubGlobal("fetch", (_u: string, init?: RequestInit) =>
      (init?.method ?? "GET") === "GET"
        ? reply(200, [BUILTIN])
        : reply(409, { detail: "You already have a transformation called “Glossary”. Pick another name, or change it." }),
    );
    render(<TransformationsPanel />);
    await screen.findByText("Summary");
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: "Glossary" } });
    fireEvent.change(screen.getByLabelText(/Instruction/), { target: { value: "Define." } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect((await screen.findByRole("alert")).textContent).toContain("You already have a transformation called");
  });

  it("says why the list could not be loaded and tries again", async () => {
    vi.stubGlobal("fetch", () => reply(500, { detail: "The studio is down." }));
    render(<TransformationsPanel />);
    expect((await screen.findByRole("alert")).textContent).toContain("Transformations could not be loaded");
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });
});
