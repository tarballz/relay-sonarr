import { describe, expect, it } from "vitest";
import { reasonText, roadblockGlyph, roadblockNote, roadblockTitle } from "./resolution.js";

// The live payloads that produced the bad copy: Ink Master (83 releases, none
// qualifying) and a genuinely empty tier.
const INK_MASTER = {
  available: false,
  releasesExist: true,
  totalReleases: 83,
  rejectionSummary: [
    { reason: "wrong series", count: 75 },
    { reason: "too few seeders", count: 6 },
  ],
  sampledEpisodes: [{ id: 1 }, { id: 2 }, { id: 3 }],
};

const TRULY_EMPTY = {
  available: false,
  releasesExist: false,
  totalReleases: 0,
  rejectionSummary: [],
  sampledEpisodes: [{ id: 1 }, { id: 2 }, { id: 3 }],
};

describe("reasonText", () => {
  it("lists why nothing qualified when releases came back", () => {
    expect(reasonText(INK_MASTER)).toBe(
      "83 found, none qualified — 75 wrong series, 6 too few seeders."
    );
  });

  it("says how many episodes were checked before claiming nothing exists", () => {
    expect(reasonText(TRULY_EMPTY)).toBe(
      "No releases found at all, across 3 sampled episodes."
    );
  });

  it("omits the sample count when only one episode was checked", () => {
    expect(reasonText({ totalReleases: 0, sampledEpisodes: [{ id: 1 }] })).toBe(
      "No releases found at all."
    );
  });

  it("is empty for a missing availability block", () => {
    expect(reasonText(null)).toBe("");
  });
});

describe("roadblockTitle", () => {
  it("does not claim a series is releaseless when releases exist", () => {
    const title = roadblockTitle(
      { status: "exhausted", availability: INK_MASTER },
      "Ink Master"
    );
    expect(title).toBe("Nothing qualified for “Ink Master” on any tier — yet.");
  });

  it("still states the plain truth when the indexers returned nothing", () => {
    expect(
      roadblockTitle({ status: "exhausted", availability: TRULY_EMPTY }, "Obscure Show")
    ).toBe("No release on any tier for “Obscure Show.”");
  });

  it("uses the mid-chain wording when the chain is not spent", () => {
    expect(
      roadblockTitle({ status: "fallback_suggested", availability: INK_MASTER }, "Jackass")
    ).toBe("No qualifying release for “Jackass” yet.");
  });
});

describe("roadblockNote", () => {
  it("tells the user the reconciler will keep trying", () => {
    expect(roadblockNote({ availability: INK_MASTER })).toMatch(/keeps searching/);
  });

  it("promises nothing when there is genuinely nothing to find", () => {
    expect(roadblockNote({ availability: TRULY_EMPTY })).toBe("");
  });
});

describe("roadblockGlyph", () => {
  it("reserves the empty-set glyph for an actually empty result", () => {
    expect(roadblockGlyph({ status: "exhausted", availability: TRULY_EMPTY })).toBe("∅");
    expect(roadblockGlyph({ status: "exhausted", availability: INK_MASTER })).toBe("⚠");
    expect(roadblockGlyph({ status: "fallback_suggested" })).toBe("⚠");
  });
});
