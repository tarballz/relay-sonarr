import { describe, expect, it } from "vitest";
import { buildTiers, tierClass, tierLabel } from "./tiers.js";

const INSTANCES = [
  { id: "1080p", name: "Sonarr 1080p" },
  { id: "4k", name: "Sonarr 4K" },
  { id: "anime", name: "Anime" },
];

describe("buildTiers", () => {
  it("maps every instance to a class and a short label", () => {
    const registry = buildTiers(INSTANCES);
    expect(registry["1080p"]).toEqual({ id: "1080p", name: "Sonarr 1080p", label: "1080P", cls: "t1080p" });
    expect(registry["4k"]).toEqual({ id: "4k", name: "Sonarr 4K", label: "4K", cls: "t4k" });
  });

  it("falls back to the instance name for an unrecognised tier", () => {
    const registry = buildTiers(INSTANCES);
    expect(registry.anime).toEqual({ id: "anime", name: "Anime", label: "Anime", cls: "tdefault" });
  });

  it("tolerates a missing or empty instance list", () => {
    expect(buildTiers()).toEqual({});
    expect(buildTiers([])).toEqual({});
  });
});

describe("tierClass", () => {
  it("keeps working without a registry (the existing call sites)", () => {
    expect(tierClass("4k")).toBe("t4k");
    expect(tierClass("1080p")).toBe("t1080p");
    expect(tierClass("uhd-4K")).toBe("t4k");
    expect(tierClass("anime")).toBe("tdefault");
    expect(tierClass(null)).toBe("tdefault");
  });

  it("prefers the registry when one is given", () => {
    const registry = buildTiers(INSTANCES);
    expect(tierClass("anime", registry)).toBe("tdefault");
    expect(tierClass("4k", registry)).toBe("t4k");
  });
});

describe("tierLabel", () => {
  it("uses one casing everywhere", () => {
    // Today the same tier reads "1080P" in three places and "1080p" in a fourth.
    expect(tierLabel("1080p")).toBe("1080P");
    expect(tierLabel("4k")).toBe("4K");
  });

  it("uses the registry name for an unknown id and the id as a last resort", () => {
    const registry = buildTiers(INSTANCES);
    expect(tierLabel("anime", registry)).toBe("Anime");
    expect(tierLabel("mystery")).toBe("mystery");
  });

  it("uses a caller-supplied fallback name for an unknown id without a registry entry", () => {
    // When the registry hasn't loaded yet, Library can pass the instance's friendly name
    expect(tierLabel("anime", undefined, "Anime Series")).toBe("Anime Series");
    expect(tierLabel("mystery", undefined, "Mystery Instance")).toBe("Mystery Instance");
  });

  it("prefers registry and sniff over the supplied name", () => {
    const registry = buildTiers(INSTANCES);
    // Known tier in registry: ignores the supplied name
    expect(tierLabel("1080p", registry, "ignored")).toBe("1080P");
    // Unknown tier in registry: still uses registry name over supplied name
    expect(tierLabel("anime", registry, "ignored")).toBe("Anime");
    // Sniffable tier without registry: sniff wins over supplied name
    expect(tierLabel("4k", undefined, "ignored")).toBe("4K");
  });

  it("falls back to id when no fallback name is provided", () => {
    expect(tierLabel("mystery")).toBe("mystery");
    expect(tierLabel("unknown", undefined, undefined)).toBe("unknown");
  });
});
