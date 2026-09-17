import { useCallback, useEffect, useReducer, useRef } from "react";
import { flowReducer, initialFlow, isBusy, showFooter } from "../../lib/flow.js";
import { useToast } from "./Toast.jsx";

// One streaming operation for a dialog: reducer state plus the live EventSource's
// cancel handle. Unmounting cancels the stream, so its handlers can never fire
// into a tree that is gone, and starting a run cancels any previous one.
export function useStreamFlow() {
  const [flow, dispatch] = useReducer(flowReducer, initialFlow);
  const cancelRef = useRef(null);
  const toast = useToast();

  const cancel = useCallback(() => {
    cancelRef.current?.();
    cancelRef.current = null;
  }, []);

  useEffect(() => cancel, [cancel]);

  const run = useCallback(
    (streamFn, params, { onResult, onError } = {}) => {
      cancel();
      dispatch({ type: "start" });
      cancelRef.current = streamFn(params, {
        onStep: (step) => dispatch({ type: "step", step }),
        onResult: (result) => {
          cancelRef.current = null;
          dispatch({ type: "result", result });
          onResult?.(result);
        },
        onError: (error) => {
          cancelRef.current = null;
          dispatch({ type: "error", error });
          if (onError) onError(error);
          else toast(error, true);
        },
      });
    },
    [cancel, toast],
  );

  return { flow, run, cancel, busy: isBusy(flow), canShowFooter: showFooter(flow) };
}
