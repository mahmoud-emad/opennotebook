// The Settings dialog: the theme, which is this browser's, and the studio's
// own settings, drawn from what the server sends. A port of the dialog half of
// the old app's `settings.rs`, element for element.
//
// Every change saves the moment it is made; there is no Save button to forget.

import { useEffect, useRef, useState } from "react";
import { errText, settingsSet, type SettingItem, type SettingsDoc, type TabInfo } from "./api";
import { Icon } from "./Icon";
import {
  APPEARANCE,
  GENERAL,
  PROVIDERS,
  SETTINGS,
  TAB_LABELS,
  THEME,
  applyTheme,
  effective,
  keys,
  reloadSettings,
  settingValue,
  shownValue,
  tabGlyph,
  themeIcon,
  themeLabel,
  type ThemePref,
} from "./settings";
import { ProvidersPanel } from "./providers";
import { focusId } from "./shell";
import { useStore } from "./store";
import { assetUrl, styleList, type StyleChoice } from "./api-studio";

/** The id of the Settings dialog's one tab panel. */
export const SET_PANEL = "set-panel";

/** A Settings tab's element id. */
export function tabEl(id: string): string {
  return `set-tab-${id}`;
}

/** The tabs in order, as the server lists them. Until it answers, General
 * alone, so the theme can be changed while the rest loads. */
export function allTabs(d: SettingsDoc): TabInfo[] {
  return d.tab_info.length > 0 ? d.tab_info : [{ id: GENERAL, label: "General", note: "", advanced: false }];
}

/** Rows in their catalogue order, gathered under their group headings. */
export function grouped(rows: SettingItem[]): [string, SettingItem[]][] {
  const out: [string, SettingItem[]][] = [];
  for (const r of rows) {
    const last = out[out.length - 1];
    if (last && last[0] === r.group) last[1].push(r);
    else out.push([r.group, [r]]);
  }
  return out;
}

/** What a save says under its control: saving, saved, or why it was
 * refused. */
export type SaveState = { kind: "saving" } | { kind: "saved" } | { kind: "failed"; text: string };

/** Save one setting and say how it went. A refusal is the server's own
 * sentence (`{"detail": …}`), as `settingsSet` throws it. */
export async function saveSetting(key: string, value: string): Promise<[SettingItem | null, SaveState]> {
  try {
    return [await settingsSet(key, value), { kind: "saved" }];
  } catch (e) {
    return [null, { kind: "failed", text: errText(e) }];
  }
}

