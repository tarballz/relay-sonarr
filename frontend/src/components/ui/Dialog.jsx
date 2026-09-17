import { motion } from "framer-motion";
import { useDialog } from "../useDialog.js";
import { Spinner } from "../Shared.jsx";

export const DIALOG_TRANSITION = { duration: 0.2, ease: [0.2, 0.7, 0.2, 1] };

// The one dialog skeleton: scrim, focus-trapped panel, head/body/foot. The close
// button is always rendered — a dialog must never leave the user without an exit.
export default function Dialog({ title, subtitle, label, onClose, busy = false, footer, maxWidth, children }) {
  const dialogRef = useDialog(onClose);

  return (
    <div className="scrim" onClick={onClose}>
      <motion.div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={label || title}
        tabIndex={-1}
        className="dialog"
        style={maxWidth ? { maxWidth } : undefined}
        onClick={(e) => e.stopPropagation()}
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={DIALOG_TRANSITION}
      >
        <div className="dialog-head">
          <div className="dialog-head-text">
            <h2>{title}</h2>
            {subtitle ? <div className="meta dialog-sub">{subtitle}</div> : null}
          </div>
          {busy ? <Spinner /> : null}
          <button type="button" className="dialog-close" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="dialog-body">{children}</div>
        {footer ? <div className="dialog-foot">{footer}</div> : null}
      </motion.div>
    </div>
  );
}
