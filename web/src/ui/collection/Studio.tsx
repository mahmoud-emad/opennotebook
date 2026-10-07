// The Studio's upper half: four tiles say what can be made; choosing one
// opens its options in place, under the tiles, and its one primary button
// makes it. What it costs is said before the click, and itemised behind
// Estimate cost.

import { useMemo, useState } from "react";
import { EstimateBanner } from "../cost";
import { CostDialog, LimitNote } from "../dialogs";
import { Icon } from "../Icon";
import { coveringMap, estimateMap } from "../mindmap";
import { coveringNotes, estimateNotes } from "../notes";
import type { Open } from "../routes";
import { assetUrl } from "../api-studio";
import { SettingsLink } from "../settings";
import { OUTPUTS, isBuild, outputBlurb, outputHint, outputIcon, outputLabel, type Output } from "../shell";
import { useStore, useStoreSel, type Store } from "../store";
import { VideoOptions, VideoTile } from "../video";
import type { PageActions } from "./actions";
import { fitLength, staged, stagedCount, type PageState } from "./state";

/** The tiles, and the chosen one's options under them. */
export function Studio({ S, A, onOpen }: { S: PageState; A: PageActions; onOpen: (o: Open) => void }) {
  const kindNow = useStore(S.chosen);
  const nSrc = useStoreSel(S.srcs, stagedCount);
  const srcsLoaded = useStore(S.srcsLoaded);
  // The Video overview tool: open or not. Its own, beside the chosen tile,
  // and never both.
  const [video, setVideo] = useState(false);
  const cid = useStore(S.props).cid;
  // On while unknown: the cost is said unless the settings turn it off.
  const showCost = useStoreSel(S.opts, (o) => o?.show_cost ?? true);
  return (
    <>
      <div className="sec-h">
        <span className="sec-t">Create</span>
      </div>
      <div className="tiles" role="group" aria-label="Create">
        {OUTPUTS.map((k) => (
          <button
            key={k}
            className={kindNow === k ? "tile on" : "tile"}
            aria-pressed={kindNow === k}
            disabled={nSrc === 0}
            title={nSrc > 0 ? outputHint[k] : srcsLoaded ? "Add a source first" : "Loading sources…"}
            onClick={() => {
              S.chosen.set(S.chosen.get() === k ? null : k);
              S.genErr.set("");
              setVideo(false);
            }}
          >
            <span className="tile-i">
              <Icon name={outputIcon[k]} className="lg" />
            </span>
            <span className="tile-n">{outputLabel[k]}</span>
            <span className="tile-d">{outputBlurb[k]}</span>
          </button>
        ))}
        <VideoTile
          on={video}
          disabled={nSrc === 0}
          title={nSrc > 0 ? "A few minutes · tens of cents" : srcsLoaded ? "Add a source first" : "Loading sources…"}
          onClick={() => {
            S.chosen.set(null);
            setVideo(!video);
          }}
        />
      </div>
      {nSrc === 0 && srcsLoaded && <p className="tiles-hint">Add a source first.</p>}

      {kindNow !== null && nSrc > 0 && <Options k={kindNow} S={S} A={A} onOpen={onOpen} />}
      {video && kindNow === null && nSrc > 0 && (
        <VideoOptions
          cid={cid}
          showCost={showCost}
          onMade={() => {
            setVideo(false);
            void A.load();
          }}
          onCancel={() => setVideo(false)}
        />
      )}
    </>
  );
}

/** A focus box. Its own component, so a keystroke in it redraws only it. */
function FocusInput({
  id,
  placeholder,
  value,
  onEnter,
}: {
  id: string;
  placeholder: string;
  value: Store<string>;
  onEnter?: () => void;
}) {
  const v = useStore(value);
  return (
    <input
      id={id}
      type="text"
      placeholder={placeholder}
      value={v}
      onChange={(e) => value.set(e.target.value)}
      onKeyDown={onEnter && ((e) => e.key === "Enter" && onEnter())}
    />
  );
}

/** The newest map or notes made from exactly the staged sources with no focus:
 * making another would say the same again, so the options say so. */
function useFresh(S: PageState, k: Output): string | null {
  const files = useStoreSel(S.srcs, (v) => staged(v).join("\n"));
  const st = k === "mindmap" ? S.mm : S.nt;
  const unfocused = useStoreSel(st.focus, (f) => f.trim() === "");
  const maps = useStore(S.mm.maps);
  const notes = useStore(S.nt.notes);
  return useMemo(() => {
    if (!unfocused) return null;
    const list = files === "" ? [] : files.split("\n");
    if (k === "mindmap") return coveringMap(maps, list);
    if (k === "notes") return coveringNotes(notes, list);
    return null;
  }, [k, unfocused, files, maps, notes]);
}

/** The chosen tile's options, under the tiles: what to pick for it, what it
 * costs, and its one primary button. */
