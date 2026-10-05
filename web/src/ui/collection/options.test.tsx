import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { StudioOptions } from "../api-studio";
import type { PageActions } from "./actions";
import { applyDefaults, fitLength, pageState } from "./state";
import { Studio } from "./Studio";

// The Create panel's options as the server sends them.
const opts: StudioOptions = {
  styles: [
    { id: "editorial", label: "Editorial", blurb: "Magazine type, thin rules", thumbnail: "assets/styles/editorial.jpg" },
    { id: "clay", label: "Clay", blurb: "Soft plasticine shapes", thumbnail: "assets/styles/clay.jpg" },
  ],
  audio_formats: [
    {
      id: "deep_dive",
      label: "Deep Dive",
      blurb: "Two hosts unpack your sources",
      lengths: [
        { id: "shorter", label: "Shorter" },
        { id: "default", label: "Default" },
        { id: "longer", label: "Longer" },
      ],
      voices: "Voices: Ava and Andrew.",
    },
    { id: "brief", label: "Brief", blurb: "One host, two minutes", lengths: [], voices: "Brief: Ava alone, about 2 minutes." },
  ],
  default_style: "clay",
  default_audio_format: "deep_dive",
  default_audio_length: "longer",
  deck_summary: "8 slides · about 5 min · Ava and Andrew",
  language_note: "Writing in French.",
  build_language_note: "Writing in French · voices are English.",
  show_cost: false,
  research: { label: "Quick research", takes: "about a minute" },
  upload: {
    extensions: ["pdf"],
    accept: ".pdf",
    kinds: "PDF",
    max_mb: 25,
    max_files: 10,
    max_links: 8,
    hint: "PDF, up to 25 MB.",
    title: "PDF, up to 25 MB each.",
  },
};

const props = {
  cid: "c1",
  open: null,
  start: null,
  setCrumb: () => {},
  onOpen: () => {},
  onGone: () => {},
};

const actions = { fetchEstimate: async () => {}, generate: () => {}, loadOptions: async () => {} } as unknown as PageActions;

afterEach(cleanup);

describe("the Create panel's options, from the server", () => {
  it("starts on the server's picks until the person picks their own", () => {
    const S = pageState(props);
    S.opts.set(opts);
    applyDefaults(S);
    expect([S.style.get(), S.audioFormat.get(), S.audioLength.get()]).toEqual(["clay", "deep_dive", "longer"]);
    S.picked.set(true);
    S.opts.set({ ...opts, default_style: "editorial" });
    applyDefaults(S);
    expect(S.style.get()).toBe("clay");
  });

  it("falls back to the default length when the format does not offer the one picked", () => {
    const S = pageState(props);
    S.opts.set(opts);
    S.audioFormat.set("brief");
    S.audioLength.set("longer");
    fitLength(S);
    expect(S.audioLength.get()).toBe("default");
  });

  it("draws the styles, formats, lengths and lines the server words", () => {
    const S = pageState({ ...props, start: "session" });
    S.opts.set(opts);
    applyDefaults(S);
    S.srcs.set([{ icon: "", name: "a", detail: "", ok: true, url: "", file: "a.md" }]);
    S.srcsLoaded.set(true);
    const { container } = render(<Studio S={S} A={actions} onOpen={() => {}} />);
    expect([...container.querySelectorAll(".style-n")].map((n) => n.textContent)).toEqual(["Editorial", "Clay"]);
    expect(container.querySelector(".style.on .style-n")?.textContent).toBe("Clay");
    expect(container.querySelector(".sw")?.getAttribute("style")).toContain("/ui/assets/styles/editorial.jpg");
    expect(container.textContent).toContain("8 slides · about 5 min · Ava and Andrew · ");
    expect(container.textContent).toContain("Writing in French · voices are English.");
    // Costs off in the settings: no banner.
    expect(screen.queryByRole("status", { name: "Estimated cost" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Audio Overview/i }));
    expect([...container.querySelectorAll(".ao-n")].map((n) => n.textContent)).toEqual(["Deep Dive", "Brief"]);
    expect([...container.querySelectorAll(".ao-len .chip")].map((n) => n.textContent)).toEqual([
      "Shorter",
      "Default",
      "Longer",
    ]);
    expect(container.textContent).toContain("Voices: Ava and Andrew.");
    fireEvent.click(screen.getByRole("radio", { name: /Brief/ }));
    expect(container.querySelector(".ao-len")).toBeNull();
    expect(S.audioLength.get()).toBe("default");
    expect(container.textContent).toContain("Brief: Ava alone, about 2 minutes.");
  });
});
