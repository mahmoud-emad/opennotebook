// The studio's first run and its AI providers. Until a provider is connected
// the app shows the setup tour instead of anything else (`SetupGate`): what
// OpenNotebook needs, a provider and its key (tested before it is kept), then
// the model each kind of work will use. Settings › AI providers draws the
// same pieces (`ProvidersPanel`).

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { errText, settingsSet } from "./api";
import {
  providerAdd,
  providerModels,
  providerRecheck,
  providerRemove,
  providersLoad,
  setupLoad,
  type KeyCheck,
  type Preset,
  type Provider,
  type Providers,
  type Role,
  type Setup,
} from "./api-providers";
import { Icon } from "./Icon";
import { Logo } from "./Logo";
import { reloadSettings } from "./settings";
import { store, useStore } from "./store";

// ── the gate ─────────────────────────────────────────────────────────────────

type Gate =
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "setup"; setup: Setup }
  | { kind: "error"; text: string };

/** Asks the gate to read the studio's state again: after the last provider
 * is removed, the tour comes back. */
export const SETUP_CHECK = store(0);

export function recheckSetup(): void {
  SETUP_CHECK.set((n) => n + 1);
}

/** The app, once the studio has a provider; the setup tour until then. */
export function SetupGate({ children }: { children: ReactNode }) {
  const [gate, setGate] = useState<Gate>({ kind: "loading" });
  const asked = useStore(SETUP_CHECK);

  const load = useCallback(() => {
    setupLoad().then(
      // A tour under way stays until it is finished, though the studio
      // became ready at its second step.
      (s) => setGate((g) => (!s.ready ? { kind: "setup", setup: s } : g.kind === "setup" ? g : { kind: "ready" })),
      (e: unknown) => setGate({ kind: "error", text: errText(e) }),
    );
  }, []);

  useEffect(() => {
    load();
  }, [load, asked]);

  switch (gate.kind) {
    case "ready":
      return <>{children}</>;
    case "setup":
      return <SetupTour setup={gate.setup} onDone={() => setGate({ kind: "ready" })} />;
    case "loading":
      return (
        <main className="setup-page">
          <div className="set-note" role="status">
            <span className="mini-spin" /> Starting OpenNotebook…
          </div>
        </main>
      );
    case "error":
      return (
        <main className="setup-page">
          <div className="empty" role="alert">
            <div className="empty-mark bad">
              <Icon name="exclamation-triangle-fill" />
            </div>
            <div className="empty-t">The studio could not be reached</div>
            <div className="empty-d">{gate.text}</div>
            <button
              onClick={() => {
                setGate({ kind: "loading" });
                load();
              }}
            >
              Try again
            </button>
          </div>
        </main>
      );
  }
}

// ── the tour ─────────────────────────────────────────────────────────────────

type Step = "welcome" | "connect" | "models";
const STEPS: [Step, string][] = [
  ["welcome", "Welcome"],
  ["connect", "Connect a provider"],
  ["models", "Choose models"],
];

