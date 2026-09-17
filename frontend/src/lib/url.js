// Artwork URLs (TVDB fanart, relayed through Sonarr) are third-party data, not
// something we author — treat them as untrusted before they ever reach a CSS
// url(). Only an absolute http(s) URL survives; anything else (javascript:,
// data:, a bare string that isn't a URL at all) comes back null.
//
// The WHATWG URL parser already percent-encodes anything unsafe in the path/
// query when it builds `.href` (a stray `"` becomes %22, a backslash becomes
// a path separator for special schemes), so `.href` alone is safe to
// interpolate. The trailing quote-replace is defense-in-depth, not the
// primary guard — callers are expected to wrap the result in a *double-quoted*
// url("...") so a literal `"` is the only character that could break out.
export function safeBackgroundUrl(raw) {
  if (!raw) return null;
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    return null; // not a parseable absolute URL
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  return parsed.href.replace(/"/g, "%22");
}
