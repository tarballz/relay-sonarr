import { describe, expect, it } from "vitest";
import { flowReducer, initialFlow, isBusy, showFooter } from "./flow.js";

const run = (actions, state = initialFlow) => actions.reduce(flowReducer, state);

describe("flowReducer", () => {
  it("starts empty and idle", () => {
    expect(initialFlow).toEqual({ phase: "idle", steps: [], result: null, error: null });
  });

  it("collects steps while streaming", () => {
    const state = run([{ type: "start" }, { type: "step", step: { phase: "add" } }, { type: "step", step: { phase: "search" } }]);
    expect(state.phase).toBe("streaming");
    expect(state.steps).toEqual([{ phase: "add" }, { phase: "search" }]);
  });

  it("start clears a previous run's steps and error", () => {
    const finished = run([{ type: "start" }, { type: "step", step: { phase: "add" } }, { type: "error", error: "boom" }]);
    const restarted = flowReducer(finished, { type: "start" });
    expect(restarted).toEqual({ phase: "streaming", steps: [], result: null, error: null });
  });

  it("keeps the steps when a result arrives", () => {
    const state = run([{ type: "start" }, { type: "step", step: { phase: "add" } }, { type: "result", result: { status: "added" } }]);
    expect(state.phase).toBe("done");
    expect(state.result).toEqual({ status: "added" });
    expect(state.steps).toHaveLength(1);
  });

  it("records an error without losing the steps so far", () => {
    const state = run([{ type: "start" }, { type: "step", step: { phase: "add" } }, { type: "error", error: "no releases" }]);
    expect(state.phase).toBe("error");
    expect(state.error).toBe("no releases");
    expect(state.steps).toHaveLength(1);
  });

  it("ignores late frames after a terminal event", () => {
    const done = run([{ type: "start" }, { type: "result", result: { status: "added" } }]);
    expect(flowReducer(done, { type: "step", step: { phase: "late" } })).toBe(done);
    expect(flowReducer(done, { type: "error", error: "late" })).toBe(done);
  });

  it("reset returns to idle", () => {
    const done = run([{ type: "start" }, { type: "result", result: {} }]);
    expect(flowReducer(done, { type: "reset" })).toEqual(initialFlow);
  });

  it("ignores unknown actions", () => {
    expect(flowReducer(initialFlow, { type: "nope" })).toBe(initialFlow);
  });
});

describe("selectors", () => {
  it("is busy only while streaming", () => {
    expect(isBusy({ phase: "streaming" })).toBe(true);
    expect(isBusy({ phase: "idle" })).toBe(false);
    expect(isBusy({ phase: "done" })).toBe(false);
    expect(isBusy({ phase: "error" })).toBe(false);
  });

  it("shows the footer whenever a stream is not running", () => {
    // The stranded-dialog regression: a finished or failed run must still offer
    // controls, even though its steps are still on screen.
    expect(showFooter({ phase: "idle", steps: [] })).toBe(true);
    expect(showFooter({ phase: "done", steps: [{ phase: "add" }] })).toBe(true);
    expect(showFooter({ phase: "error", steps: [{ phase: "add" }] })).toBe(true);
    expect(showFooter({ phase: "streaming", steps: [] })).toBe(false);
  });
});
