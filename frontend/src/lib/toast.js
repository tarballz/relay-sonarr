// Toast queue state. Pure: the provider owns the ids and the timers, this owns
// the list. Ids are supplied by the caller so a push never has to derive one
// from state it may not have seen yet.

export const MAX_TOASTS = 3;
export const DEFAULT_TTL = 4200;

export const initialToasts = { items: [] };

export function toastReducer(state, action) {
  switch (action.type) {
    case "push": {
      const items = [...state.items, { id: action.id, msg: action.msg, err: Boolean(action.err) }];
      return { items: items.slice(-MAX_TOASTS) };
    }
    case "dismiss": {
      const items = state.items.filter((t) => t.id !== action.id);
      return items.length === state.items.length ? state : { items };
    }
    default:
      return state;
  }
}
