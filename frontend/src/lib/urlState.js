// Filters and view toggles belong in the URL: a reload keeps them and a link
// carries them. Pure serialize/parse plus a thin hook over useSearchParams.

import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

const FALSEY = new Set(["0", "false", ""]);

function coerce(raw, fallback) {
  if (typeof fallback === "number") {
    const n = Number(raw);
    return Number.isFinite(n) ? n : fallback;
  }
  if (typeof fallback === "boolean") return !FALSEY.has(String(raw).toLowerCase());
  return raw;
}

export function parse(searchParams, defaults) {
  const state = { ...defaults };
  for (const key of Object.keys(defaults)) {
    if (!searchParams.has(key)) continue;
    state[key] = coerce(searchParams.get(key), defaults[key]);
  }
  return state;
}

export function serialize(state, defaults, existing) {
  const out = new URLSearchParams(existing ? existing.toString() : "");
  for (const [key, fallback] of Object.entries(defaults)) {
    const value = state[key];
    if (value === fallback || value === undefined || value === null || value === "") {
      out.delete(key);
      continue;
    }
    out.set(key, typeof fallback === "boolean" ? (value ? "1" : "0") : String(value));
  }
  return out;
}

export function useUrlState(defaults) {
  const [searchParams, setSearchParams] = useSearchParams();
  const state = useMemo(() => parse(searchParams, defaults), [searchParams, defaults]);

  const setState = useCallback(
    (patch) => {
      setSearchParams((current) => serialize({ ...parse(current, defaults), ...patch }, defaults, current), {
        replace: true,
      });
    },
    [defaults, setSearchParams],
  );

  return [state, setState];
}
