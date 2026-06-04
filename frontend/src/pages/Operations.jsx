import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { Spinner, Empty } from "../components/Shared.jsx";

const PHASE_LABEL = {
  add: "Add", refresh: "Episodes", search: "Search", grab: "Download",
  move: "Move", swap: "Profile", fallback: "Decide", exhausted: "Result",
};

function outcome(op) {
  if (op.error) return { label: "error", cls: "err" };
  const s = op.result?.status;
  if (!s) return { label: "running", cls: "run" };
  if (s === "added" || s === "placed") return { label: "downloading", cls: "ok" };
  if (s === "fallback_suggested") return { label: "awaiting choice", cls: "warn" };
  if (s === "exhausted") return { label: "no release", cls: "warn" };
  return { label: s, cls: "run" };
}

function when(iso) {
  if (!iso) return "";
  // Show local HH:MM:SS — the timeline detail matters more than the date here.
  const d = new Date(iso);
  return d.toLocaleTimeString();
}

export default function Operations() {
  const { data, isLoading } = useQuery({
    queryKey: ["operations"],
    queryFn: api.operations,
    refetchInterval: 4000,
  });
  const [open, setOpen] = useState({});

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Operations</h1>
          <p className="page-sub">Behind-the-scenes log of recent smart-adds and fallback steps.</p>
        </div>
      </div>

      {isLoading && <div style={{ padding: 24 }}><Spinner /></div>}
      {data && data.length === 0 && (
        <Empty big="No operations yet">Add a show and the play-by-play shows up here.</Empty>
      )}

      <div className="op-list">
        {(data || []).map((op) => {
          const o = outcome(op);
          const isOpen = open[op.id];
          return (
            <motion.div key={op.id} className="op-card" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}>
              <button className="op-summary" onClick={() => setOpen((s) => ({ ...s, [op.id]: !s[op.id] }))}>
                <span className={`op-badge ${o.cls}`}>{o.label}</span>
                <span className="op-title">{op.title}</span>
                <span className="op-kind num">{op.kind}</span>
                <span className="op-time num">{when(op.startedAt)}</span>
                <span className="op-caret">{isOpen ? "▾" : "▸"}</span>
              </button>
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
    </>
  );
}
