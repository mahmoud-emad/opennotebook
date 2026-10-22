// Settings › Transformations: the instructions /transform runs over the
// sources. The built-in ones are listed to read; the person's own are added,
// changed and removed here.

import { useCallback, useEffect, useState } from "react";
import { errText } from "./api";
import {
  transformationAdd,
  transformationChange,
  transformationRemove,
  transformationsLoad,
  type Transformation,
} from "./api-transformations";

/** The name and instruction being typed, and the one being changed (none for a new one). */
type Draft = { id: string | null; name: string; prompt: string };

const BLANK: Draft = { id: null, name: "", prompt: "" };

export function TransformationsPanel() {
  const [list, setList] = useState<Transformation[] | null>(null);
  const [err, setErr] = useState("");
  const [draft, setDraft] = useState<Draft>(BLANK);
  const [busy, setBusy] = useState(false);
  // Why the last save or removal failed, said by the form.
  const [said, setSaid] = useState("");
  // The one asked "Remove it?", so a click is never the end of one.
  const [asked, setAsked] = useState<string | null>(null);

  const load = useCallback(() => {
    transformationsLoad().then(
      (l) => {
        setList(l);
        setErr("");
      },
      (e: unknown) => setErr(errText(e)),
    );
  }, []);
  useEffect(load, [load]);

  const save = () => {
    setBusy(true);
    setSaid("");
    const done =
      draft.id === null
        ? transformationAdd(draft.name, draft.prompt)
        : transformationChange(draft.id, draft.name, draft.prompt);
    done.then(
      () => {
        setDraft(BLANK);
        setBusy(false);
        load();
      },
      (e: unknown) => {
        setSaid(errText(e));
        setBusy(false);
      },
    );
  };

  const remove = (t: Transformation) => {
    setAsked(null);
    setSaid("");
    transformationRemove(t.id).then(
      () => {
        if (draft.id === t.id) setDraft(BLANK);
        load();
      },
      (e: unknown) => setSaid(`“${t.name}” could not be removed. ${errText(e)}`),
    );
  };

  if (list === null) {
    return err !== "" ? (
      <div className="set-note err" role="alert">
        Transformations could not be loaded: {err}
        <div>
          <button className="est-retry" onClick={load}>
            Try again
          </button>
        </div>
      </div>
    ) : (
      <div className="set-note">
        <span className="mini-spin" /> Loading transformations…
      </div>
    );
  }

  const mine = list.filter((t) => !t.builtin);
  const ready = draft.name.trim() !== "" && draft.prompt.trim() !== "";
  return (
    <div className="tf">
      <div className="set-grp">
        <h4 className="set-gh">Built in</h4>
        {list
          .filter((t) => t.builtin)
          .map((t) => (
            <div key={t.id} className="set-row">
              <div className="set-text">
                <div className="set-label">{t.name}</div>
                <div className="set-help">{t.description}</div>
              </div>
            </div>
          ))}
      </div>
      <div className="set-grp">
        <h4 className="set-gh">Your own</h4>
        {mine.length === 0 && <div className="set-help tf-none">You have none yet. Add one below.</div>}
        {mine.map((t) => (
          <div key={t.id} className="set-row">
            <div className="set-text">
              <div className="set-label">{t.name}</div>
              <div className="set-help tf-prompt">{t.prompt}</div>
            </div>
            <div className="set-ctl tf-acts">
              <button className="ghost" onClick={() => setDraft({ id: t.id, name: t.name, prompt: t.prompt })}>
                Change
              </button>
              {asked === t.id ? (
                <>
                  <button className="primary" onClick={() => remove(t)}>
                    Remove it
                  </button>
                  <button className="ghost" onClick={() => setAsked(null)}>
                    Keep it
                  </button>
                </>
              ) : (
                <button className="ghost" onClick={() => setAsked(t.id)}>
                  Remove
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
      <div className="prov-add">
        <h4 className="set-gh">{draft.id === null ? "Add your own" : "Change it"}</h4>
        <div className="prov-fields">
          <label className="prov-field">
            <span className="set-label">Name</span>
            <input
              className="set-input prov-input"
              value={draft.name}
              maxLength={60}
              placeholder="Glossary"
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            />
            <span className="set-help">Type it after /transform to run it.</span>
          </label>
          <label className="prov-field">
            <span className="set-label">Instruction</span>
            <textarea
              className="set-input prov-input tf-text"
              value={draft.prompt}
              maxLength={2000}
              placeholder="Define every technical term the sources use, one line each."
              onChange={(e) => setDraft({ ...draft, prompt: e.target.value })}
            />
            <span className="set-help">What to do with the sources. The answer cites the passages it rests on.</span>
          </label>
          <div className="prov-go">
            <button className="primary" disabled={!ready || busy} onClick={save}>
              {busy ? "Saving…" : draft.id === null ? "Add" : "Save"}
            </button>
            {draft.id !== null && (
              <button className="ghost" disabled={busy} onClick={() => setDraft(BLANK)}>
                Cancel
              </button>
            )}
          </div>
          {said !== "" && (
            <p className="set-st err" role="alert">
              {said}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
