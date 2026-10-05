// Every error a person sees comes worded by the server, which sends it as
// `{"detail": …}`, and `api.ts` shows it as it is. What is left for this file
// is the one failure the server cannot word because it was never reached: the
// network, or a proxy in front of it answering in its place.

/** The studio was not reached at all. */
export const UNREACHABLE =
  "The studio cannot be reached. Check your connection and that the studio is running, then try again.";

/** What a refusal says when it came without the server's own sentence: a
 * proxy in front of the studio answered it, or the answer was cut off. */
export function unworded(status: number): string {
  if (status === 413) return "The file is larger than the studio takes. Split it, or upload the part you need.";
  // A gateway answering for a studio that is down or restarting.
  if (status === 502 || status === 503 || status === 504) return UNREACHABLE;
  return "The studio did not say why that failed. Reload the page and try again.";
}

/** Anything thrown that is not an Error, as a person reads it. */
export const UNKNOWN = "Something went wrong. Reload the page and try again.";
