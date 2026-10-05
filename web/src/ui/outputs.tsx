// Everything made from a collection, as the rows of its outputs list: a deck,
// an audio overview, a map or a set of notes, each saying where it is. A port
// of the old app's `outputs.rs`.

import { useEffect, useState } from "react";
import { errText, type SessionSummary } from "./api";
import {
  buildOutput,
  getSession,
  mindmapDelete,
  mindmapRetitle,
  notesDelete,
  notesRetitle,
  playerUrl,
  sessionDelete,
  sessionEventsUrl,
  sessionRetitle,
  type BuildReq,
  type MindMapSummary,
  type StudyNotesSummary,
} from "./api-studio";
import { CardMenu } from "./CardMenu";
import { askConfirm, askPrompt, usd } from "./dialogs";
import { known } from "./errors";
import { collTitle } from "./home";
import { Icon } from "./Icon";
import { mmss, report, when } from "./shell";

/** An audio overview's format, as the person reads it. */
export function audioFormatLabel(id: string): string {
  return { brief: "Brief", critique: "Critique", debate: "Debate" }[id] ?? "Deep Dive";
}

/** Start a failed output again, as a new output in the same collection.
 *
 * The new one is started FIRST, and the failed row is deleted only once that
 * has been accepted. The other way round (delete, then start) lost the failed
 * row, its reason included, every time the second call was refused: no
 * sources any more, over the cost limit, the studio down.
 *
 * What is kept: the title, the collection, the style, the audio format,
 * length and focus, the slide count, and the output's OWN number of voices,
 * all read back from it. Voices are deliberately not re-read from settings —
 * somebody editing their voices between the failure and the retry should not
 * silently change what this output sounds like. Whatever the output does not
 * record (an older one has no style; a prep that failed before its outline
 * has no slides) is left out, and the server fills it from the settings as it
 * does for a new build.
 *
 * Resolves to a note when the retry started but left the failed row behind. */
export async function retryPrep(sid: string, title: string): Promise<string | null> {
  const old = await getSession(sid);
  const cid = old.collection;
  const a = old.audio;
  const style = old.style && old.style.trim() !== "" ? old.style : null;
  const req: BuildReq = a
    ? {
        kind: "audio",
        title,
        speakers: old.speakers || null,
        audio_format: a.format as BuildReq["audio_format"],
        audio_length: a.length as BuildReq["audio_length"],
        focus: a.focus,
      }
    : {
        kind: "slides",
        title,
        speakers: old.speakers || null,
        slide_count: old.slides > 0 ? old.slides : null,
        style,
      };
  await buildOutput(cid, req);
  // The new one is on its way; the failed one can go.
  try {
    await sessionDelete(sid);
    return null;
  } catch (e) {
    return `It started again, but the failed one could not be removed. ${errText(e)}`;
  }
}

/** The phases a prep runs, in order, named for a person. */
export const PHASES: [string, string][] = [
  ["research", "Researching the web"],
  ["ingest", "Reading your sources"],
  ["script", "Writing the script"],
  ["deck", "Recording the voices, and drawing any slides"],
  ["validate", "Checking it renders"],
];

/** What a failed prep says to the person who started it, and what it keeps back.
 *
 * Returns [plain, detail]. `plain` is the only thing shown by default: one
 * sentence, no ids, no internal nouns, and where possible the action that
 * fixes it. `detail` is the prep's own words, kept behind a disclosure for
 * whoever is debugging.
 *
 * The split exists because the unsplit version shipped and was wrong: a
 * person who asked for a slide deck was shown four internal identifiers, an
 * instruction naming two calls they cannot make, and no mention of the
 * actual cause, which was an exhausted billing account. */
