import { useState } from "react";
import { motion } from "framer-motion";
import { Spinner } from "./Shared.jsx";
import { useDialog } from "./useDialog.js";

// Small modal for confirming a (usually destructive) action. If checkboxLabel is
// given, the checkbox value is passed to onConfirm.
export default function ConfirmDialog({
  title,
  message,
  confirmLabel = "Confirm",
  danger = false,
  checkboxLabel,
  onConfirm,
  onCancel,
  busy = false,
}) {
  const [checked, setChecked] = useState(false);
  const dialogRef = useDialog(onCancel);
  return (
    <div className="scrim" onClick={onCancel}>
      <motion.div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className="dialog"
        style={{ maxWidth: 440 }}
        onClick={(e) => e.stopPropagation()}
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.2, ease: [0.2, 0.7, 0.2, 1] }}
      >
        <div className="dialog-head">
          <h2>{title}</h2>
        </div>
        <div className="dialog-body">
          <p style={{ color: "var(--ink-dim)", margin: 0 }}>{message}</p>
          {checkboxLabel && (
            <label className="check-row">
              <input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} />
              {checkboxLabel}
            </label>
          )}
        </div>
        <div className="dialog-foot">
          <button className="btn ghost" onClick={onCancel} disabled={busy}>Cancel</button>
          <button
            className={`btn ${danger ? "danger" : "primary"}`}
            onClick={() => onConfirm(checked)}
            disabled={busy}
          >
            {busy ? <Spinner /> : confirmLabel}
          </button>
        </div>
      </motion.div>
    </div>
  );
}
