import { motion } from "framer-motion";
import { tierClass } from "../api.js";
import {
  reasonText,
  roadblockGlyph,
  roadblockNote,
  roadblockTitle,
} from "../lib/resolution.js";
import { Spinner } from "./Shared.jsx";

// The roadblock menu: render the backend-supplied `options` as choice cards.
// Shared by the live AddDialog and the Operations "Resolve" flow. Each click
// hands the whole option back to `onDispatch`, which routes by `option.action`.
// `busyId` is the id of the option currently running (shows a spinner on it).
//
// All the wording lives in lib/resolution.js — see the note there on why it is
// worth testing.
export default function ResolutionOptions({ data, series, onDispatch, busyId }) {
  const options = data?.options || [];
  const note = roadblockNote(data);
  return (
    <motion.div
      className="resolution"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
    >
      <div className="resolution-head">
        <span className="glyph">{roadblockGlyph(data)}</span>
        <div>
          <strong>{roadblockTitle(data, series.title)}</strong>
          {data?.availability && (
            <p className="resolution-reason">{reasonText(data.availability)}</p>
          )}
          {note && <p className="resolution-note">{note}</p>}
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
              className={`resolution-opt ${tier} ${o.action === "remove" ? "danger" : ""}${
                o.recommended ? " recommended" : ""
              }`}
              disabled={!o.enabled || (busyId && !running)}
              onClick={() => onDispatch(o)}
            >
              <span className="resolution-opt-label">
                {running && <Spinner />} {o.label}
                {o.recommended && <span className="resolution-opt-tag">Recommended</span>}
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