export function prepFailureText(raw: string): [string, string] {
  const r = raw.trim();
  if (r === "") return ["Something went wrong while making this. Your sources are kept.", ""];
  const low = r.toLowerCase();
  // The new server writes the reason as a sentence a person can act on; it is
  // the message itself, not a detail to hide behind a disclosure.
  if (/^[A-Z][^{}<>]*[.!?]$/.test(r) && !/\bHTTP \d{3}\b/.test(r)) return [r, ""];
  // The AI provider's failures read the same here as everywhere else.
  let plain = known(r);
  if (plain === null) {
    if (low.includes("name conflict") && low.includes("theme"))
      plain = "That visual style could not be applied. Pick a different style and try again.";
    else if (low.includes("no extracted pairs") || low.includes("q&a door is empty") || low.includes("not a bot"))
      plain =
        "Your sources could not be read. A link behind a sign-in or a bot check saves " +
        "the warning page instead of the document, so try a direct link or paste the " +
        "text in.";
    else if (low.includes("timed out") || low.includes("timeout"))
      plain =
        "The slides took too long to draw and this was stopped. Trying again " + "with fewer slides usually works.";
    else if (low.includes("rendered") || low.includes("nothing rendered"))
      plain =
        "The slides could not be drawn, so there is no deck to narrate. Your sources " +
        "are kept: try again, or change them.";
    else
      plain =
        "Something went wrong while making this. Your sources are kept, so you " + "can try again or change them.";
  }
  return [plain, r];
}

/** One thing made from the collection, for the outputs list: every kind in one
 * list, newest first. */
export type Made =
  | { kind: "session"; s: SessionSummary }
  | { kind: "map"; m: MindMapSummary }
  | { kind: "notes"; n: StudyNotesSummary };

export function madeCreated(m: Made): number {
  return m.kind === "session" ? m.s.created_ms : m.kind === "map" ? m.m.created_ms : m.n.created_ms;
}

export function madeKey(m: Made): string {
  return m.kind === "session" ? `s-${m.s.sid}` : m.kind === "map" ? `m-${m.m.id}` : `n-${m.n.id}`;
}

/** One output, by its kind and its id: what a rename or a delete acts on. */
export type Target = { kind: "session" | "map" | "notes"; id: string };

/** What it is, at the start of a sentence saying what went wrong with it. */
export function subject(t: Target): string {
  return t.kind === "session" ? "It" : t.kind === "map" ? "The mind map" : "The study notes";
}

/** Rename one output of collection `cid`. Throws why it was not renamed,
 * including one that is no longer there. */
export async function retitle(cid: string, t: Target, title: string): Promise<void> {
  if (t.kind === "session") await sessionRetitle(t.id, title);
  else if (t.kind === "map") await mindmapRetitle(cid, t.id, title);
  else await notesRetitle(cid, t.id, title);
}

/** Delete one output of collection `cid`. Throws why it was not. */
export async function deleteOutput(cid: string, t: Target): Promise<void> {
  if (t.kind === "session") await sessionDelete(t.id);
  else if (t.kind === "map") await mindmapDelete(cid, t.id);
  else await notesDelete(cid, t.id);
}

/** Something being made that answers in seconds: a map, a set of notes. */
export function PendingRow({ icon, text }: { icon: string; text: string }) {
  return (
    <div className="out-row run">
      <span className="out-i">
        <Icon name={icon} />
      </span>
      <div className="out-main">
        <div className="out-t">{text}</div>
        <div className="pbar wide">
          <div className="pbar-run" />
        </div>
      </div>
    </div>
  );
}

/** An output's ⋯ menu: rename it, or delete it after asking. One for every
 * row of the outputs list, so all of them are acted on the same way. */
