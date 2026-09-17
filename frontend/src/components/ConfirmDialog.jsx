import { useState } from "react";
import { Spinner } from "./Shared.jsx";
import Dialog from "./ui/Dialog.jsx";

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
  return (
    <Dialog
      title={title}
      onClose={onCancel}
      maxWidth={440}
      footer={
        <>
          <button className="btn ghost" onClick={onCancel} disabled={busy}>Cancel</button>
          <button
            className={`btn ${danger ? "danger" : "primary"}`}
            onClick={() => onConfirm(checked)}
            disabled={busy}
          >
            {busy ? <Spinner /> : confirmLabel}
          </button>
        </>
      }
    >
      <p style={{ color: "var(--ink-dim)", margin: 0 }}>{message}</p>
      {checkboxLabel && (
        <label className="check-row">
          <input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} />
          {checkboxLabel}
        </label>
      )}
    </Dialog>
  );
}
