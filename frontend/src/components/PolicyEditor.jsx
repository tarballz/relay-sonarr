import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { useQueryClient } from "@tanstack/react-query";
import { api, tierClass } from "../api.js";
import { Spinner } from "./Shared.jsx";
import { useDialog } from "./useDialog.js";

// Per-series policy editor: preferred tier, per-episode split toggle, ordered
// fallback tiers (each optionally time-gated), pause/resume, and "reconcile now".
// Reads the effective policy on open (stored or derived) and saves overrides.
export default function PolicyEditor({ series, instances, onClose, onToast }) {
  const [policy, setPolicy] = useState(null);
  const [paused, setPaused] = useState(false);
  const [busy, setBusy] = useState(false);
  const qc = useQueryClient();
  const dialogRef = useDialog(onClose);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await api.policy(series.tvdbId);
        if (alive) {
          setPolicy(res.policy);
          setPaused(res.paused);
        }
      } catch (e) {
        onToast?.(e.message, true);
        onClose();
      }
    })();
    return () => {
      alive = false;
    };
  }, [series.tvdbId]);

  function patch(p) {
    setPolicy((cur) => ({ ...cur, ...p }));
  }
  function setStep(i, p) {
    setPolicy((cur) => ({
      ...cur,
      fallbacks: cur.fallbacks.map((s, j) => (j === i ? { ...s, ...p } : s)),
    }));
  }
  function addStep() {
    const first = instances[0]?.id;
    patch({ fallbacks: [...(policy.fallbacks || []), { instanceId: first, profile: null, afterDays: 0 }] });
  }
  function removeStep(i) {
    patch({ fallbacks: policy.fallbacks.filter((_, j) => j !== i) });
  }

  function invalidate() {
    qc.invalidateQueries({ queryKey: ["library-status"] });
    qc.invalidateQueries({ queryKey: ["series"] });
  }

  async function save() {
    setBusy(true);
    try {
      await api.setPolicy(series.tvdbId, policy);
      invalidate();
      onToast?.(`Saved policy for “${series.title}”.`);
      onClose();
    } catch (e) {
      onToast?.(e.message, true);
      setBusy(false);
    }
  }

  async function togglePause() {
    setBusy(true);
    try {
      if (paused) await api.resumeSeries(series.tvdbId);
      else await api.pauseSeries(series.tvdbId);
      setPaused(!paused);
      invalidate();
    } catch (e) {
      onToast?.(e.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function reconcileNow() {
    setBusy(true);
    try {
      const r = await api.reconcileSeries(series.tvdbId);
      const acted = (r.searchedOnDesired || 0) + (r.filled || 0);
      onToast?.(acted ? `Reconciling “${series.title}” — ${acted} episode(s) actioned.`
                      : `“${series.title}” is up to date — nothing to do.`);
      invalidate();
      onClose();
    } catch (e) {
      onToast?.(e.message, true);
      setBusy(false);
    }
  }

  return (
    <div className="scrim" onClick={onClose}>
      <motion.div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Policy for ${series.title}`}
        tabIndex={-1}
        className="dialog"
        onClick={(e) => e.stopPropagation()}
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.2, ease: [0.2, 0.7, 0.2, 1] }}
      >
        <div className="dialog-head">
          <h2>{series.title}</h2>
          <div className="meta" style={{ marginTop: 6 }}>
            <span className="mono">tvdb {series.tvdbId}</span>
            {paused && <span style={{ color: "var(--ink-faint)" }}>· paused</span>}
          </div>
        </div>

        <div className="dialog-body">
          {!policy ? (
            <div style={{ padding: "8px 0" }}><Spinner /></div>
          ) : (
            <>
              <div className="section-label">Preferred tier</div>
              <select
                className="input"
                value={policy.preferredTier}
                onChange={(e) => patch({ preferredTier: e.target.value })}
              >
                {instances.map((i) => (
                  <option key={i.id} value={i.id}>{i.name}</option>
                ))}
              </select>

              <label className="check-row">
                <input
                  type="checkbox"
                  checked={policy.allowSplit}
                  onChange={(e) => patch({ allowSplit: e.target.checked })}
                />
                Allow per-episode split across tiers
              </label>

              <div className="section-label">Fallback tiers</div>
              {(policy.fallbacks || []).map((s, i) => (
                <div key={i} className={`policy-step ${tierClass(s.instanceId)}`}>
                  <select
                    className="input"
                    value={s.instanceId}
                    onChange={(e) => setStep(i, { instanceId: e.target.value })}
                  >
                    {instances.map((inst) => (
                      <option key={inst.id} value={inst.id}>{inst.name}</option>
                    ))}
                  </select>
                  <input
                    className="input"
                    placeholder="profile (optional)"
                    value={s.profile || ""}
                    onChange={(e) => setStep(i, { profile: e.target.value || null })}
                  />
                  <input
                    className="input policy-days"
                    type="number"
                    min="0"
                    title="Only spill here after this many days"
                    value={s.afterDays ?? 0}
                    onChange={(e) => setStep(i, { afterDays: Number(e.target.value) })}
                  />
                  <span style={{ fontSize: 12, color: "var(--ink-faint)" }}>days</span>
                  <button className="row-del" onClick={() => removeStep(i)}>✕</button>
                </div>
              ))}
              <button className="btn ghost" onClick={addStep} style={{ alignSelf: "flex-start" }}>
                + Add fallback tier
              </button>
            </>
          )}
        </div>

        <div className="dialog-foot" style={{ justifyContent: "space-between" }}>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn ghost" onClick={togglePause} disabled={busy || !policy}>
              {paused ? "Resume" : "Pause"}
            </button>
            <button className="btn ghost" onClick={reconcileNow} disabled={busy || !policy}>
              Reconcile now
            </button>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn ghost" onClick={onClose} disabled={busy}>Cancel</button>
            <button className="btn primary" onClick={save} disabled={busy || !policy}>
              {busy ? <Spinner /> : "Save"}
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  );
}
