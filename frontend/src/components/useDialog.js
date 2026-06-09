import { useEffect, useRef } from "react";

// Accessibility for modal dialogs: Escape-to-close, a Tab focus-trap, and moving
// focus into the dialog on open. Returns a ref to attach to the dialog element
// (which should also get role="dialog" aria-modal="true" tabIndex={-1}).
//
// A module-level stack makes nesting correct: only the TOP-most open dialog
// responds to Escape/Tab, so closing a nested confirm doesn't also close its
// parent.
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

const dialogStack = [];

export function useDialog(onClose) {
  const ref = useRef(null);
  // Keep the latest onClose without re-running the effect when its identity
  // changes (callers usually pass an inline arrow).
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const token = {};
    dialogStack.push(token);
    const node = ref.current;

    const focusables = () =>
      node
        ? Array.from(node.querySelectorAll(FOCUSABLE)).filter((el) => el.offsetParent !== null)
        : [];

    // Move focus into the dialog so keyboard users start inside it.
    const initial = focusables();
    (initial[0] || node)?.focus?.();

    function onKey(e) {
      if (dialogStack[dialogStack.length - 1] !== token) return; // only the topmost
      if (e.key === "Escape") {
        e.preventDefault();
        onCloseRef.current && onCloseRef.current();
        return;
      }
      if (e.key === "Tab") {
        const items = focusables();
        if (items.length === 0) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }

    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      const i = dialogStack.indexOf(token);
      if (i >= 0) dialogStack.splice(i, 1);
    };
  }, []);

  return ref;
}
