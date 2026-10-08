// The Transcript tab: the narration, following the voice.

import { useEffect, useRef, useState } from "react";
import { Icon } from "../Icon";
import type { ScriptLine } from "./api";
import { clock, onAt } from "./time";

/** The narration, line by line, the line being said in view; a line plays
 * from there. Scrolling away stops the following until "Back to now". */
export function Transcript({ lines, t, seek }: { lines: ScriptLine[]; t: number; seek: (ms: number) => void }) {
  const on = onAt(lines, t);
  const [follow, setFollow] = useState(true);
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!follow || on < 0) return;
    boxRef.current?.querySelector(`[data-i="${on}"]`)?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }, [on, follow]);
  return (
    <div className="w-pane">
      <div ref={boxRef} className="w-lines" onWheel={() => setFollow(false)} onTouchMove={() => setFollow(false)}>
        {lines.map((ln, i) => (
          <button key={i} data-i={i} className={i === on ? "w-line on" : i < on ? "w-line seen" : "w-line"}
            onClick={() => {
              setFollow(true);
              seek(ln.start_ms);
            }}>
            <span className="w-line-t">{clock(ln.start_ms)}</span>
            <span>{ln.text}</span>
          </button>
        ))}
      </div>
      {!follow && (
        <button className="w-follow" onClick={() => setFollow(true)}>
          <Icon name="arrow-down" />
          Back to now
        </button>
      )}
    </div>
  );
}
