// The state of one streaming dialog operation.
//
// `phase` — not `steps.length` — decides what the dialog renders, so a finished
// or failed run still shows its controls instead of stranding the user with the
// play-by-play and no way forward.

export const initialFlow = { phase: "idle", steps: [], result: null, error: null };

const TERMINAL = new Set(["done", "error"]);

export function flowReducer(state, action) {
  switch (action.type) {
    case "start":
      return { phase: "streaming", steps: [], result: null, error: null };
    case "step":
      if (state.phase !== "streaming") return state;
      return { ...state, steps: [...state.steps, action.step] };
    case "result":
      if (TERMINAL.has(state.phase)) return state;
      return { ...state, phase: "done", result: action.result ?? null };
    case "error":
      if (TERMINAL.has(state.phase)) return state;
      return { ...state, phase: "error", error: action.error ?? "Failed" };
    case "reset":
      return initialFlow;
    default:
      return state;
  }
}

export const isBusy = (state) => state.phase === "streaming";

export const showFooter = (state) => state.phase !== "streaming";
