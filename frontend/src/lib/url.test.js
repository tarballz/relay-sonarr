import { describe, expect, it } from "vitest";
import { safeBackgroundUrl } from "./url.js";

describe("safeBackgroundUrl", () => {
  it("passes an ordinary https URL through", () => {
    const url = "https://artworks.thetvdb.com/banners/fanart/original/121361-1.jpg";
    expect(safeBackgroundUrl(url)).toBe(url);
  });

  it("passes an ordinary http URL through", () => {
    const url = "http://example.com/art.jpg";
    expect(safeBackgroundUrl(url)).toBe(url);
  });

  it("rejects a javascript: URL", () => {
    expect(safeBackgroundUrl("javascript:alert(1)")).toBeNull();
  });

  it("rejects a data: URL", () => {
    expect(safeBackgroundUrl("data:text/html,<script>alert(1)</script>")).toBeNull();
  });

  it("encodes an embedded double quote so it can't terminate a quoted url()", () => {
    const raw = 'https://example.com/a".jpg';
    const safe = safeBackgroundUrl(raw);
    expect(safe).not.toContain('"');
    expect(safe).toContain("%22");
  });

  it("encodes a \")-style breakout attempt so it can't terminate a quoted url()", () => {
    const raw = 'https://example.com/a").body{background:red}//x.jpg';
    const safe = safeBackgroundUrl(raw);
    expect(safe).not.toContain('"');
    expect(safe).toContain("%22");
  });

  it("encodes a trailing backslash in the query string so it can't escape the closing quote", () => {
    const raw = "https://example.com/a?x=1\\";
    const safe = safeBackgroundUrl(raw);
    expect(safe).not.toContain("\\");
    expect(safe).toContain("%5C");
  });

  it("encodes a backslash mid-query so it can't escape the closing quote", () => {
    const raw = "https://example.com/a?x=1\\end";
    const safe = safeBackgroundUrl(raw);
    expect(safe).not.toContain("\\");
    expect(safe).toContain("%5C");
  });

  it("returns null for a non-parseable string", () => {
    expect(safeBackgroundUrl("not a url at all")).toBeNull();
  });

  it("returns null for null or undefined", () => {
    expect(safeBackgroundUrl(null)).toBeNull();
    expect(safeBackgroundUrl(undefined)).toBeNull();
  });

  it("returns null for an empty string", () => {
    expect(safeBackgroundUrl("")).toBeNull();
  });
});