export function OutputMenu({
  title,
  shown,
  consequence,
  onRename,
  onDelete,
}: {
  /** Its name as stored, where a rename starts from; may be empty. */
  title: string;
  /** Its name as the row shows it, for the menu's label and the question. */
  shown: string;
  /** What a delete removes and what it keeps, said in the question. */
  consequence: string;
  /** The new name, once the person has given one that differs. */
  onRename: (next: string) => void;
  onDelete: () => void;
}) {
  return (
    <CardMenu
      label={shown}
      items={[
        ["Rename", false],
        ["Delete", true],
      ]}
      onPick={(i) => {
        if (i === 0) {
          const was = title.trim();
          askPrompt("Rename", title, "Save", (v) => {
            const next = v.trim();
            if (next !== "" && next !== was) onRename(next);
          });
        } else {
          askConfirm(`Delete “${shown}”?`, consequence, "Delete", () => onDelete());
        }
      }}
    />
  );
}

/** A failure in the outputs list that is not one output's own: a map or notes
 * that could not be made, a list that could not be read. */
export function ErrRow({
  icon = "",
  title,
  detail = "",
  onDismiss,
  onRetry,
}: {
  icon?: string;
  title: string;
  detail?: string;
  onDismiss?: () => void;
  onRetry?: () => void;
}) {
  return (
    <div className="out-row bad" role="alert">
      {icon !== "" && (
        <span className="out-i">
          <Icon name={icon} />
        </span>
      )}
      <div className="out-main">
        <div className="out-t">{title}</div>
        {detail !== "" && <div className="out-d">{detail}</div>}
      </div>
      {onRetry && (
        <button onClick={onRetry}>
          <Icon name="arrow-clockwise" />
          Try again
        </button>
      )}
      {onDismiss && (
        <button className="icon-btn" title="Dismiss" aria-label="Dismiss" onClick={onDismiss}>
          <Icon name="x-lg" />
        </button>
      )}
    </div>
  );
}

/** What deleting a map or notes does, the verb agreeing with the kind: "The
 * mind map is removed", "The study notes are removed". */
export function removedLine(what: string): string {
  const verb = what.endsWith("notes") ? "are" : "is";
  return `The ${what} ${verb} removed. The sources and everything else made from them stay.`;
}

/** A map or a set of notes in the outputs list: opens beside the Studio.
 *
 * Read only, with no rename or delete, it is a shared output on a shared
 * collection's page; with `href` it is a link (a shared deck or audio
 * overview, to the player) rather than a button. */
/** What went wrong with one row's action (a retry, a rename, a delete), said
 * right under that row rather than in a banner away from it. */
export function RowErr({ text, onDismiss }: { text: string; onDismiss: () => void }) {
  return (
    <div className="opt-err row-err" role="alert">
      <span className="grow">{text}</span>
      <button className="icon-btn" title="Dismiss" aria-label="Dismiss" onClick={onDismiss}>
        <Icon name="x-lg" />
      </button>
    </div>
  );
}

export function ItemRow({
  icon,
  what,
  title,
  facts,
  whenMs,
  on = false,
  href,
  onOpen,
  onRename,
  onDelete,
  err = "",
  onDismissErr,
}: {
  icon: string;
  /** What it is, for the delete question: "mind map", "study notes". */
  what: string;
  title: string;
  facts: string;
  whenMs: number;
  /** Whether it is the one open in the viewer. */
  on?: boolean;
  /** Where it opens, when that is another page: the player. */
  href?: string;
  onOpen?: () => void;
  /** The new name, once the person has given one that differs. */
  onRename?: (next: string) => void;
  onDelete?: () => void;
  /** Why the last rename or delete of it failed; empty when nothing did. */
  err?: string;
  onDismissErr?: () => void;
}) {
  const shown = title.trim() === "" ? `Untitled ${what}` : title;
  const w = when(whenMs);
  const body = (
    <>
      <span className="out-i">
        <Icon name={icon} />
      </span>
      <span className="out-main">
        <span className="out-t">{shown}</span>
        <span className="out-d num">
          {facts}
          {w !== "" && ` · ${w}`}
        </span>
      </span>
    </>
  );
  return (
    <>
      <div className={on ? "out-row on" : "out-row"}>
        {href !== undefined ? (
          <a className="out-open" href={href} title="Play">
            {body}
            <span className="out-play">
              <Icon name="play-fill" />
            </span>
          </a>
        ) : (
          <button
            className="out-open"
            aria-pressed={onOpen ? on : undefined}
            onClick={() => onOpen?.()}
          >
            {body}
          </button>
        )}
        {onRename && onDelete && (
          <OutputMenu
            title={title}
            shown={shown}
            consequence={removedLine(what)}
            onRename={onRename}
            onDelete={onDelete}
          />
        )}
      </div>
      {err !== "" && <RowErr text={err} onDismiss={() => onDismissErr?.()} />}
    </>
  );
}