export function SetupTour({ setup, onDone }: { setup: Setup; onDone: () => void }) {
  const [step, setStep] = useState<Step>("welcome");
  const [added, setAdded] = useState<Provider | null>(null);

  return (
    <main className="setup-page">
      <div className="setup-card" role="dialog" aria-modal="false" aria-labelledby="setup-title">
        <div className="setup-brand">
          <Logo />
          <span>OpenNotebook</span>
        </div>
        {!setup.can_setup ? (
          <>
            <h1 id="setup-title">This studio is not set up yet</h1>
            <p className="setup-lead">{setup.message}</p>
          </>
        ) : (
          <>
            <ol className="setup-steps" aria-label="Setup steps">
              {STEPS.map(([id, label], i) => (
                <li key={id} className={id === step ? "on" : STEPS.findIndex((s) => s[0] === step) > i ? "done" : ""}>
                  <span className="setup-n">{i + 1}</span>
                  {label}
                </li>
              ))}
            </ol>
            {step === "welcome" && (
              <>
                <h1 id="setup-title">Welcome to OpenNotebook</h1>
                <p className="setup-lead">
                  OpenNotebook reads your sources and makes decks, audio overviews, videos, mind maps and study notes
                  from them with the AI models you choose. Connect an AI provider to start.
                </p>
                <ul className="setup-points">
                  <li>
                    <Icon name="check-circle-fill" /> Your key is tested before it is kept, so you know it works and has
                    credit.
                  </li>
                  <li>
                    <Icon name="check-circle-fill" /> It is kept encrypted on this server and only ever sent to the
                    provider.
                  </li>
                  <li>
                    <Icon name="check-circle-fill" /> OpenRouter, OpenAI, Anthropic, Gemini, Mistral, Groq, DeepSeek,
                    Ollama and any OpenAI-compatible server all work.
                  </li>
                </ul>
                <div className="setup-actions">
                  <button className="primary" onClick={() => setStep("connect")}>
                    Connect a provider
                    <Icon name="chevron-right" />
                  </button>
                </div>
              </>
            )}
            {step === "connect" && (
              <>
                <h1 id="setup-title">Connect an AI provider</h1>
                <p className="setup-lead">
                  Pick where your models come from. OpenRouter is the simplest: one key for every feature.
                </p>
                <ProviderForm
                  onConnected={(p) => {
                    setAdded(p);
                    setStep("models");
                  }}
                />
                <div className="setup-actions">
                  <button className="ghost" onClick={() => setStep("welcome")}>
                    <Icon name="chevron-left" />
                    Back
                  </button>
                </div>
              </>
            )}
            {step === "models" && (
              <>
                <h1 id="setup-title">Choose models</h1>
                {added?.check && (
                  <p className="setup-ok" role="status">
                    <Icon name="check-circle-fill" /> {added.check.sentence}
                  </p>
                )}
                <p className="setup-lead">
                  Each kind of work uses the model below, picked from what your providers offer. Change any of them now
                  or later in Settings › Models.
                </p>
                <RolesList />
                <div className="setup-actions">
                  <button className="ghost" onClick={() => setStep("connect")}>
                    <Icon name="plus-lg" />
                    Add another provider
                  </button>
                  <button className="primary" onClick={onDone}>
                    Open the studio
                    <Icon name="chevron-right" />
                  </button>
                </div>
              </>
            )}
          </>
        )}
      </div>
    </main>
  );
}

// ── connecting one ───────────────────────────────────────────────────────────

/** Pick a provider, give its key (or its address), and connect it. The
 * server tests the key first and refuses one that does not work, with the
 * reason; nothing is kept until it passes. */
