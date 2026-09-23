// Copy for the smart-add roadblock screen.
//
// These helpers exist because the wording here is load-bearing: the dialog once
// offered "Remove the empty series" against a series with 83 releases, and a
// user took it. An availability miss means "nothing qualified on the episodes
// we sampled" — never "nothing exists" — unless the indexers really did return
// zero releases (`releasesExist === false`).

const hasReleases = (data) => !!data?.availability?.releasesExist;

// Turn an availability result into a one-line "why nothing qualified" summary.
export function reasonText(av) {
  if (!av) return "";
  const total = av.totalReleases || 0;
  if (total === 0) {
    const sampled = (av.sampledEpisodes || []).length;
    // Only meaningful on this branch: a zero total means *every* sampled
    // episode came back empty, so the sample size is the strength of the claim.
    const checked = sampled > 1 ? `, across ${sampled} sampled episodes` : "";
    return `No releases found at all${checked}.`;
  }
  const parts = (av.rejectionSummary || []).map((r) => `${r.count} ${r.reason}`);
  return `${total} found, none qualified${parts.length ? " — " + parts.join(", ") : ""}.`;
}

export function roadblockTitle(data, title) {
  if (data?.status !== "exhausted") return `No qualifying release for “${title}” yet.`;
  if (hasReleases(data)) return `Nothing qualified for “${title}” on any tier — yet.`;
  return `No release on any tier for “${title}.”`;
}

// The reassurance that makes "Leave monitored" the obvious choice.
export function roadblockNote(data) {
  if (!hasReleases(data)) return "";
  return (
    "Relay keeps searching every episode on its own schedule and will grab " +
    "them as they qualify."
  );
}

export function roadblockGlyph(data) {
  return data?.status === "exhausted" && !hasReleases(data) ? "∅" : "⚠";
}
