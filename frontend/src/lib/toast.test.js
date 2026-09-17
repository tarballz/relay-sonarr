import { describe, expect, it } from "vitest";
import { MAX_TOASTS, initialToasts, toastReducer } from "./toast.js";

// The id comes from the caller (the provider's ref counter), so two pushes in
// one tick can never collide the way a state-derived id would.
const push = (state, id, msg, err) => toastReducer(state, { type: "push", id, msg, err });

describe("toastReducer", () => {
  it("starts empty", () => {
    expect(initialToasts.items).toEqual([]);
  });

  it("appends the item under the id it was given", () => {
    const one = push(initialToasts, 1, "saved");
    const two = push(one, 2, "removed", true);
    expect(one.items[0]).toEqual({ id: 1, msg: "saved", err: false });
    expect(two.items.map((t) => t.id)).toEqual([1, 2]);
    expect(two.items[1].err).toBe(true);
  });

  it("drops the oldest past the cap", () => {
    let state = initialToasts;
    ["a", "b", "c", "d"].forEach((msg, i) => {
      state = push(state, i + 1, msg);
    });
    expect(state.items).toHaveLength(MAX_TOASTS);
    expect(state.items.map((t) => t.msg)).toEqual(["b", "c", "d"]);
  });

  it("dismisses by id and ignores unknown ids", () => {
    const two = push(push(initialToasts, 1, "a"), 2, "b");
    const one = toastReducer(two, { type: "dismiss", id: 1 });
    expect(one.items.map((t) => t.msg)).toEqual(["b"]);
    expect(toastReducer(one, { type: "dismiss", id: 99 })).toBe(one);
  });

  it("ignores unknown actions", () => {
    expect(toastReducer(initialToasts, { type: "nope" })).toBe(initialToasts);
  });
});
