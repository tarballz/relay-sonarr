import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, tierClass } from "../api.js";
import { TierBadge, Spinner, ErrorState } from "../components/Shared.jsx";

export default function Settings() {
  const { data: instances, isLoading, isError, error, refetch } = useQuery({ queryKey: ["instances"], queryFn: api.instances });
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const qc = useQueryClient();

  const [toast, setToast] = useState(null);
  const showToast = (msg, err) => {
    setToast({ msg, err });
    setTimeout(() => setToast(null), 4000);
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Settings</h1>
          <p className="page-sub">Configured instances, smart-add fallback routing, and reconciler defaults.</p>
        </div>
      </div>

      {isLoading && <div style={{ padding: 24 }}><Spinner /></div>}
      {isError && <ErrorState what="settings" error={error} onRetry={refetch} />}

      <div className="section-label">Instances</div>
      <div className="result-grid" style={{ marginBottom: 32 }}>
        {(instances || []).map((i) => (
          <div key={i.id} className="panel" style={{ padding: "16px 18px", display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
            <span className={`pill ${i.online ? "online" : "offline"}`}><span className="led" /></span>
            <TierBadge instanceId={i.id} name={i.name} />
            <span className="num" style={{ color: "var(--ink-dim)" }}>{i.url}</span>
            <span style={{ marginLeft: "auto", color: i.online ? "var(--ok)" : "var(--danger)", fontSize: 13 }}>
              {i.online ? `online · v${i.version || "?"}` : "offline"}
            </span>
          </div>
        ))}
        <p style={{ color: "var(--ink-faint)", fontSize: 12.5, margin: "2px 2px 0" }}>
          Instances and their API keys are defined in <span className="num">config.yaml</span> + <span className="num">.env</span> (keys never leave the server).
        </p>
      </div>

      {instances && settings && (
        <ChainEditor instances={instances} initial={settings.fallbackChains || {}} onToast={showToast} qc={qc} />
      )}

      <DefaultsEditor onToast={showToast} qc={qc} />

      {toast && <div className={`toast ${toast.err ? "err" : ""}`}>{toast.msg}</div>}
    </>
  );
}

// --- Fallback chain editor --------------------------------------------------
// Edits the {startId: [{instanceId, profile}]} map and saves it as a DB override.
function ChainEditor({ instances, initial, onToast, qc }) {
  const [chains, setChains] = useState(() => clone(initial));
  const [busy, setBusy] = useState(false);
  // Re-seed if the server copy changes (e.g. another tab saved).
  useEffect(() => { setChains(clone(initial)); }, [JSON.stringify(initial)]);

  const dirty = JSON.stringify(chains) !== JSON.stringify(strip(initial));

  function setStep(start, i, patch) {
    setChains((c) => ({ ...c, [start]: c[start].map((s, j) => (j === i ? { ...s, ...patch } : s)) }));
  }
  function addStep(start) {
    setChains((c) => ({ ...c, [start]: [...(c[start] || []), { instanceId: instances[0]?.id, profile: null }] }));
  }
  function removeStep(start, i) {
    setChains((c) => ({ ...c, [start]: c[start].filter((_, j) => j !== i) }));
  }
  function move(start, i, dir) {
    setChains((c) => {
      const arr = [...c[start]];
      const j = i + dir;
      if (j < 0 || j >= arr.length) return c;
      [arr[i], arr[j]] = [arr[j], arr[i]];
      return { ...c, [start]: arr };
    });
  }

  async function save() {
    setBusy(true);
    try {
      // Drop empty chains so the override map stays clean.
      const payload = Object.fromEntries(
        Object.entries(chains).filter(([, steps]) => steps.length > 0)
      );
      await api.setFallbackChains(payload);
      qc.invalidateQueries({ queryKey: ["settings"] });
      onToast("Fallback chains saved.");
    } catch (e) {
      onToast(e.message, true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="section-label">Smart-add fallback chains</div>
      <div className="panel" style={{ padding: "18px 20px", marginBottom: 32 }}>
        {instances.map((inst) => {
          const steps = chains[inst.id] || [];
          return (
            <div key={inst.id} style={{ marginBottom: 18 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                <TierBadge instanceId={inst.id} name={inst.name} />
                <span style={{ color: "var(--ink-faint)", fontSize: 12.5 }}>
                  {steps.length ? "tries, in order:" : "no fallback"}
                </span>
              </div>
              {steps.map((s, i) => (
                <div key={i} className={`policy-step ${tierClass(s.instanceId)}`} style={{ marginBottom: 8 }}>
                  <select
                    className="input"
                    aria-label="Fallback instance"
                    value={s.instanceId}
                    onChange={(e) => setStep(inst.id, i, { instanceId: e.target.value })}
                  >
                    {instances.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
                  </select>
                  <input
                    className="input"
                    placeholder="profile (optional)"
                    aria-label="Quality profile name"
                    value={s.profile || ""}
                    onChange={(e) => setStep(inst.id, i, { profile: e.target.value || null })}
                  />
                  <button className="row-del" title="Move up" aria-label="Move up" onClick={() => move(inst.id, i, -1)}>▲</button>
                  <button className="row-del" title="Move down" aria-label="Move down" onClick={() => move(inst.id, i, 1)}>▼</button>
                  <button className="row-del" title="Remove step" aria-label="Remove step" onClick={() => removeStep(inst.id, i)}>✕</button>
                </div>
              ))}
              <button className="btn ghost" onClick={() => addStep(inst.id)} style={{ marginTop: 2 }}>
                + Add fallback step
              </button>
            </div>
          );
        })}
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 6 }}>
          <button className="btn primary" disabled={busy || !dirty} onClick={save}>
            {busy ? <Spinner /> : "Save chains"}
          </button>
          <span style={{ color: "var(--ink-faint)", fontSize: 12.5 }}>
            Each step is tried only if the previous tier had no qualifying release. Saved to Relay’s DB (no restart).
          </span>
        </div>
      </div>
    </>
  );
}

// --- Reconciler defaults editor ---------------------------------------------
function DefaultsEditor({ onToast, qc }) {
  const { data } = useQuery({ queryKey: ["defaults"], queryFn: api.defaults });
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (data) setForm({ allowSplit: data.allowSplit ?? true, escalateAfterDays: data.escalateAfterDays ?? 0 });
  }, [JSON.stringify(data)]);

  if (!form) return null;

  async function save() {
    setBusy(true);
    try {
      await api.setDefaults({ ...data, ...form });
      qc.invalidateQueries({ queryKey: ["defaults"] });
      onToast("Defaults saved.");
    } catch (e) {
      onToast(e.message, true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="section-label">Reconciler defaults</div>
      <div className="panel" style={{ padding: "18px 20px" }}>
        <label className="check-row" style={{ marginBottom: 14 }}>
          <input
            type="checkbox"
            checked={form.allowSplit}
            onChange={(e) => setForm((f) => ({ ...f, allowSplit: e.target.checked }))}
          />
          Allow per-episode split across tiers by default
        </label>
        <label className="field" style={{ maxWidth: 260 }}>
          Escalate to fallback after (days)
          <input
            className="input"
            type="number"
            min="0"
            value={form.escalateAfterDays}
            onChange={(e) => setForm((f) => ({ ...f, escalateAfterDays: Number(e.target.value) }))}
          />
        </label>
        <div style={{ marginTop: 16 }}>
          <button className="btn primary" disabled={busy} onClick={save}>
            {busy ? <Spinner /> : "Save defaults"}
          </button>
        </div>
      </div>
    </>
  );
}

// Normalize the server chain shape to the editor's, dropping any extra keys.
function strip(chains) {
  return Object.fromEntries(
    Object.entries(chains || {}).map(([k, steps]) => [
      k,
      steps.map((s) => ({ instanceId: s.instanceId, profile: s.profile ?? null })),
    ])
  );
}
function clone(chains) {
  return JSON.parse(JSON.stringify(strip(chains)));
}