/** A deck or an audio overview in the outputs list. Ready, it opens in the
 * player; preparing, it follows the prep's steps live; failed, it says why
 * and offers the way out. */
export function SessionRow({
  s,
  live,
  onChanged,
  onRename,
  onDelete,
  err = "",
  onDismissErr,
}: {
  s: SessionSummary;
  /** Whether this row follows its prep's event stream; see `LIVE_MAX`. */
  live: boolean;
  onChanged: () => void;
  /** Without these the row has no menu: an output of a read-only copy. */
  onRename?: (next: string) => void;
  onDelete?: () => void;
  /** Why the last rename or delete of it failed; empty when nothing did. */
  err?: string;
  onDismissErr?: () => void;
}) {
  const audio = s.kind === "audio";
  const icon = audio ? "soundwave" : "easel";
  const shown = s.title.trim() === "" ? collTitle("") : s.title;
  const state = s.state;
  const [retrying, setRetrying] = useState(false);
  // Why the last Retry could not start, said under the row it is about.
  const [retryErr, setRetryErr] = useState("");
  // A failed prep's reason, from the output itself.
  const why = state === "failed" ? prepFailureText(s.failure) : null;
  const w = when(s.created_ms);

  const retry = () => {
    if (retrying) return;
    setRetrying(true);
    setRetryErr("");
    retryPrep(s.sid, s.title)
      .then(
        (note) => note && report(note),
        (e) => setRetryErr(`It could not be started again. ${errText(e)}`),
      )
      .finally(() => {
        setRetrying(false);
        onChanged();
      });
  };

  const body = (
    <>
      <span className="out-i">
        <Icon name={icon} />
      </span>
      <span className="out-main">
        <span className="out-t">{shown}</span>
        {state === "preparing" ? (
          // Keyed apart, so a row that becomes live opens its stream.
          live ? (
            <Progress key={`live-${s.sid}`} sid={s.sid} waiting={s.waiting} />
          ) : (
            <Progress key={`idle-${s.sid}`} sid="" waiting={s.waiting} />
          )
        ) : state === "failed" ? (
          <span className="out-d bad">
            <span className="badge failed">Failed</span> {why?.[0] ?? ""}
          </span>
        ) : (
          <span className="out-d num">
            {audio ? (
              <>
                {audioFormatLabel(s.audio_format)}
                {s.duration_ms > 0 && ` · ${mmss(s.duration_ms)}`}
              </>
            ) : (
              <>
                {`${s.slide_count} slides · `}
                {s.speakers > 1 ? `${s.speakers} voices` : "1 voice"}
                {s.duration_ms > 0 && ` · ${mmss(s.duration_ms)}`}
              </>
            )}
            {/* What its making cost, when the prep recorded it; older
                outputs never did, and say nothing. */}
            {s.spent_known && (
              <>
                {" · "}
                <span title="What the model calls for this build cost, indexing its sources included">
                  Spent {usd(s.spent_usd)}
                </span>
              </>
            )}
            {w !== "" && ` · ${w}`}
          </span>
        )}
      </span>
    </>
  );

  return (
    <>
      <div className={state === "failed" ? "out-row bad" : state === "preparing" ? "out-row run" : "out-row"}>
        {state === "ready" ? (
          <a className="out-open" href={playerUrl(s.sid)} title={audio ? "Listen" : "Play"}>
            {body}
            <span className="out-play">
              <Icon name="play-fill" />
            </span>
          </a>
        ) : (
          <div className="out-open">{body}</div>
        )}
        {state === "failed" && (
          <button disabled={retrying} onClick={retry}>
            <Icon name="arrow-clockwise" />
            {retrying ? "Starting…" : "Retry"}
          </button>
        )}
        {onRename && onDelete && (
          <OutputMenu
            title={s.title}
            shown={shown}
            consequence="It is removed from this collection. The sources and everything else made from them stay."
            onRename={onRename}
            onDelete={onDelete}
          />
        )}
      </div>
      {retryErr !== "" && <RowErr text={retryErr} onDismiss={() => setRetryErr("")} />}
      {err !== "" && <RowErr text={err} onDismiss={() => onDismissErr?.()} />}
      {why && why[1] !== "" && (
        <details className="whydetail out-why">
          <summary>Technical details</summary>
          <pre>{why[1]}</pre>
        </details>
      )}
    </>
  );
}