function Options({
  k,
  S,
  A,
  onOpen,
}: {
  k: Output;
  S: PageState;
  A: PageActions;
  onOpen: (o: Open) => void;
}) {
  // What the panel offers and how it says it, as the server words it from
  // the settings.
  const opts = useStore(S.opts);
  const optsErr = useStore(S.optsErr);
  const style = useStore(S.style);
  // A deck's speakers: as picked here, or the count the settings start on.
  const speakers = useStore(S.speakers) ?? opts?.default_deck_speakers ?? null;
  const audioFormat = useStore(S.audioFormat);
  const audioLength = useStore(S.audioLength);
  const est = useStore(S.est);
  const estErr = useStore(S.estErr);
  const estLoading = useStore(S.estLoading);
  const genErr = useStore(S.genErr);
  const generating = useStore(S.generating);
  const quickSt = k === "mindmap" ? S.mm : S.nt;
  const qLoading = useStore(quickSt.estLoading);
  const qEst = useStore(quickSt.est);
  const mmMaking = useStore(S.mm.making);
  const ntMaking = useStore(S.nt.making);
  const fresh = useFresh(S, k);
  const build = isBuild(k);
  /** A map or notes of this kind is being made. */
  const making = k === "mindmap" ? mmMaking : k === "notes" ? ntMaking : false;
  const language = opts === null ? null : build ? opts.build_language_note : opts.language_note;
  // On while unknown: the cost is said unless the settings turn it off.
  const showCost = opts?.show_cost ?? true;
  const format = opts?.audio_formats.find((f) => f.id === audioFormat) ?? null;
  const offered = format?.lengths ?? [];
  const voices = opts?.deck_speakers.find((c) => c.count === speakers)?.voices ?? null;
  // A map's or notes' estimate comes in a build's shape, so every tool is
  // said and checked against the limit the same way.
  const shownEst = build ? est : qEst;
  // The build as chosen would be refused for its cost.
  const over = build ? !!est?.over_limit : !!shownEst?.over_limit;
  const onEstimate = () => {
    S.estOpen.set(true);
    if (est === null && !estLoading) void A.fetchEstimate();
  };
  return (
    <div className="opts" role="region" aria-label={`${outputLabel[k]} options`}>
      <div className="opts-h">
        <span className="opts-t">{outputLabel[k]}</span>
        <span className="opts-d">{outputHint[k]}</span>
      </div>
      {language && (
        <p className="lang-chip">
          <Icon name="translate" />
          {`${language} `}
          <SettingsLink tab="general" text="Settings › General" />
        </p>
      )}
      {opts === null && optsErr !== "" && (
        <div className="opt-err" role="alert">
          {`The options could not be loaded: ${optsErr} `}
          <button className="link-btn" onClick={() => void A.loadOptions()}>
            Try again
          </button>
        </div>
      )}
      {k === "session" && (
        <>
          <div className="opt-l">Style</div>
          <div className="style-grid" role="radiogroup" aria-label="Style">
            {(opts?.styles ?? []).map((st) => (
              <button
                key={st.id}
                className={style === st.id ? "style on" : "style"}
                role="radio"
                aria-checked={style === st.id}
                title={st.blurb}
                onClick={() => {
                  S.picked.set(true);
                  S.style.set(st.id);
                }}
              >
                <span className="sw" style={{ backgroundImage: `url(${assetUrl(st.thumbnail)})` }} />
                <span className="style-n">{st.label}</span>
              </button>
            ))}
          </div>
          {opts !== null && (
            <div className="ao-len" role="radiogroup" aria-label="Speakers">
              <span className="opt-l">Speakers</span>
              {opts.deck_speakers.map((c) => (
                <button
                  key={c.count}
                  className={speakers === c.count ? "chip on" : "chip"}
                  role="radio"
                  aria-checked={speakers === c.count}
                  onClick={() => S.speakers.set(c.count)}
                >
                  {c.label}
                </button>
              ))}
            </div>
          )}
          {voices !== null && (
            <p className="opt-hint">
              {`${voices} `}
              <SettingsLink tab="voices" text="Change in Settings › Voices" />
            </p>
          )}
          {opts !== null && (
            <p className="opt-hint">
              {`${opts.deck_summary} · `}
              <SettingsLink tab="defaults" text="Change defaults in Settings" />
            </p>
          )}
        </>
      )}
      {k === "audio" && (
        <>
          <div className="opt-l">Format</div>
          <div className="ao-formats" role="radiogroup" aria-label="Format">
            {(opts?.audio_formats ?? []).map((f) => (
              <button
                key={f.id}
                className={audioFormat === f.id ? "ao-f on" : "ao-f"}
                role="radio"
                aria-checked={audioFormat === f.id}
                onClick={() => {
                  S.picked.set(true);
                  S.audioFormat.set(f.id);
                  fitLength(S);
                }}
              >
                <span className="ao-n">{f.label}</span>
                <span className="ao-d">{f.blurb}</span>
              </button>
            ))}
          </div>
          {offered.length > 0 && (
            <div className="ao-len" role="radiogroup" aria-label="Length">
              <span className="opt-l">Length</span>
              {offered.map((l) => (
                <button
                  key={l.id}
                  className={audioLength === l.id ? "chip on" : "chip"}
                  role="radio"
                  aria-checked={audioLength === l.id}
                  onClick={() => {
                    S.picked.set(true);
                    S.audioLength.set(l.id);
                  }}
                >
                  {l.label}
                </button>
              ))}
            </div>
          )}
          {format !== null && (
            <p className="opt-hint">
              {`${format.voices} `}
              <SettingsLink tab="voices" text="Change in Settings › Voices" />
            </p>
          )}
          <label className="opt-l" htmlFor="ao-focus">
            Focus
          </label>
          <FocusInput id="ao-focus" placeholder="A topic, an audience or a level (optional)" value={S.audioFocus} />
        </>
      )}
      {k === "mindmap" && (
        <>
          <label className="opt-l" htmlFor="mm-focus">
            Focus
          </label>
          <FocusInput
            id="mm-focus"
            placeholder="Centre the map on a topic (optional)"
            value={S.mm.focus}
            onEnter={() => A.generate("mindmap")}
          />
          {fresh !== null && (
            <p className="opt-note">
              <Icon name="check-circle-fill" />
              {" A map of exactly these sources exists. "}
              <button className="link-btn" onClick={() => onOpen({ kind: "map", id: fresh })}>
                Open it
              </button>
            </p>
          )}
        </>
      )}
      {k === "notes" && (
        <>
          <label className="opt-l" htmlFor="nt-focus">
            Focus
          </label>
          <FocusInput
            id="nt-focus"
            placeholder="Centre the notes on a topic (optional)"
            value={S.nt.focus}
            onEnter={() => A.generate("notes")}
          />
          {fresh !== null && (
            <p className="opt-note">
              <Icon name="check-circle-fill" />
              {" Notes of exactly these sources exist. "}
              <button className="link-btn" onClick={() => onOpen({ kind: "notes", id: fresh })}>
                Open them
              </button>
            </p>
          )}
        </>
      )}

      {/* What it costs, said before the click, the same way for every tool.
          Off in Settings, it is not said at all; over the limit is said below
          either way. */}
      {showCost && !over && (
        <EstimateBanner
          est={shownEst}
          loading={build ? estLoading : qLoading}
          failed={build ? estErr !== "" : !qLoading && qEst === null}
        />
      )}
      {/* Over the limit is said whether or not costs are shown: it is not a
          note about cost but the reason Generate is off. */}
      {shownEst?.over_limit && (
        <LimitNote e={shownEst} className="opt-err" />
      )}
      {genErr !== "" && (
        <div className="opt-err" role="alert">
          {genErr}
        </div>
      )}
      <div className="opts-a">
        <button title="Every step and what it costs, before you start" onClick={onEstimate}>
          Estimate cost
        </button>
        <span className="grow" />
        <button className="ghost" onClick={() => S.chosen.set(null)}>
          Cancel
        </button>
        <button className="primary" disabled={generating || over || making} onClick={() => A.generate(k)}>
          {generating ? "Starting…" : `Generate ${outputLabel[k].toLowerCase()}`}
        </button>
      </div>
    </div>
  );
}

