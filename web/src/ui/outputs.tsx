// Everything made from a collection, as the rows of its outputs list: a deck,
// an audio overview, a map or a set of notes, each saying where it is. A port
// of the old app's `outputs.rs`.

import { useState } from "react";
import { errText, type OutputProgress, type SessionSummary } from "./api";
import {
  mindmapDelete,
  mindmapRetitle,
  notesDelete,
  notesRetitle,
  playerUrl,
  retryOutput,
  sessionDelete,
  sessionRetitle,
  type MindMapSummary,
  type StudyNotesSummary,
} from "./api-studio";
import { CardMenu } from "./CardMenu";
import { RowErr } from "./common";
import { askConfirm, askPrompt, usd } from "./dialogs";
import { Icon } from "./Icon";
import { mmss, when } from "./shell";

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

/** A rename or delete on its way, where the row's ⋯ menu was. */
function RowBusy({ text }: { text: string }) {
  return <span className="mini-spin" role="status" title={text} aria-label={text} />;
}

/** A map or a set of notes in the outputs list: opens beside the Studio.
 *
 * Read only, with no rename or delete, it is a shared output on a shared
 * collection's page; with `href` it is a link (a shared deck or audio
 * overview, to the player) rather than a button. */
export function ItemRow({
  icon,
  what,
  title,
  shown,
  facts,
  whenMs,
  on = false,
  href,
  onOpen,
  onRename,
  onDelete,
  busy = "",
  err = "",
  onDismissErr,
}: {
  icon: string;
  /** What it is, for the delete question: "mind map", "study notes". */
  what: string;
  /** Its name as stored, where a rename starts from; may be empty. */
  title: string;
  /** Its name as the server says to show it: "Untitled mind map" for none. */
  shown: string;
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
  /** A rename or delete of it on its way, in words; empty when none is. */
  busy?: string;
  /** Why the last rename or delete of it failed; empty when nothing did. */
  err?: string;
  onDismissErr?: () => void;
}) {
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
        {busy !== "" ? (
          <RowBusy text={busy} />
        ) : (
          onRename &&
          onDelete && (
            <OutputMenu
              title={title}
              shown={shown}
              consequence={removedLine(what)}
              onRename={onRename}
              onDelete={onDelete}
            />
          )
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
  at = null,
  onChanged,
  onRename,
  onDelete,
  busy = "",
  err = "",
  onDismissErr,
}: {
  s: SessionSummary;
  /** How far its prep is, from the collection's event stream; null until it
   * says. */
  at?: OutputProgress | null;
  onChanged: () => void;
  /** Without these the row has no menu: an output of a read-only copy. */
  onRename?: (next: string) => void;
  onDelete?: () => void;
  /** A rename or delete of it on its way, in words; empty when none is. */
  busy?: string;
  /** Why the last rename or delete of it failed; empty when nothing did. */
  err?: string;
  onDismissErr?: () => void;
}) {
  const audio = s.kind === "audio";
  const icon = audio ? "soundwave" : "easel";
  const shown = s.display_title;
  const state = s.state;
  const [retrying, setRetrying] = useState(false);
  // Why the last Retry could not start, said under the row it is about.
  const [retryErr, setRetryErr] = useState("");
  // A failed prep's reason and its technical detail, both as the server says.
  const why = state === "failed" ? [s.failure, s.failure_detail] : null;
  const w = when(s.created_ms);

  const retry = () => {
    if (retrying) return;
    setRetrying(true);
    setRetryErr("");
    retryOutput(s.sid)
      .catch((e) => setRetryErr(`It could not be started again. ${errText(e)}`))
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
          <Progress at={at} waiting={s.waiting} />
        ) : state === "failed" ? (
          <span className="out-d bad">
            <span className="badge failed">Failed</span> {why?.[0] ?? ""}
          </span>
        ) : (
          <span className="out-d num">
            {audio ? (
              <>
                {s.audio_label}
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
        {busy !== "" ? (
          <RowBusy text={busy} />
        ) : (
          onRename &&
          onDelete && (
            <OutputMenu
              title={s.title}
              shown={shown}
              consequence="It is removed from this collection. The sources and everything else made from them stay."
              onRename={onRename}
              onDelete={onDelete}
            />
          )
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

/** A prep's progress: the step it is on and how far, as the collection's
 * event stream says it (`at`), null until it has. Until a step has started,
 * why the build has not started yet, when the server says one is waiting. */
export function Progress({ at, waiting = "" }: { at: OutputProgress | null; waiting?: string }) {
  const step = at?.step ?? "";
  const done = at?.steps_done ?? 0;
  const total = at?.steps_total ?? 0;
  const label = at?.label || "Starting";
  const pct = total > 0 ? Math.floor((done * 100) / total) : 0;
  return (
    <>
      <span className="out-d num">
        <span className="badge preparing">Preparing</span>
        {` ${label}`}
        {total > 0 && ` · ${done}/${total}`}
      </span>
      {waiting !== "" && step === "" && <span className="out-d">{waiting}</span>}
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
