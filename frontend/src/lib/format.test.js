import { describe, expect, it } from "vitest";
import { ago, bytes, duration, when } from "./format.js";

describe("bytes", () => {
  it("formats sizes with one decimal from MB up", () => {
    expect(bytes(0)).toBe("0 B");
    expect(bytes(900)).toBe("900 B");
    expect(bytes(1536)).toBe("1.5 KB");
    expect(bytes(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(bytes(15 * 1024 * 1024)).toBe("15 MB");
    expect(bytes(2.5 * 1024 ** 3)).toBe("2.5 GB");
  });

  it("returns a dash for missing values", () => {
    expect(bytes(null)).toBe("—");
    expect(bytes(undefined)).toBe("—");
  });

  it("returns a dash for non-numeric values", () => {
    expect(bytes("abc")).toBe("—");
  });
});

describe("ago", () => {
  it("uses the largest whole unit", () => {
    expect(ago(5)).toBe("just now");
    expect(ago(90)).toBe("1m ago");
    expect(ago(3600)).toBe("1h ago");
    expect(ago(3600 * 5 + 60)).toBe("5h ago");
    expect(ago(86400 * 3)).toBe("3d ago");
  });

  it("returns a dash for missing values", () => {
    expect(ago(null)).toBe("—");
  });
});

describe("when", () => {
  const now = new Date("2026-09-17T12:00:00Z");

  it("shows only the time for today", () => {
    const out = when("2026-09-17T09:30:00Z", now);
    expect(out).not.toMatch(/Sep/);
    expect(out).toMatch(/\d/);
  });

  it("includes the date for another day", () => {
    expect(when("2026-09-15T09:30:00Z", now)).toMatch(/Sep/);
  });

  it("returns a dash for missing or unparseable values", () => {
    expect(when(null, now)).toBe("—");
    expect(when("not-a-date", now)).toBe("—");
  });
});

describe("duration", () => {
  it("formats sub-second, seconds and minutes", () => {
    expect(duration(0)).toBe("0ms");
    expect(duration(850)).toBe("850ms");
    expect(duration(1500)).toBe("1.5s");
    expect(duration(65000)).toBe("1m 5s");
    expect(duration(null)).toBe("—");
  });
});