export function SettingsDialog({ onClose }: { onClose: () => void }) {
  const s = useStore(SETTINGS);
  const theme = useStore(THEME);
  // The tab shown, by id. Starts where the link that opened it pointed.
  const [tab, setTab] = useState(() => SETTINGS.get().open ?? GENERAL);
  const [status, setStatus] = useState<Record<string, SaveState>>({});
  const box = useRef<HTMLDivElement>(null);

  // Read again on opening: another tab or an agent may have changed them.
  // Focus moves into the dialog so Escape and Tab work from the start.
  useEffect(() => {
    void reloadSettings();
    box.current?.focus();
  }, []);

  const save = (key: string, value: string) => {
    setStatus((m) => ({ ...m, [key]: { kind: "saving" } }));
    void saveSetting(key, value).then(([item, st]) => {
      if (item) {
        SETTINGS.set((p) => ({
          ...p,
          doc: { ...p.doc, items: p.doc.items.map((i) => (i.key === item.key ? item : i)) },
        }));
      }
      setStatus((m) => ({ ...m, [key]: st }));
    });
  };

  const d = s.doc;
  const loaded = s.loaded;
  const loadErr = s.err;
  const tabs = allTabs(d);
  let current = tab;
  // The old Appearance tab is General's theme row now.
  if (current === APPEARANCE) current = GENERAL;
  // A link to a tab this server does not have lands on the first one.
  if (loaded && !tabs.some((t) => t.id === current)) {
    current = tabs[0]?.id ?? GENERAL;
  }
  const info: TabInfo = tabs.find((t) => t.id === current) ?? {
    id: current,
    label: TAB_LABELS.find((t) => t[0] === current)?.[1] ?? "Settings",
    note: "",
    advanced: false,
  };
  const oneSpeaker = settingValue(d, keys.SPEAKER_COUNT) === "1";
  const groups = grouped(d.items.filter((i) => i.tab === info.label));
  // In the order they are drawn: the advanced ones last.
  const ids = [...tabs.filter((t) => !t.advanced), ...tabs.filter((t) => t.advanced)].map((t) => t.id);
  const close = () => {
    SETTINGS.set((p) => ({ ...p, open: null }));
    onClose();
  };

  return (
    <>
      <div className="set-veil over" onClick={close} />
      <div
        ref={box}
        className="set-dialog over"
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        tabIndex={-1}
        onKeyDown={(e) => {
          if (e.key === "Escape") close();
        }}
      >
        <nav className="set-nav">
          <h2>Settings</h2>
          {/* Up and down move between the tabs, as in any vertical tablist;
              only the selected tab is in the Tab order. */}
          <div
            className="set-tabs"
            role="tablist"
            aria-label="Settings"
            aria-orientation="vertical"
            onKeyDown={(e) => {
              const n = Math.max(ids.length, 1);
              const at = Math.max(ids.indexOf(tab), 0);
              let to: number;
              switch (e.key) {
                case "ArrowDown":
                  to = (at + 1) % n;
                  break;
                case "ArrowUp":
                  to = (at + n - 1) % n;
                  break;
                case "Home":
                  to = 0;
                  break;
                case "End":
                  to = n - 1;
                  break;
                default:
                  return;
              }
              e.preventDefault();
              const id = ids[to];
              if (id !== undefined) {
                setTab(id);
                focusId(tabEl(id));
              }
            }}
          >
            {tabs
              .filter((t) => !t.advanced)
              .map((t) => (
                <TabButton key={t.id} t={t} on={t.id === current} onPick={setTab} />
              ))}
            {/* The advanced tabs under their own heading, after the rest. */}
            {tabs.some((t) => t.advanced) && (
              <div className="set-tabs-h" role="presentation">
                Advanced
              </div>
            )}
            {tabs
              .filter((t) => t.advanced)
              .map((t) => (
                <TabButton key={t.id} t={t} on={t.id === current} onPick={setTab} />
              ))}
          </div>
        </nav>
        <section id={SET_PANEL} className="set-body" role="tabpanel" aria-labelledby={tabEl(current)}>
          <div className="set-head">
            <h3>{info.label}</h3>
            <button className="icon-btn" aria-label="Close settings" title="Close" onClick={close}>
              <Icon name="x-lg" />
            </button>
          </div>
          {info.note !== "" && (
            <p className="set-tnote">
              <Icon name="info-circle" />
              <span>{info.note}</span>
            </p>
          )}
          {current === GENERAL && (
            <div className="set-row">
              <div className="set-text">
                <div className="set-label">Theme</div>
                <div className="set-help">Applies here and in the player.</div>
              </div>
              <div className="set-ctl">
                <div className="seg" role="radiogroup" aria-label="Theme">
                  {(["dark", "light"] as ThemePref[]).map((t) => (
                    <button
                      key={themeLabel(t)}
                      className={theme === t ? "on" : ""}
                      role="radio"
                      aria-checked={theme === t ? "true" : "false"}
                      onClick={() => applyTheme(t)}
                    >
                      <Icon name={themeIcon(t)} />
                      {themeLabel(t)}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}
          {current === PROVIDERS ? (
            <ProvidersPanel />
          ) : !loaded && d.items.length === 0 ? (
            <div className="set-note">
              <span className="mini-spin" /> Loading settings…
            </div>
          ) : loadErr !== "" && d.items.length === 0 ? (
            <div className="set-note err" role="alert">
              Settings could not be loaded: {loadErr}
              <div>
                <button className="est-retry" onClick={() => void reloadSettings()}>
                  Try again
                </button>
              </div>
            </div>
          ) : (
            groups.map(([g, rows]) => {
              const off = oneSpeaker && g === "Second voice";
              return (
                <div key={`g-${g}`} className={off ? "set-grp off" : "set-grp"}>
                  {g !== "" && <h4 className="set-gh">{g}</h4>}
                  {off && (
                    <p className="set-gnote">
                      Decks use one voice while Speakers in a deck is One. Deep Dive, Critique and Debate still use
                      this one.
                    </p>
                  )}
                  {rows.map((item) => (
                    <SettingRow key={item.key} item={item} status={status[item.key]} onSave={save} />
                  ))}
                </div>
              );
            })
          )}
          {current !== PROVIDERS && (
            <p className="set-foot">
              Saved as soon as you change it, for the next thing you make or open.
              {current === GENERAL && " The theme is kept in this browser."}
            </p>
          )}
        </section>
      </div>
    </>
  );
}

/** One tab in the Settings tablist. */
function TabButton({ t, on, onPick }: { t: TabInfo; on: boolean; onPick: (id: string) => void }) {
  return (
    <button
      id={tabEl(t.id)}
      className={on ? "set-tab on" : "set-tab"}
      role="tab"
      tabIndex={on ? 0 : -1}
      aria-selected={on ? "true" : "false"}
      aria-controls={SET_PANEL}
      onClick={() => onPick(t.id)}
    >
      <span className="set-glyph">
        <Icon name={tabGlyph(t.id)} />
      </span>
      {t.label}
    </button>
  );
}

/** The value a model row's select shows when the id is not a tested one. */
export const CUSTOM = "\u0000custom";

/** What a field shows: what the person last typed or picked, until the value
 * it stands for changes (a save, a reset, a reload), as the old page's fields
 * kept their text until the value behind them moved. */
function useDraft(src: string): [string, (v: string) => void] {
  const [draft, setDraft] = useState(src);
  const [seen, setSeen] = useState(src);
  if (seen !== src) {
    setSeen(src);
    setDraft(src);
  }
  return [draft, setDraft];
}

/** A text field's own `change` event: on Enter or on leaving the field, not on
 * every keystroke, which is what React's `onChange` would give. */
function onCommit(fn: (v: string) => void) {
  return (el: HTMLInputElement | null) => {
    if (!el) return;
    const h = () => fn(el.value);
    el.addEventListener("change", h);
    return () => el.removeEventListener("change", h);
  };
}

// The slide styles with their pictures, read once from the server.
let styleCache: StyleChoice[] = [];

/** The slide styles to draw the style setting with, read only for that row
 * (`on`). Until the server has answered, the setting's own choices, with no
 * pictures. */
function useStyles(item: SettingItem, on: boolean): StyleChoice[] {
  const [list, setList] = useState<StyleChoice[]>(styleCache);
  useEffect(() => {
    if (!on || styleCache.length > 0) return;
    const stop = new AbortController();
    styleList(stop.signal).then(
      (l) => {
        styleCache = l;
        setList(l);
      },
      // The setting's own choices stand in; the dialog still saves.
      () => {},
    );
    return () => stop.abort();
  }, [on]);
  return list.length > 0
    ? list
    : item.options.map((o) => ({ id: o.value, label: o.label, blurb: "", thumbnail: "" }));
}

export function SettingRow({
  item,
  status,
  onSave,
}: {
  item: SettingItem;
  status: SaveState | undefined;
  onSave: (key: string, value: string) => void;
}) {
  const key = item.key;
  const eff = effective(item);
  const changed = item.value !== "";
  const listId = `sug-${item.key}`;
  // A model row: the person chose "Custom…" and is typing an id.
  const [customOpen, setCustomOpen] = useState(false);
  const tested = item.options.some((o) => o.value === eff);
  const custom = item.model && (customOpen || !tested);
  const styleRow = item.key === keys.STYLE;
  const styles = useStyles(item, styleRow);
  const [sel, setSel] = useDraft(custom ? CUSTOM : eff);
  const [text, setText] = useDraft(item.model ? (tested ? "" : eff) : eff);

  // "Custom…" chosen: the field for the id takes the keyboard.
  useEffect(() => {
    if (customOpen) focusId(`cm-${key}`);
  }, [customOpen, key]);

  let control;
  if (styleRow) {
    // The slide styles as their sample slides, as the Create panel shows them.
    control = (
      <div className="style-grid set-styles" role="radiogroup" aria-label={item.label}>
        {styles.map((st) => (
          <button
            key={st.id}
            className={eff === st.id ? "style on" : "style"}
            role="radio"
            aria-checked={eff === st.id ? "true" : "false"}
            title={st.blurb}
            onClick={() => onSave(key, st.id)}
          >
            <span
              className="sw"
              style={st.thumbnail !== "" ? { backgroundImage: `url(${assetUrl(st.thumbnail)})` } : undefined}
            />
            <span className="style-n">{st.label}</span>
          </button>
        ))}
      </div>
    );
  } else if (item.model) {
    control = (
      <div className="set-model">
        <select
          className="set-input"
          aria-label={item.label}
          value={custom ? CUSTOM : sel}
          onChange={(e) => {
            const v = e.target.value;
            setSel(v);
            if (v === CUSTOM) {
              setCustomOpen(true);
            } else {
              setCustomOpen(false);
              onSave(key, v);
            }
          }}
        >
          {item.options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.hint === "" ? o.label : `${o.label} · ${o.hint}`}
            </option>
          ))}
          <option value={CUSTOM}>Custom…</option>
        </select>
        {custom && (
          <>
            <input
              id={`cm-${item.key}`}
              ref={onCommit((raw) => {
                const v = raw.trim();
                if (v !== "") onSave(key, v);
              })}
              className="set-input"
              type="text"
              aria-label={`${item.label}, a model id`}
              list={listId}
              spellCheck={false}
              placeholder="provider/model-id"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <datalist id={listId}>
              {item.suggestions.map((sg) => (
                <option key={sg} value={sg} />
              ))}
            </datalist>
          </>
        )}
      </div>
    );
  } else if (item.kind === "choice") {
    control = (
      <select
        className="set-input"
        aria-label={item.label}
        value={sel}
        onChange={(e) => {
          setSel(e.target.value);
          onSave(key, e.target.value);
        }}
      >
        {item.options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    );
  } else if (item.kind === "toggle") {
    const on = eff !== "off";
    control = (
      <button
        className={on ? "switch on" : "switch"}
        role="switch"
        aria-checked={on ? "true" : "false"}
        aria-label={item.label}
        onClick={() => onSave(key, on ? "off" : "on")}
      >
        <span className="knob" />
      </button>
    );
  } else if (item.kind === "number") {
    const range = item.min !== null && item.max !== null ? `${item.min} to ${item.max}` : "";
    control = (
      <>
        <input
          ref={onCommit((v) => onSave(key, v))}
          className="set-input narrow"
          type="number"
          aria-label={item.unit === "" ? item.label : `${item.label}, in ${item.unit}`}
          title={range}
          min={item.min ?? ""}
          max={item.max ?? ""}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        {item.unit !== "" && <span className="set-unit">{item.unit}</span>}
      </>
    );
  } else {
    control = (
      <>
        {/* The value in force, default included, so a field never looks empty
            when something applies. Saving the default back removes the
            override on the server. */}
        <input
          ref={onCommit((v) => onSave(key, v))}
          className="set-input"
          type="text"
          aria-label={item.label}
          list={listId}
          placeholder="Uses the default"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        {item.suggestions.length > 0 && (
          <datalist id={listId}>
            {item.suggestions.map((sg) => (
              <option key={sg} value={sg} />
            ))}
          </datalist>
        )}
      </>
    );
  }

  const [stClass, stText] = saveLine(status);
  const defaultShown = shownValue(item, item.default);
  return (
    <div className={styleRow ? "set-row wide" : "set-row"}>
      <div className="set-text">
        <div className="set-label">{item.label}</div>
        {item.help !== "" && <div className="set-help">{item.help}</div>}
        {item.model && item.price !== "" && <div className="set-meta num">In use: {item.price}</div>}
        {changed && <div className="set-meta">Default: {defaultShown}</div>}
        {stText !== "" && (
          <div className={stClass} role={stClass.endsWith("err") ? "alert" : "status"}>
            {stText}
          </div>
        )}
      </div>
      <div className="set-ctl">
        {control}
        {changed && (
          <button
            className="set-reset"
            title={`Back to the default: ${defaultShown}`}
            onClick={() => {
              setCustomOpen(false);
              onSave(key, "");
            }}
          >
            <Icon name="arrow-counterclockwise" />
            Reset
          </button>
        )}
      </div>
    </div>
  );
}

/** The line under a control for a save's state: its class and its words. */
export function saveLine(status: SaveState | undefined): [string, string] {
  switch (status?.kind) {
    case "saving":
      return ["set-st", "Saving…"];
    case "saved":
      return ["set-st ok", "Saved"];
    case "failed":
      return ["set-st err", status.text];
    default:
      return ["set-st", ""];
  }
}
