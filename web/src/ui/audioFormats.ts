// An audio overview's formats and lengths, as NotebookLM names them: the
// Studio's picker, the outputs list and the player all say them from here.

/** NotebookLM's four formats: id, name, what it is, and the lengths it offers
 * (none for Brief, which is always about two minutes). */
export const AUDIO_FORMATS: [string, string, string, string[]][] = [
  [
    "deep_dive",
    "Deep Dive",
    "Two hosts in a lively conversation that unpacks your sources",
    ["shorter", "default", "longer"],
  ],
  ["brief", "Brief", "One host, the key points in about two minutes", []],
  ["critique", "Critique", "An expert review of your sources, with constructive feedback", ["shorter", "default"]],
  ["debate", "Debate", "Two hosts argue different sides of what your sources raise", ["shorter", "default"]],
];

/** The lengths a format offers; none for Brief. */
export function offeredLengths(format: string): string[] {
  return AUDIO_FORMATS.find((f) => f[0] === format)?.[3] ?? [];
}

/** An audio overview's format and length as a person reads them: "Brief",
 * "Deep Dive · shorter". The default length goes unsaid. */
export function audioDesc(format: string, length: string): string {
  const name = audioFormatName(format) ?? format;
  return length !== "default" && offeredLengths(format).includes(length) ? `${name} · ${length}` : name;
}

/** A format's name, as a person reads it; null for one this page does not
 * know, so each place says its own stand-in. */
export function audioFormatName(format: string): string | null {
  return AUDIO_FORMATS.find((f) => f[0] === format)?.[1] ?? null;
}
