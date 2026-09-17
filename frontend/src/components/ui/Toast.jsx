import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef } from "react";
import { DEFAULT_TTL, initialToasts, toastReducer } from "../../lib/toast.js";

const ToastContext = createContext(() => {});

export function useToast() {
  return useContext(ToastContext);
}

export function ToastProvider({ children }) {
  const [state, dispatch] = useReducer(toastReducer, initialToasts);
  const timers = useRef(new Map());

  const dismiss = useCallback((id) => {
    const handle = timers.current.get(id);
    if (handle) {
      clearTimeout(handle);
      timers.current.delete(id);
    }
    dispatch({ type: "dismiss", id });
  }, []);

  // Ids come from a ref, not from state: two toasts raised in the same tick must
  // get different ids, or one timer would dismiss the other's toast.
  const lastId = useRef(0);

  const toast = useCallback(
    (msg, err) => {
      const id = (lastId.current += 1);
      dispatch({ type: "push", id, msg, err });
      timers.current.set(id, setTimeout(() => dismiss(id), DEFAULT_TTL));
      return id;
    },
    [dismiss],
  );

  // Every pending timer dies with the provider: a late timer must never fire
  // into an unmounted tree (the old per-page toasts never cleared theirs).
  useEffect(
    () => () => {
      timers.current.forEach((handle) => clearTimeout(handle));
      timers.current.clear();
    },
    [],
  );

  const value = useMemo(() => toast, [toast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-host" role="status" aria-live="polite">
        {state.items.map((t) => (
          <div key={t.id} className={`toast ${t.err ? "err" : ""}`} onClick={() => dismiss(t.id)}>
            {t.msg}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