/** How many preparing rows follow their prep live at once. */
export const LIVE_MAX = 2;

/** A prep's progress: the step it is on and how far, from the studio's event
 * stream for that output. The stream belongs to the row and closes with it,
 * so a list of preparing outputs does not leave connections behind. An empty
 * `sid` follows nothing and shows only that it is preparing. */
export function Progress({ sid, waiting = "" }: { sid: string; waiting?: string }) {
  const [at, setAt] = useState<[string, number, number]>(["", 0, 0]);
  // Why the build has not started (no worker yet), from the list and then the
  // stream; cleared the moment it starts.
  const [held, setHeld] = useState(waiting);
  useEffect(() => {
    if (sid === "") return;
    let es: EventSource;
    try {
      es = new EventSource(sessionEventsUrl(sid));
    } catch {
      return;
    }
    const onProg = (e: MessageEvent) => {
      let v: Record<string, unknown>;
      try {
        v = JSON.parse(String(e.data)) as Record<string, unknown>;
      } catch {
        return;
      }
      if (typeof v.step !== "string" && typeof v.steps_total !== "number") return;
      const n = (x: unknown) => (typeof x === "number" && x > 0 ? Math.floor(x) : 0);
      setAt([typeof v.step === "string" ? v.step : "", n(v.steps_done), n(v.steps_total)]);
    };
    // The old stream named its event `prep.progress`; the new one `progress`.
    es.addEventListener("prep.progress", onProg);
    es.addEventListener("progress", onProg);
    es.addEventListener("message", onProg);
    es.addEventListener("prep.waiting", (e: MessageEvent) => {
      try {
        const v = JSON.parse(String(e.data)) as { waiting?: unknown } | string | null;
        const why = typeof v === "string" ? v : v && typeof v.waiting === "string" ? v.waiting : "";
        setHeld(why);
      } catch {
        setHeld("");
      }
    });
    return () => es.close();
  }, [sid]);
  const [step, done, total] = at;
  const label = PHASES.find(([k]) => k === step)?.[1] ?? "Starting";
  const pct = total > 0 ? Math.floor((done * 100) / total) : 0;
  return (
    <>
      <span className="out-d num">
        <span className="badge preparing">Preparing</span>
        {sid !== "" && ` ${label}`}
        {total > 0 && ` · ${done}/${total}`}
      </span>
      {held && step === "" && <span className="out-d">{held}</span>}
      {/* Indeterminate until a step count is known: no value to announce. */}
      <span
        className="pbar wide"
        role="progressbar"
        aria-label="Progress"
        aria-valuenow={total > 0 ? pct : undefined}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        {total > 0 ? <span className="pfill" style={{ width: `${pct}%` }} /> : <span className="pbar-run" />}
      </span>
    </>
  );
}
