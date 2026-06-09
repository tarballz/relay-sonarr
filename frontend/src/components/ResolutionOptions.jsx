import { motion } from "framer-motion";
import { tierClass } from "../api.js";
import { Spinner } from "./Shared.jsx";

// Turn an availability result into a one-line "why nothing qualified" summary.
export function reasonText(av) {
  if (!av) return "";
  const total = av.totalReleases || 0;
  if (total === 0) return "No releases found at all.";
  const parts = (av.rejectionSummary || []).map((r) => `${r.count} ${r.reason}`);
  return `${total} found, none qualified${parts.length ? " — " + parts.join(", ") : ""}.`;
}

// The roadblock menu: render the backend-supplied `options` as choice cards.
// Shared by the live AddDialog and the Operations "Resolve" flow. Each click
// hands the whole option back to `onDispatch`, which routes by `option.action`.
// `busyId` is the id of the option currently running (shows a spinner on it).
export default function ResolutionOptions({ data, series, onDispatch, busyId }) {
  const options = data?.options || [];
  const exhausted = data?.status === "exhausted";
  return (
    <motion.div
      className="resolution"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
    >
      <div className="resolution-head">
        <span className="glyph">{exhausted ? "∅" : "⚠"}</span>
        <div>
          <strong>
            {exhausted
              ? `No release on any tier for “${series.title}.”`
              : `No qualifying release for “${series.title}” yet.`}
          </strong>
          {data?.availability && (
            <p className="resolution-reason">{reasonText(data.availability)}</p>
          )}
          <p className="resolution-prompt">Choose how to resolve it:</p>
        </div>
      </div>

      <div className="resolution-opts">
        {options.map((o) => {
          const tier = o.instanceId ? tierClass(o.instanceId) : "";
          const running = busyId === o.id;
          return (
            <button
              key={o.id}
              className={`resolution-opt ${tier} ${o.action === "remove" ? "danger" : ""}`}
              disabled={!o.enabled || (busyId && !running)}
              onClick={() => onDispatch(o)}
            >
              <span className="resolution-opt-label">
                {running && <Spinner />} {o.label}
              </span>
              <span className="resolution-opt-desc">
                {o.enabled ? o.description : o.disabledReason || o.description}
              </span>
            </button>
          );
        })}
      </div>
    </motion.div>
  );
}
