// A thin bar along the top of the page while the studio is waiting on the
// server. Every screen already says what it is doing where it can; this is
// for everything that does not, so that no click is ever met with nothing.

import { useEffect, useState } from "react";
import { PENDING } from "./api";
import { useStore } from "./store";

/** How long a request may take before the bar shows. Most are answered
 * sooner, and a bar that flashed for each would be noise. */
export const ACTIVITY_DELAY_MS = 500;

export function ActivityBar() {
  const pending = useStore(PENDING) > 0;
  const [shown, setShown] = useState(false);
  useEffect(() => {
    if (!pending) {
      // Hidden when the last request ends. A state set in a timer's callback
      // is not one set in the effect itself.
      const t = setTimeout(() => setShown(false), 0);
      return () => clearTimeout(t);
    }
    const t = setTimeout(() => setShown(true), ACTIVITY_DELAY_MS);
    return () => clearTimeout(t);
  }, [pending]);
  if (!shown) return null;
  return (
    <div className="activity" role="progressbar" aria-label="Waiting for the studio" aria-busy="true">
      <span className="activity-run" />
    </div>
  );
}