/** The itemised cost of what is chosen, behind the options' Estimate cost. */
export function CostDialogs({ S, A, cid }: { S: PageState; A: PageActions; cid: string }) {
  const estOpen = useStore(S.estOpen);
  const kindNow = useStore(S.chosen);
  const est = useStore(S.est);
  const estErr = useStore(S.estErr);
  const estLoading = useStore(S.estLoading);
  const mmEst = useStore(S.mm.est);
  const mmEstLoading = useStore(S.mm.estLoading);
  const ntEst = useStore(S.nt.est);
  const ntEstLoading = useStore(S.nt.estLoading);
  const mmEstErr = useStore(S.mm.estErr);
  const ntEstErr = useStore(S.nt.estErr);
  const close = () => S.estOpen.set(false);
  if (!estOpen) return null;
  if (kindNow === "mindmap" || kindNow === "notes") {
    const q = kindNow === "mindmap" ? mmEst : ntEst;
    const loading = kindNow === "mindmap" ? mmEstLoading : ntEstLoading;
    // Why it could not be had, as the studio said it.
    const failed = kindNow === "mindmap" ? mmEstErr : ntEstErr;
    return (
      <CostDialog
        est={q}
        verb="Make"
        loading={loading}
        err={loading || q !== null ? "" : failed}
        onClose={close}
        onRetry={() =>
          void (kindNow === "mindmap" ? estimateMap(cid, S.mm, A.signal()) : estimateNotes(cid, S.nt, A.signal()))
        }
        onBuild={() => {
          close();
          A.generate(kindNow);
        }}
      />
    );
  }
  return (
    <CostDialog
      est={est}
      loading={estLoading}
      err={estErr}
      onClose={close}
      onRetry={() => void A.fetchEstimate()}
      onBuild={() => {
        close();
        const k = S.chosen.get();
        if (k !== null) A.generate(k);
      }}
    />
  );
}
