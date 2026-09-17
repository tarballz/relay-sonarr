import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { openStream } from "./stream.js";

class FakeEventSource {
  static last = null;

  constructor(url) {
    this.url = url;
    this.listeners = {};
    this.closeCount = 0;
    FakeEventSource.last = this;
  }

  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }

  close() {
    this.closeCount += 1;
  }

  emit(type, data) {
    for (const fn of this.listeners[type] || []) fn({ data });
  }
}

beforeEach(() => {
  FakeEventSource.last = null;
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function open(handlers = {}) {
  const cancel = openStream("/smart-add/stream", { tvdbId: 7, seasons: [1, 2], skip: null }, handlers);
  return { cancel, es: FakeEventSource.last };
}

describe("openStream", () => {
  it("builds the url with repeated params and skips empty values", () => {
    const { es } = open();
    expect(es.url).toBe("/api/smart-add/stream?tvdbId=7&seasons=1&seasons=2");
  });

  it("dispatches step events until a terminal event arrives", () => {
    const onStep = vi.fn();
    const onResult = vi.fn();
    const { es } = open({ onStep, onResult });

    es.emit("step", JSON.stringify({ phase: "add" }));
    es.emit("result", JSON.stringify({ status: "added" }));
    es.emit("step", JSON.stringify({ phase: "late" }));

    expect(onStep).toHaveBeenCalledTimes(1);
    expect(onStep).toHaveBeenCalledWith({ phase: "add" });
    expect(onResult).toHaveBeenCalledWith({ status: "added" });
    expect(es.closeCount).toBe(1);
  });

  it("reports a server error frame's message", () => {
    const onError = vi.fn();
    const { es } = open({ onError });
    es.emit("error", JSON.stringify({ message: "profile 'SD' not found" }));
    expect(onError).toHaveBeenCalledWith("profile 'SD' not found");
    expect(es.closeCount).toBe(1);
  });

  it("falls back to a generic message for a transport error with no data", () => {
    const onError = vi.fn();
    const { es } = open({ onError });
    es.emit("error", undefined);
    expect(onError).toHaveBeenCalledWith("Stream error");
  });

  it("ignores a transport error after the stream already finished", () => {
    const onError = vi.fn();
    const { es } = open({ onResult: () => {}, onError });
    es.emit("result", JSON.stringify({ status: "added" }));
    es.emit("error", undefined);
    expect(onError).not.toHaveBeenCalled();
  });

  it("turns a malformed frame into a stream error instead of throwing", () => {
    const onStep = vi.fn();
    const onError = vi.fn();
    const { es } = open({ onStep, onError });

    expect(() => es.emit("step", "{not json")).not.toThrow();

    expect(onStep).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith("Malformed stream frame");
    expect(es.closeCount).toBe(1);
  });

  it("cancels once, however many times it is called", () => {
    const { cancel, es } = open();
    cancel();
    cancel();
    expect(es.closeCount).toBe(1);
  });
});
