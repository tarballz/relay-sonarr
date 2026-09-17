import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, tierClass } from "../api.js";
import { TierBadge, Spinner, ErrorState } from "../components/Shared.jsx";
import { useToast } from "../components/ui/Toast.jsx";

export default function Settings() {
  const { data: instances, isLoading, isError, error, refetch } = useQuery({ queryKey: ["instances"], queryFn: api.instances });
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const qc = useQueryClient();
  const toast = useToast();

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
        <ChainEditor instances={instances} initial={settings.fallbackChains || {}} toast={toast} qc={qc} />
      )}

      <DefaultsEditor toast={toast} qc={qc} />
    </>
  );
}

// --- Fallback chain editor --------------------------------------------------
// Edits the {startId: [{instanceId, profile}]} map and saves it as a DB override.
function ChainEditor({ instances, initial, toast, qc }) {
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
      toast("Fallback chains saved.");
    } catch (e) {
      toast(e.message, true);
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
function DefaultsEditor({ toast, qc }) {
  const { data } = useQuery({ queryKey: ["defaults"], queryFn: api.defaults });
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (data) setForm({
      allowSplit: data.allowSplit ?? true,
      escalateAfterDays: data.escalateAfterDays ?? 0,
      stalledDays: data.stalledDays ?? 1,
      minSeeders: data.minSeeders ?? 5,
      seederGrab: data.seederGrab ?? true,
      deadHours: data.deadHours ?? 6,
      seederRelaxAfterDays: data.seederRelaxAfterDays ?? 3,
      nearCompletePct: data.nearCompletePct ?? 95,
      regrabCap: data.regrabCap ?? 5,
    });
  }, [JSON.stringify(data)]);

  if (!form) return null;

  async function save() {
    setBusy(true);
    try {
      await api.setDefaults({ ...data, ...form });
      qc.invalidateQueries({ queryKey: ["defaults"] });
      toast("Defaults saved.");
    } catch (e) {
      toast(e.message, true);
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
        <label className="check-row" style={{ marginBottom: 14 }}>
          <input
            type="checkbox"
            checked={form.seederGrab}
            onChange={(e) => setForm((f) => ({ ...f, seederGrab: e.target.checked }))}
          />
          Grab the best-seeded release directly (falls back to a normal search)
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
        <label className="field" style={{ maxWidth: 260, marginTop: 14 }}>
          Remove torrents stuck at 0% after (days)
          <input
            className="input"
            type="number"
            min="1"
            value={form.stalledDays}
            onChange={(e) => setForm((f) => ({ ...f, stalledDays: Number(e.target.value) }))}
          />
        </label>
        <label className="field" style={{ maxWidth: 260, marginTop: 14 }}>
          Treat a torrent with no metadata or 0 seeders as dead after (hours)
          <input
            className="input"
            type="number"
            min="1"
            value={form.deadHours}
            onChange={(e) => setForm((f) => ({ ...f, deadHours: Number(e.target.value) }))}
          />
        </label>
        <div className="muted" style={{ fontSize: 12, marginTop: 6, maxWidth: 460 }}>
          Needs a download client configured in config.yaml — Sonarr reports a dead
          torrent as "ok", so only the client can tell one apart from a slow one.
          Without it this field has no effect and the day-based threshold applies.
        </div>
        <label className="field" style={{ maxWidth: 260, marginTop: 14 }}>
          Never discard a download past (% complete)
          <input
            className="input"
            type="number"
            min="1"
            max="100"
            value={form.nearCompletePct}
            onChange={(e) => setForm((f) => ({ ...f, nearCompletePct: Number(e.target.value) }))}
          />
        </label>
        <div className="muted" style={{ fontSize: 12, marginTop: 6, maxWidth: 460 }}>
          A removal blocklists the release, so the replacement restarts from zero.
          Downloads above this mark keep a 3-day grace instead. Set to 100 to disable.
        </div>
        <label className="field" style={{ maxWidth: 260, marginTop: 14 }}>
          Replacement grabs per tick (0 = let Sonarr re-search)
          <input
            className="input"
            type="number"
            min="0"
            value={form.regrabCap}
            onChange={(e) => setForm((f) => ({ ...f, regrabCap: Number(e.target.value) }))}
          />
        </label>
        <div className="muted" style={{ fontSize: 12, marginTop: 6, maxWidth: 460 }}>
          After removing a dead torrent Relay searches and grabs the best-seeded
          replacement itself, because Sonarr's own re-search ranks by quality and
          often re-picks another dead release. Each one costs a slow interactive
          search, so the budget is small.
        </div>
        <label className="field" style={{ maxWidth: 260, marginTop: 14 }}>
          Minimum torrent seeders to count a release (0 = off)
          <input
            className="input"
            type="number"
            min="0"
            value={form.minSeeders}
            onChange={(e) => setForm((f) => ({ ...f, minSeeders: Number(e.target.value) }))}
          />
        </label>
        <div className="muted" style={{ fontSize: 12, marginTop: 6, maxWidth: 460 }}>
          Applies to Relay's availability checks and tier decisions only — Sonarr's own
          RSS/automatic grabs don't see it. Set "Minimum Seeders" on your indexers in
          Sonarr/Prowlarr as the enforcement backstop.
        </div>
        <label className="field" style={{ maxWidth: 260, marginTop: 14 }}>
          Accept the best available release after (days wanted)
          <input
            className="input"
            type="number"
            min="0"
            value={form.seederRelaxAfterDays}
            onChange={(e) =>
              setForm((f) => ({ ...f, seederRelaxAfterDays: Number(e.target.value) }))
            }
          />
        </label>
        <div className="muted" style={{ fontSize: 12, marginTop: 6, maxWidth: 460 }}>
          Old back-catalogue legitimately tops out at a handful of seeders, so the
          floor above gives way once an episode has waited this long. 0 never relaxes.
        </div>
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
