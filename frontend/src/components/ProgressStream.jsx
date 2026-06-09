import { motion, AnimatePresence } from "framer-motion";
import { Spinner } from "./Shared.jsx";

// Friendly label per phase emitted by the backend stream.
const PHASE_LABEL = {
  add: "Add to tier",
  refresh: "Load episodes",
  search: "Search indexers",
  grab: "Start download",
  move: "Move tier",
  swap: "Switch profile",
  gaps: "Find gaps",
  split: "Split tiers",
  fallback: "Decide next",
  exhausted: "Result",
};

// Collapse the raw event list (running→done per phase) into one row per phase,
// keeping arrival order and the latest status/message for each.
function toRows(steps) {
  const order = [];
  const byPhase = {};
  for (const s of steps) {
    if (!(s.phase in byPhase)) order.push(s.phase);
    byPhase[s.phase] = s;
  }
  return order.map((p) => byPhase[p]);
}

function ReasonBreakdown({ availability }) {
  const summary = availability?.rejectionSummary || [];
  if (!summary.length) return null;
  return (
    <div className="reasons">
      {summary.map((r, i) => (
        <span key={i} className="reason-chip">
          {r.count} {r.reason}
        </span>
      ))}
    </div>
  );
}

export default function ProgressStream({ steps }) {
  const rows = toRows(steps);
  return (
    <div className="progress-stream">
      <AnimatePresence initial={false}>
        {rows.map((s) => {
          const running = s.status === "running";
          const av = s.data?.availability;
          return (
            <motion.div
              key={s.phase}
              className={`pstep ${running ? "running" : "done"} ${s.phase}`}
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.18 }}
            >
              <span className="pstep-icon">
                {running ? <Spinner /> : <span className="check-mark">✓</span>}
              </span>
              <div className="pstep-body">
                <div className="pstep-head">
                  <span className="pstep-label">{PHASE_LABEL[s.phase] || s.phase}</span>
                  <span className="pstep-msg">{s.message}</span>
                </div>
                {s.phase === "search" && !running && av && !av.available && (
                  <ReasonBreakdown availability={av} />
                )}
              </div>
            </motion.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}
