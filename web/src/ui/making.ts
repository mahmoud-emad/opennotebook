// Mind maps and study notes are made by the server's worker: asking answers
// at once, the row is listed as "making" until its job has made it, and one
// whose job fails is removed, the job saying why. The collection's event
// stream says both: the lists as they change, and each job's end with why it
// failed (`ended`). This is how a page keeps track of those jobs, whether it
// asked for them or found them being made when it opened (another tab, a
// reload), so "Making…" always says what the server says.

import { getJob, type MadeState } from "./api-studio";

/** Something the worker makes. */
export type Makeable = { id: string; state: MadeState; job_id: string | null };

/** How a job ended: null when it made what it was making, else why not. */
type Why = string | null;

/** What a page keeps while things are made: how many of its own asks are on
 * their way, the jobs it waits on, the ends heard before anyone waited, and
 * the jobs a list showed being made whose end has not been heard (a failed
 * row leaves the list before its job ends). */
export type MakingState = {
  asked: number;
  waiting: Map<string, (why: Why) => void>;
  ended: Map<string, Why>;
  listed: Set<string>;
};

/** How many ends nobody waited on are kept, the oldest forgotten first. */
const ENDED_MAX = 64;

export function newMaking(): MakingState {
  return { asked: 0, waiting: new Map(), ended: new Map(), listed: new Set() };
}

/** The ready ones of a list, and whether any is still being made. */
export function sorted<T extends Makeable>(list: T[], m: MakingState): { ready: T[]; making: boolean } {
  const making = list.flatMap((x) => (x.state === "making" && x.job_id !== null ? [x.job_id] : []));
  for (const id of making) m.listed.add(id);
  return {
    ready: list.filter((x) => x.state !== "making"),
    making: m.asked > 0 || making.length > 0,
  };
}

/** The server said a job ended. An ask of this page waiting on it is told;
 * else the end is kept for an ask still on its way. Returns why it failed
 * when no ask waited but the list showed it being made (another tab, a
 * reload), for the page to say; null otherwise. */
export function jobEnded(m: MakingState, id: string, why: Why): Why {
  const shown = m.listed.delete(id);
  const waiter = m.waiting.get(id);
  if (waiter) {
    m.waiting.delete(id);
    waiter(why);
    return null;
  }
  m.ended.set(id, why);
  for (const old of m.ended.keys()) {
    if (m.ended.size <= ENDED_MAX) break;
    m.ended.delete(old);
  }
  return shown ? why : null;
}

/** How the job `id` ends, as the stream says it. */
function endOf(m: MakingState, id: string): Promise<Why> {
  if (m.ended.has(id)) {
    const why = m.ended.get(id) ?? null;
    m.ended.delete(id);
    return Promise.resolve(why);
  }
  return new Promise((resolve) => m.waiting.set(id, resolve));
}

/** Ask the server once how each job this page waits on stands: for a stream
 * that was down while one ended, so its end was never said. */
export async function recheck(m: MakingState, signal?: AbortSignal): Promise<void> {
  await Promise.all(
    [...m.waiting.keys()].map(async (id) => {
      try {
        const j = await getJob(id, signal);
        if (j.status === "done") jobEnded(m, id, null);
        else if (j.status === "failed" || j.status === "cancelled") jobEnded(m, id, j.error ?? "");
      } catch {
        // Asked again the next time the stream is down.
      }
    }),
  );
}


/** Ask for one and wait until the worker has made it. Its id, or the
 * sentence that says why it was not made. */
export async function madeBy(
  m: MakingState,
  ask: () => Promise<{ job: { id: string }; made: { id: string } }>,
): Promise<{ id: string } | { why: string }> {
  const { job, made } = await ask();
  const why = await endOf(m, job.id);
  return why === null ? { id: made.id } : { why };
}
