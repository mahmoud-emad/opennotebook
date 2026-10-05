// The steps a deck or an audio overview is made in, by the names the server
// reports them under (`build/pipeline.py`'s PHASES), as a person reads them.
// The outputs list and the player both say them from here.

export const PHASES: [string, string][] = [
  ["research", "Researching the web"],
  ["ingest", "Reading your sources"],
  ["script", "Writing the script"],
  ["deck", "Recording the voices, and drawing any slides"],
  ["validate", "Checking it renders"],
];

/** A step as a person reads it; null for one this page does not know. */
export function phaseLabel(step: string): string | null {
  return PHASES.find(([k]) => k === step)?.[1] ?? null;
}
