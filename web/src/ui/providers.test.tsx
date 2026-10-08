import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SetupGate } from "./providers";

function reply(status: number, body: unknown) {
  return Promise.resolve(
    new Response(status === 204 ? null : JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

const PRESETS = [
  {
    kind: "openrouter",
    label: "OpenRouter",
    blurb: "One key for hundreds of models.",
    base_url: "https://openrouter.ai/api/v1",
    needs_key: true,
    key_page: "openrouter.ai/settings/keys",
    key_env: "OPENROUTER_API_KEY",
    roles: ["text"],
  },
  {
    kind: "ollama",
    label: "Ollama",
    blurb: "Models running on your own machine.",
    base_url: "http://localhost:11434/v1",
    needs_key: false,
    key_page: "",
    key_env: "OLLAMA_API_KEY",
    roles: [],
  },
];

const ADDED = {
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
    sentence: "Connected to OpenRouter: $4.13 left on this key.",
    balance: "$4.13 left on this key",
    models: 300,
    checked_at: "t",
  },
};

const ROLES = [
  { role: "chat", label: "Chat and Ask", keys: ["OPENNOTEBOOK_CHAT_MODEL"], model: "google/gemini-2.5-flash-lite", model_name: "Gemini 2.5 Flash Lite", chosen: false },
  { role: "image", label: "Pictures", keys: ["OPENNOTEBOOK_VIDEO_IMAGE_MODEL"], model: "", model_name: "", chosen: false },
];

type Route = (method: string, path: string, body: unknown) => Promise<Response> | undefined;

function serve(route: Route) {
  const fetch = vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url, "http://x").pathname.replace(/^\/api/, "");
    const method = init?.method ?? "GET";
    const body: unknown = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    return route(method, path, body) ?? reply(404, { detail: `no route for ${method} ${path}` });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the setup gate", () => {
  it("shows the studio at once when a provider is connected", async () => {
    serve((_m, p) => (p === "/setup" ? reply(200, { ready: true, can_setup: true, providers: [], message: "" }) : undefined));
    render(
      <SetupGate>
        <div>the studio</div>
      </SetupGate>,
    );
    expect(await screen.findByText("the studio")).toBeTruthy();
  });

  it("holds the studio back for the tour, and refuses a bad key with its reason", async () => {
    const asked: unknown[] = [];
    serve((m, p, body) => {
      if (p === "/setup") return reply(200, { ready: false, can_setup: true, providers: [], message: "Connect" });
      if (p === "/ai/providers" && m === "GET") return reply(200, { presets: PRESETS, providers: [], roles: [] });
      if (p === "/ai/providers" && m === "POST") {
        asked.push(body);
        return reply(422, { detail: "OpenRouter refused this key. Check that you copied all of it." });
      }
      return undefined;
    });
    render(
      <SetupGate>
        <div>the studio</div>
      </SetupGate>,
    );
    expect(await screen.findByText("Welcome to OpenNotebook")).toBeTruthy();
    expect(screen.queryByText("the studio")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Connect a provider/ }));
    const key = await screen.findByPlaceholderText("Paste your key");
    const go = screen.getByText("Test and connect").closest("button")!;
    // Nothing to test until there is a key.
    expect(go.disabled).toBe(true);
    fireEvent.change(key, { target: { value: "sk-or-bad" } });
    fireEvent.click(go);
    expect((await screen.findByRole("alert")).textContent).toBe(
      "OpenRouter refused this key. Check that you copied all of it.",
    );
    expect(asked).toEqual([{ kind: "openrouter", key: "sk-or-bad", base_url: "" }]);
    expect(screen.queryByText("the studio")).toBeNull();
  });

  it("goes on to the models once a key works, and opens the studio after", async () => {
    serve((m, p) => {
      if (p === "/setup") return reply(200, { ready: false, can_setup: true, providers: [], message: "Connect" });
      if (p === "/ai/providers" && m === "GET") return reply(200, { presets: PRESETS, providers: [ADDED], roles: ROLES });
      if (p === "/ai/providers" && m === "POST") return reply(201, ADDED);
      if (p === "/ai/providers/openrouter/models") return reply(200, { models: ["google/gemini-2.5-flash-lite"] });
      if (p === "/settings") return reply(200, { tabs: [], settings: [] });
      return undefined;
    });
    render(
      <SetupGate>
        <div>the studio</div>
      </SetupGate>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Connect a provider/ }));
    fireEvent.change(await screen.findByPlaceholderText("Paste your key"), { target: { value: "sk-or-good-1234" } });
    fireEvent.click(screen.getByText("Test and connect"));
    expect(await screen.findByText("Choose models")).toBeTruthy();
    expect(screen.getByText(/Connected to OpenRouter: \$4\.13 left on this key\./)).toBeTruthy();
    expect(await screen.findByText("Gemini 2.5 Flash Lite, suggested by your provider.")).toBeTruthy();
    expect(screen.getByText(/No connected provider can do this yet/)).toBeTruthy();
    fireEvent.click(screen.getByText("Open the studio"));
    expect(await screen.findByText("the studio")).toBeTruthy();
  });

  it("asks for an address, not a key, for a local server", async () => {
    serve((_m, p) => {
      if (p === "/setup") return reply(200, { ready: false, can_setup: true, providers: [], message: "Connect" });
      if (p === "/ai/providers") return reply(200, { presets: PRESETS, providers: [], roles: [] });
      return undefined;
    });
    render(
      <SetupGate>
        <div>the studio</div>
      </SetupGate>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Connect a provider/ }));
    fireEvent.click(await screen.findByText("Ollama"));
    expect(screen.queryByPlaceholderText("Paste your key")).toBeNull();
    expect((screen.getByPlaceholderText("http://localhost:1234/v1") as HTMLInputElement).value).toBe(
      "http://localhost:11434/v1",
    );
    expect(screen.getByText("Test and connect").closest("button")!.disabled).toBe(false);
  });

  it("tells someone who cannot set it up whom to ask", async () => {
    serve((_m, p) =>
      p === "/setup"
        ? reply(200, {
            ready: false,
            can_setup: false,
            providers: [],
            message: "Only whoever runs this studio can connect AI providers.",
          })
        : undefined,
    );
    render(
      <SetupGate>
        <div>the studio</div>
      </SetupGate>,
    );
    expect(await screen.findByText("This studio is not set up yet")).toBeTruthy();
    expect(screen.getByText("Only whoever runs this studio can connect AI providers.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Connect a provider/ })).toBeNull();
  });

  it("says so in a sentence when the studio cannot be reached", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new TypeError("Failed to fetch")));
    render(
      <SetupGate>
        <div>the studio</div>
      </SetupGate>,
    );
    expect(await screen.findByText("The studio could not be reached")).toBeTruthy();
    expect(screen.getByText("Try again")).toBeTruthy();
  });
});