export function ProviderForm({ onConnected }: { onConnected: (p: Provider) => void }) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [loadErr, setLoadErr] = useState("");
  const [kind, setKind] = useState("openrouter");
  const [key, setKey] = useState("");
  const [url, setUrl] = useState("");
  const [otherUrl, setOtherUrl] = useState(false);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    providersLoad().then(
      (p) => setPresets(p.presets),
      (e: unknown) => setLoadErr(errText(e)),
    );
  }, []);

  const p = presets.find((x) => x.kind === kind);
  const local = kind === "ollama" || kind === "custom";
  const showUrl = local || otherUrl;
  const ready = p !== undefined && (!p.needs_key || key.trim() !== "") && (!showUrl || url.trim() !== "" || p.base_url !== "");

  const pick = (k: string) => {
    setKind(k);
    setSaid(null);
    setOtherUrl(false);
    setUrl(presets.find((x) => x.kind === k)?.base_url ?? "");
  };

  const connect = () => {
    setBusy(true);
    setSaid(null);
    providerAdd({ kind, key: key.trim(), base_url: showUrl ? url.trim() : "" }).then(
      (added) => {
        setBusy(false);
        setKey("");
        setSaid({ ok: true, text: added.check?.sentence ?? `Connected to ${added.label}.` });
        void reloadSettings();
        onConnected(added);
      },
      (e: unknown) => {
        setBusy(false);
        setSaid({ ok: false, text: errText(e) });
      },
    );
  };

  if (loadErr !== "") {
    return (
      <div className="set-note err" role="alert">
        The providers could not be loaded: {loadErr}
      </div>
    );
  }
  if (presets.length === 0) {
    return (
      <div className="set-note">
        <span className="mini-spin" /> Loading providers…
      </div>
    );
  }
  return (
    <div className="prov-form">
      <div className="prov-presets" role="radiogroup" aria-label="AI provider">
        {presets.map((x) => (
          <button
            key={x.kind}
            role="radio"
            aria-checked={x.kind === kind ? "true" : "false"}
            className={x.kind === kind ? "prov-preset on" : "prov-preset"}
            onClick={() => pick(x.kind)}
          >
            <span className="prov-name">{x.label}</span>
            <span className="prov-blurb">{x.blurb}</span>
          </button>
        ))}
      </div>
      {p && (
        <div className="prov-fields">
          {p.needs_key && (
            <label className="prov-field">
              <span className="set-label">{p.label} API key</span>
              <input
                className="set-input prov-input"
                type="password"
                autoComplete="off"
                spellCheck={false}
                placeholder="Paste your key"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && ready && !busy) connect();
                }}
              />
              {p.key_page !== "" && (
                <span className="set-help">
                  Make one at{" "}
                  <a href={`https://${p.key_page}`} target="_blank" rel="noreferrer">
                    {p.key_page}
                  </a>
                  .
                </span>
              )}
            </label>
          )}
          {showUrl ? (
            <label className="prov-field">
              <span className="set-label">Address</span>
              <input
                className="set-input prov-input"
                type="url"
                spellCheck={false}
                placeholder="http://localhost:1234/v1"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
              <span className="set-help">
                The server's OpenAI-compatible address; it usually ends in /v1.
                {kind === "ollama" && " When the studio runs in Docker, use http://host.docker.internal:11434/v1."}
              </span>
            </label>
          ) : (
            <button className="link-btn" onClick={() => setOtherUrl(true)}>
              Use another address
            </button>
          )}
          {p.key_env !== "" && (
            <p className="set-help">
              Or set <code>{p.key_env}</code> in the server's environment instead.
            </p>
          )}
          <div className="prov-go">
            <button className="primary" disabled={!ready || busy} onClick={connect}>
              {busy ? (
                <>
                  <span className="mini-spin" /> Testing the key…
                </>
              ) : (
                "Test and connect"
              )}
            </button>
          </div>
          {said && (
            <p className={said.ok ? "set-st ok" : "set-st err"} role={said.ok ? "status" : "alert"}>
              {said.text}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ── the models each kind of work uses ────────────────────────────────────────

/** Every role with the model it uses, each changeable to any model a
 * connected provider offers. A role no provider can do says so. */
export function RolesList() {
  const [doc, setDoc] = useState<Providers | null>(null);
  const [err, setErr] = useState("");
  const [models, setModels] = useState<string[]>([]);

  const load = useCallback(() => {
    providersLoad().then(
      (d) => {
        setDoc(d);
        // Every connected provider's models, for the fields to suggest.
        void Promise.all(d.providers.map((p) => providerModels(p.id).catch(() => [] as string[]))).then((lists) =>
          setModels(lists.flat()),
        );
      },
      (e: unknown) => setErr(errText(e)),
    );
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (err !== "") {
    return (
      <div className="set-note err" role="alert">
        The models could not be loaded: {err}
      </div>
    );
  }
  if (doc === null) {
    return (
      <div className="set-note">
        <span className="mini-spin" /> Loading models…
      </div>
    );
  }
  return (
    <div className="prov-roles">
      <datalist id="prov-models">
        {models.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>
      {doc.roles.map((r) => (
        <RoleRow key={r.role} role={r} onSaved={load} />
      ))}
    </div>
  );
}

function RoleRow({ role, onSaved }: { role: Role; onSaved: () => void }) {
  const [draft, setDraft] = useState(role.model);
  const [seen, setSeen] = useState(role.model);
  const [st, setSt] = useState<{ ok: boolean; text: string } | null>(null);
  // The field follows the model in force when it changes under it.
  if (seen !== role.model) {
    setSeen(role.model);
    setDraft(role.model);
  }

  const save = () => {
    const v = draft.trim();
    if (v === role.model) return;
    setSt({ ok: true, text: "Saving…" });
    // A role can fill more than one setting: maps and notes share one.
    Promise.all(role.keys.map((k) => settingsSet(k, v))).then(
      () => {
        setSt({ ok: true, text: "Saved." });
        void reloadSettings();
        onSaved();
      },
      (e: unknown) => setSt({ ok: false, text: errText(e) }),
    );
  };

  return (
    <div className="set-row">
      <div className="set-text">
        <div className="set-label">{role.label}</div>
        <div className="set-help">
          {role.model === ""
            ? "No connected provider can do this yet. Connect one that can, such as OpenRouter."
            : role.chosen
              ? `${role.model_name}, as chosen.`
              : `${role.model_name}, suggested by your provider.`}
        </div>
        {st && <div className={st.ok ? "set-st ok" : "set-st err"}>{st.text}</div>}
      </div>
      <div className="set-ctl">
        <input
          className="set-input"
          list="prov-models"
          spellCheck={false}
          aria-label={`${role.label} model`}
          placeholder="Model id"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={save}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
          }}
        />
      </div>
    </div>
  );
}

// ── Settings › AI providers ──────────────────────────────────────────────────

export function ProvidersPanel() {
  const [doc, setDoc] = useState<Providers | null>(null);
  const [err, setErr] = useState("");
  const [adding, setAdding] = useState(false);
  const [round, setRound] = useState(0);

  const load = useCallback(() => {
    providersLoad().then(setDoc, (e: unknown) => setErr(errText(e)));
  }, []);

  useEffect(() => {
    load();
  }, [load, round]);

  const changed = () => {
    setRound((n) => n + 1);
    void reloadSettings();
  };

  if (err !== "" && doc === null) {
    return (
      <div className="set-note err" role="alert">
        The providers could not be loaded: {err}
        <div>
          <button className="est-retry" onClick={load}>
            Try again
          </button>
        </div>
      </div>
    );
  }
  if (doc === null) {
    return (
      <div className="set-note">
        <span className="mini-spin" /> Loading providers…
      </div>
    );
  }
  return (
    <>
      <div className="set-grp">
        <h4 className="set-gh">Connected</h4>
        {doc.providers.length === 0 && <p className="set-gnote">No provider is connected yet.</p>}
        {doc.providers.map((p) => (
          <ProviderRow
            key={p.id}
            p={p}
            onRemoved={() => {
              changed();
              recheckSetup();
            }}
          />
        ))}
        {adding ? (
          <div className="prov-add">
            <ProviderForm
              onConnected={() => {
                setAdding(false);
                changed();
              }}
            />
            <button className="ghost" onClick={() => setAdding(false)}>
              Cancel
            </button>
          </div>
        ) : (
          <div className="prov-go">
            <button onClick={() => setAdding(true)}>
              <Icon name="plus-lg" />
              Add a provider
            </button>
          </div>
        )}
      </div>
      <div className="set-grp">
        <h4 className="set-gh">Models for each kind of work</h4>
        <RolesList key={round} />
      </div>
    </>
  );
}

function ProviderRow({ p, onRemoved }: { p: Provider; onRemoved: () => void }) {
  const [check, setCheck] = useState<KeyCheck | null>(p.check ?? null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [confirm, setConfirm] = useState(false);

  const test = () => {
    setBusy(true);
    setErr("");
    providerRecheck(p.id).then(
      (c) => {
        setBusy(false);
        setCheck(c);
      },
      (e: unknown) => {
        setBusy(false);
        setErr(errText(e));
      },
    );
  };

  const remove = () => {
    setBusy(true);
    providerRemove(p.id).then(onRemoved, (e: unknown) => {
      setBusy(false);
      setErr(errText(e));
    });
  };

  const where = p.source === "env" ? "Set in the server's environment" : "Added here";
  return (
    <div className="set-row">
      <div className="set-text">
        <div className="set-label">
          {p.label}
          {p.primary && <span className="prov-tag">Main</span>}
        </div>
        <div className="set-help">
          {where}
          {p.key_hint !== "" && ` · key ${p.key_hint}`} · {p.base_url}
        </div>
        {check && (
          <div className={check.ok ? "set-st ok" : "set-st err"} role={check.ok ? "status" : "alert"}>
            {check.sentence}
          </div>
        )}
        {err !== "" && (
          <div className="set-st err" role="alert">
            {err}
          </div>
        )}
      </div>
      <div className="set-ctl">
        {confirm ? (
          <>
            <button className="primary danger" disabled={busy} onClick={remove}>
              Remove {p.label}
            </button>
            <button className="ghost" onClick={() => setConfirm(false)}>
              Cancel
            </button>
          </>
        ) : (
          <>
            <button disabled={busy} onClick={test}>
              {busy ? <span className="mini-spin" /> : <Icon name="arrow-clockwise" />}
              Test again
            </button>
            {p.source === "app" && (
              <button className="ghost bad" disabled={busy} onClick={() => setConfirm(true)}>
                <Icon name="trash" />
                Remove
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
