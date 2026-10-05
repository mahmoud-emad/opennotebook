// The sources panel: the box that adds links, text, files or a topic to
// research, and the collection's sources under it, each with where it came
// from. Files dropped anywhere on the panel are uploaded.

import { useState, type DragEvent } from "react";
import { Icon } from "../Icon";
import { SettingsLink } from "../settings";
import { SrcRow, srcKey } from "../sources";
import { useStore, useStoreSel } from "../store";
import type { PageActions } from "./actions";
import { stagedCount, type PageState } from "./state";

/** Whether a drag carries files, as opposed to text, a link or an image
 * dragged off the page. */
const draggingFiles = (e: DragEvent) => e.dataTransfer.types.includes("Files");

export function SourcesPanel({ S, A, onFold }: { S: PageState; A: PageActions; onFold: () => void }) {
  // A copy whose author did not allow edits takes nothing new.
  const ro = useStoreSel(S.summary, (s) => !!s?.read_only);
  const nSrc = useStoreSel(S.srcs, stagedCount);
  // Files dragged over the panel: the drop overlay shows while this is above
  // zero. A count, because entering a child leaves its parent.
  const [dragDepth, setDragDepth] = useState(0);
  return (
    <aside
      className="panel sources"
      aria-label="Sources"
      // Files dropped anywhere on the panel are uploaded. Only a drag that
      // carries files is answered; text dragged about is not.
      onDragEnter={(e) => {
        if (ro || !draggingFiles(e)) return;
        e.preventDefault();
        setDragDepth((d) => d + 1);
      }}
      onDragOver={(e) => {
        if (ro || !draggingFiles(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={(e) => {
        if (draggingFiles(e)) setDragDepth((d) => Math.max(d - 1, 0));
      }}
      onDrop={(e) => {
        if (ro || !draggingFiles(e)) return;
        e.preventDefault();
        setDragDepth(0);
        void A.uploadFiles([...e.dataTransfer.files]);
      }}
    >
      {dragDepth > 0 && (
        <div className="src-drop" aria-hidden="true">
          <Icon name="upload" className="xl" />
          <span>Drop files to add them</span>
        </div>
      )}
      <div className="src-head">
        <h2>
          Sources{nSrc > 0 && <span className="h-n">{` ${nSrc}`}</span>}
        </h2>
        <button
          className="icon-btn src-fold"
          title="Fold the sources away"
          aria-label="Fold the sources away"
          aria-expanded="true"
          onClick={onFold}
        >
          <Icon name="chevron-left" />
        </button>
      </div>
      {!ro && <AddBox S={S} A={A} />}
      <SourceList S={S} A={A} ro={ro} />
    </aside>
  );
}

/** The add box and its buttons. Its own component, so a keystroke in it
 * redraws only it. */
function AddBox({ S, A }: { S: PageState; A: PageActions }) {
  const draft = useStore(S.draft);
  const adding = useStore(S.adding);
  // What the server says the box takes and how long research reads; until it
  // answers, the box says nothing it cannot be sure of.
  const opts = useStore(S.opts);
  const research = opts?.research ?? null;
  const upload = opts?.upload ?? null;
  const topicTyped = draft.trim() !== "" && !draft.includes("http://") && !draft.includes("https://");
  return (
    <>
      <textarea
        aria-label="A link, some text, or a topic"
        value={draft}
        placeholder={
          upload ? `Paste up to ${upload.max_links} links, text, or a topic to research` : "Paste links, text, or a topic to research"
        }
        onChange={(e) => S.draft.set(e.target.value)}
      />
      <div className="src-actions" role="group" aria-label="Add sources">
        <button disabled={draft.trim() === "" || adding} onClick={() => void A.addSource()}>
          <Icon name="plus-lg" />
          {adding ? "Adding…" : "Add source"}
        </button>
        <button
          className="ghost"
          title={
            research
              ? `Read the web on this topic for ${research.takes} and add a written report`
              : "Read the web on this topic and add a written report"
          }
          disabled={draft.trim() === ""}
          onClick={() => void A.research()}
        >
          <Icon name="search" />
          Research a topic
        </button>
        <button
          className="src-upload"
          title={upload?.title}
          onClick={() => document.getElementById("src-files")?.click()}
        >
          <Icon name="upload" />
          Upload files
        </button>
        {upload && <p className="src-upload-d">{upload.hint}</p>}
      </div>
      {/* A topic in the box: what Research a topic will do with it. */}
      {topicTyped && research && (
        <p className="src-hint">
          {`${research.label} takes ${research.takes}. `}
          <SettingsLink tab="defaults" text="Change in Settings › Generation defaults" />
        </p>
      )}
      <input
        id="src-files"
        type="file"
        multiple
        accept={upload?.accept}
        hidden
        tabIndex={-1}
        aria-hidden="true"
        onChange={(e) => {
          const input = e.currentTarget;
          const files = [...(input.files ?? [])];
          // Emptied, so picking the same file again is a change.
          input.value = "";
          void A.uploadFiles(files);
        }}
      />
    </>
  );
}

/** The sources, with the rows still on their way above them: files being
 * read and topics being researched. */
function SourceList({ S, A, ro }: { S: PageState; A: PageActions; ro: boolean }) {
  const srcs = useStore(S.srcs);
  const srcsLoaded = useStore(S.srcsLoaded);
  const srcsErr = useStore(S.srcsErr);
  const researching = useStore(S.researching);
  const uploading = useStore(S.uploading);
  const addingNote = useStore(S.addingNote);
  const removing = useStore(S.removing);
  const rowErr = useStore(S.rowErr);
  const takes = useStoreSel(S.opts, (o) => o?.research.takes ?? "");
  return (
    <div className="srclist" aria-live="polite">
      {uploading.map(([n, name]) => (
        <div key={`u-${n}`} className="src run">
          <span className="src-i spin" title="Reading…" />
          <div className="src-t">
            <div className="src-n" title={name}>{`Reading ${name}…`}</div>
            <div className="src-d">Uploading and reading the file</div>
          </div>
        </div>
      ))}
      {researching.map(([n, topic, said]) => (
        <div key={`r-${n}`} className="src run">
          <span className="src-i spin" title="Researching…" />
          <div className="src-t">
            <div className="src-n">{`Researching: ${topic}`}</div>
            <div className="src-d">{said !== "" ? said : takes !== "" ? `Reading the web · ${takes}` : "Reading the web"}</div>
          </div>
        </div>
      ))}
      {addingNote !== "" && (
        <div className="src run">
          <span className="src-i spin" title="Adding…" />
          <div className="src-t">
            <div className="src-n" title={addingNote}>
              {addingNote}
            </div>
            <div className="src-d">Adding the note</div>
          </div>
        </div>
      )}
      {!srcsLoaded ? (
        <div className="src-note">
          <span className="mini-spin" />
          Loading sources…
        </div>
      ) : srcsErr !== "" && srcs.length === 0 ? (
        <div className="src-note bad" role="alert">
          {`Sources could not be loaded: ${srcsErr}`}
          <button className="link-btn" onClick={() => void A.loadSources()}>
            Try again
          </button>
        </div>
      ) : srcs.length === 0 && researching.length === 0 && uploading.length === 0 && addingNote === "" ? (
        <div className="src-empty">
          <div className="src-empty-m">
            <Icon name="link-45deg" className="xl" />
          </div>
          <div className="src-empty-t">No sources yet</div>
          <div className="dim small">
            {ro
              ? "Its author shared it without its sources."
              : "Paste links or text, upload files, or research a topic. Everything you make here is made from these."}
          </div>
        </div>
      ) : (
        srcs.map((s) => (
          <SrcRow
            key={srcKey(s)}
            s={s}
            onRemove={ro ? undefined : A.removeSource}
            busy={s.file !== "" && removing.includes(s.file)}
            err={rowErr[`src:${s.file}`] ?? ""}
            onDismissErr={() => A.setRowErr(`src:${s.file}`, "")}
          />
        ))
      )}
    </div>
  );
}
