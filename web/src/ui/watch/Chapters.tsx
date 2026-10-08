// The Chapters tab: the chapters and, for a whiteboard, the scenes in each.

import { Fragment } from "react";
import type { Script } from "./api";
import { clock, onAt } from "./time";

/** Each chapter with its time, and under it its scenes with up to four of
 * their labels; a click plays from there. */
export function Chapters({ chapters, scenes, t, seek }: {
  chapters: Script["chapters"];
  scenes: Script["scenes"];
  t: number;
  seek: (ms: number) => void;
}) {
  const on = onAt(chapters, t);
  const sceneOn = onAt(scenes, t);
  return (
    <div className="w-pane w-lines">
      {chapters.map((c, i) => (
        <Fragment key={`c${i}`}>
          <button className={i === on ? "w-chap on" : "w-chap"} onClick={() => seek(c.start_ms)}>
            <span className="w-line-t">{clock(c.start_ms)}</span>
            <span>{c.title}</span>
          </button>
          {scenes.map((sc, j) => {
            if (sc.start_ms < c.start_ms || sc.start_ms >= c.end_ms) return null;
            const labels = sc.labels ?? [];
            return (
              <button key={`s${j}`} className={j === sceneOn ? "w-scene on" : "w-scene"} onClick={() => seek(sc.start_ms)}>
                <span className="w-line-t">{clock(sc.start_ms)}</span>
                <span>
                  {sc.title || "Scene"}
                  {labels.length > 0 && <span className="w-scene-l">{labels.slice(0, 4).join(" · ")}</span>}
                </span>
              </button>
            );
          })}
        </Fragment>
      ))}
      {chapters.length === 0 && <p className="w-none">This video has no chapters.</p>}
    </div>
  );
}
