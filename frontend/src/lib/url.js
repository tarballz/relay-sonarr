// Artwork URLs (TVDB fanart, relayed through Sonarr) are third-party data, not
// something we author — treat them as untrusted before they ever reach a CSS
// url(). Only an absolute http(s) URL survives; anything else (javascript:,
// data:, a bare string that isn't a URL at all) comes back null.
//
// Callers are expected to wrap the result in a *double-quoted* url("...").
// Within that quoted CSS string, `"` and `\` are the only two characters that
// can end or corrupt it early (a trailing `\` would escape the closing quote
// itself); `)` is inert once inside quotes; newline/CR/tab are stripped by
// the URL parser and other C0 controls are percent-encoded by it. The parser
// also normalizes a `\` in the *path* to `/` for special schemes, but leaves
// a literal `\` untouched in the query or fragment — so both `"` and `\` are
// explicitly escaped below rather than relying on the parser alone.
export function safeBackgroundUrl(raw) {
  if (!raw) return null;
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    return null; // not a parseable absolute URL
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  return parsed.href.replace(/\\/g, "%5C").replace(/"/g, "%22");
}
