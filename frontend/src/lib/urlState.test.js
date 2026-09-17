import { describe, expect, it } from "vitest";
import { parse, serialize } from "./urlState.js";

const DEFAULTS = { q: "", sort: "title", view: "grid", tier: "all", page: 1, missingOnly: false };

describe("parse", () => {
  it("returns the defaults for an empty query", () => {
    expect(parse(new URLSearchParams(""), DEFAULTS)).toEqual(DEFAULTS);
  });

  it("coerces each value to the type of its default", () => {
    const state = parse(new URLSearchParams("q=mad&page=3&missingOnly=1"), DEFAULTS);
    expect(state.q).toBe("mad");
    expect(state.page).toBe(3);
    expect(state.missingOnly).toBe(true);
  });

  it("falls back to the default for an unparseable number or unknown key", () => {
    const state = parse(new URLSearchParams("page=abc&nope=1"), DEFAULTS);
    expect(state.page).toBe(1);
    expect(state).not.toHaveProperty("nope");
  });

  it("treats 0 and false as false for booleans", () => {
    expect(parse(new URLSearchParams("missingOnly=0"), DEFAULTS).missingOnly).toBe(false);
    expect(parse(new URLSearchParams("missingOnly=false"), DEFAULTS).missingOnly).toBe(false);
  });
});

describe("serialize", () => {
  it("omits values equal to their default", () => {
    expect(serialize(DEFAULTS, DEFAULTS).toString()).toBe("");
  });

  it("writes only what differs", () => {
    const qs = serialize({ ...DEFAULTS, sort: "missing", missingOnly: true }, DEFAULTS);
    expect(qs.get("sort")).toBe("missing");
    expect(qs.get("missingOnly")).toBe("1");
    expect(qs.get("view")).toBeNull();
  });

  it("round-trips through parse", () => {
    const state = { ...DEFAULTS, q: "mad men", page: 2, view: "table" };
    expect(parse(serialize(state, DEFAULTS), DEFAULTS)).toEqual(state);
  });

  it("keeps query keys it doesn't own", () => {
    const existing = new URLSearchParams("ref=email&sort=stale");
    const qs = serialize({ ...DEFAULTS, sort: "missing" }, DEFAULTS, existing);
    expect(qs.get("ref")).toBe("email");
    expect(qs.get("sort")).toBe("missing");
  });
});
