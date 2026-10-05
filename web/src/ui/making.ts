// Mind maps and study notes are made by the server's worker: asking answers
// at once, the row is listed as "making" until its job has made it, and one
// whose job fails is removed, the job saying why. This is how a page follows
// those jobs, whether it asked for them or found them being made when it
// opened (another tab, a reload), so "Making…" always says what the server
// says.

import { followJob, type MadeState } from "./api-studio";

/** How often a job being made is asked about. */
export const FOLLOW_MS = 1000;

/** Something the worker makes. */
export type Makeable = { id: string; state: MadeState; job_id: string | null };

/** What a page keeps while things are made: how many of its own asks are on
 * their way, and which jobs it already follows. */
export type MakingState = { asked: number; following: Set<string> };

export function newMaking(): MakingState {
  return { asked: 0, following: new Set() };
}

/** The ready ones of a list, and whether any is still being made. Each job
 * being made that this page does not follow yet is followed; once it ends,
 * `ended` is told why it failed, or null when it made what it was making. */
export function sorted<T extends Makeable>(
  list: T[],
  m: MakingState,
  ended: (why: string | null) => void,
  signal?: AbortSignal,
): { ready: T[]; making: boolean } {
  for (const x of list) {
    const id = x.job_id;
    if (x.state !== "making" || id === null || m.following.has(id)) continue;
    m.following.add(id);
    void followJob(id, () => {}, signal, FOLLOW_MS).then((why) => {
      m.following.delete(id);
      if (!signal?.aborted) ended(why);
    });
  }
  return {
    ready: list.filter((x) => x.state !== "making"),
    making: m.asked > 0 || list.some((x) => x.state === "making"),
  };
}

/** Ask for one and wait until the worker has made it. Its id, or the
 * sentence that says why it was not made. */
export async function madeBy(
  m: MakingState,
  ask: () => Promise<{ job: { id: string }; made: { id: string } }>,
): Promise<{ id: string } | { why: string }> {
  const { job, made } = await ask();
  m.following.add(job.id);
  try {
    const why = await followJob(job.id, () => {}, undefined, FOLLOW_MS);
    return why === null ? { id: made.id } : { why };
  } finally {
    m.following.delete(job.id);
  }
}
