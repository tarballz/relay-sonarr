import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { Spinner, Empty, ErrorState } from "../components/Shared.jsx";
import ResolveDialog from "../components/ResolveDialog.jsx";

const PHASE_LABEL = {
  add: "Add", refresh: "Episodes", search: "Search", grab: "Download",
  move: "Move", swap: "Profile", gaps: "Gaps", split: "Split",
  fallback: "Decide", exhausted: "Result",
};

function outcome(op) {
  if (op.error) return { label: "error", cls: "err" };
  const s = op.result?.status;
  // Reconciler passes carry no status field — summarize the work they did.
  if (op.kind === "reconcile") {
    if (!op.finishedAt) return { label: "running", cls: "run" };
    const r = op.result || {};
    const n = (r.searchedOnDesired || 0) + (r.filled || 0);
    return { label: n ? `reconciled ${n}` : "up to date", cls: n ? "ok" : "run" };
  }
  if (!s) return { label: "running", cls: "run" };
  if (s === "added" || s === "placed") return { label: "downloading", cls: "ok" };
  if (s === "split") return { label: "split", cls: "ok" };
  if (s === "fallback_suggested") return { label: "awaiting choice", cls: "warn" };
  if (s === "exhausted") return { label: "no release", cls: "warn" };
  return { label: s, cls: "run" };
}

// A finished op still sitting at a roadblock can be resolved from here.
function actionable(op) {
  const s = op.result?.status;
  return !!op.finishedAt && (s === "fallback_suggested" || s === "exhausted");
}

function when(iso) {
  if (!iso) return "";
  // Show local HH:MM:SS — the timeline detail matters more than the date here.
  const d = new Date(iso);
  return d.toLocaleTimeString();
}

function ago(seconds) {
  if (seconds == null) return "never";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

// Health-at-a-glance card for the autonomous loop. Green when healthy, amber on
// recent errors, red when stale/wedged, grey when disabled.
function ReconcilerHealth() {
  const { data: s, isError } = useQuery({
    queryKey: ["reconciler-status"],
    queryFn: api.reconcilerStatus,
    refetchInterval: 4000,
  });
  if (isError || !s) return null;
  let state = "ok";
  let label = "Reconciler healthy";
  if (!s.enabled) { state = "off"; label = "Reconciler disabled"; }
  else if (!s.healthy) { state = "err"; label = "Reconciler stalled"; }
  else if (s.consecutiveFailures > 0 || s.lastError) { state = "warn"; label = "Reconciler — recent error"; }

  return (
    <div className={`panel rec-health rec-${state}`}>
      <span className="rec-dot" />
      <div className="rec-body">
        <strong>{label}</strong>
        <p className="rec-meta">
          {s.enabled
            ? <>last tick {ago(s.secondsSinceLastTick)} · {s.lastTickActions} action{s.lastTickActions === 1 ? "" : "s"} · {s.totalTicks} tick{s.totalTicks === 1 ? "" : "s"}{s.lastTickDurationS != null ? ` · ${s.lastTickDurationS.toFixed(1)}s` : ""}</>
            : "Set RECONCILER_ENABLED=true to let it run."}
        </p>
        {s.lastError && <p className="rec-err-text">last error: {s.lastError}</p>}
      </div>
    </div>
  );
}

export default function Operations() {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["operations"],
    queryFn: api.operations,
    refetchInterval: 4000,
  });
  const [open, setOpen] = useState({});
  const [resolving, setResolving] = useState(null);
  const [source, setSource] = useState("all"); // all | reconciler | user
  const [toast, setToast] = useState(null);
  const showToast = (msg, err) => {
    setToast({ msg, err });
    setTimeout(() => setToast(null), 4200);
  };

  const ops = (data || []).filter(
    (op) => source === "all" || (op.source || "user") === source
  );

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Operations</h1>
          <p className="page-sub">Behind-the-scenes log of smart-adds, fallbacks, and the autonomous reconciler.</p>
        </div>
        <div className="health-row">
          {[["all", "All"], ["reconciler", "Auto"], ["user", "Manual"]].map(([k, label]) => (
            <button
              key={k}
              className={`btn ${source === k ? "primary" : "ghost"}`}
              onClick={() => setSource(k)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <ReconcilerHealth />

      {isLoading && <div style={{ padding: 24 }}><Spinner /></div>}
      {isError && <ErrorState what="operations" error={error} onRetry={refetch} />}
      {data && ops.length === 0 && (
        <Empty big="No operations yet">Add a show or let the reconciler run — the play-by-play shows up here.</Empty>
      )}

      <div className="op-list">
        {ops.map((op) => {
          const o = outcome(op);
          const isOpen = open[op.id];
          return (
            <motion.div key={op.id} className="op-card" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}>
              <div className="op-row">
                <button className="op-summary" onClick={() => setOpen((s) => ({ ...s, [op.id]: !s[op.id] }))}>
                  <span className={`op-badge ${o.cls}`}>{o.label}</span>
                  <span className="op-title">{op.title}</span>
                  <span className="op-kind num">{op.kind}</span>
                  <span className="op-time num">{when(op.startedAt)}</span>
                  <span className="op-caret">{isOpen ? "▾" : "▸"}</span>
                </button>
                {actionable(op) && (
                  <button className="btn t1080p op-resolve" onClick={() => setResolving(op)}>
                    Resolve
                  </button>
                )}
              </div>
              <AnimatePresence initial={false}>
                {isOpen && (
                  <motion.div
                    className="op-steps"
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: "auto", opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.2 }}
                  >
                    {op.steps.map((st, i) => (
                      <div key={i} className="op-step">
                        <span className="op-step-phase">{PHASE_LABEL[st.phase] || st.phase}</span>
                        <span className="op-step-msg">{st.message}</span>
                      </div>
                    ))}
                    {op.error && <div className="op-step err">⚠ {op.error}</div>}
                  </motion.div>
                )}
              </AnimatePresence>
            </motion.div>
          );
        })}
      </div>

      {resolving && (
        <ResolveDialog
          series={{ title: resolving.title, tvdbId: resolving.tvdbId }}
          initial={resolving.result}
          onClose={() => setResolving(null)}
          onToast={showToast}
        />
      )}
      {toast && <div className={`toast ${toast.err ? "err" : ""}`}>{toast.msg}</div>}
    </>
  );
}
