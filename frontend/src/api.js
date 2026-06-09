// Thin fetch wrapper over the backend's same-origin /api surface.
async function req(path, opts = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json();
}

export const api = {
  instances: () => req("/instances"),
  settings: () => req("/settings"),
  setFallbackChains: (body) =>
    req("/settings/fallback-chains", { method: "PUT", body: JSON.stringify(body) }),
  search: (term) => req(`/search?term=${encodeURIComponent(term)}`),
  series: () => req("/series"),
  queue: () => req("/queue"),
  profiles: (id) => req(`/instances/${id}/profiles`),
  rootFolders: (id) => req(`/instances/${id}/root-folders`),
  add: (body) => req("/add", { method: "POST", body: JSON.stringify(body) }),
  smartAdd: (body) => req("/smart-add", { method: "POST", body: JSON.stringify(body) }),
  advanceFallback: (body) =>
    req("/advance-fallback", { method: "POST", body: JSON.stringify(body) }),
  availability: (instanceId, seriesId) =>
    req(`/availability?instanceId=${instanceId}&seriesId=${seriesId}`),
  operations: () => req("/operations"),
  removeSeries: (instanceId, seriesId, deleteFiles = false) =>
    req(`/instances/${instanceId}/series/${seriesId}?deleteFiles=${deleteFiles}`, {
      method: "DELETE",
    }),
  // Reconciler / policy surface.
  libraryStatus: () => req("/library/status"),
  plan: (tvdb) => req(`/series/${tvdb}/plan`),
  planRefresh: (tvdb, target) =>
    req(`/series/${tvdb}/plan?refresh=${encodeURIComponent(target)}`),
  policy: (tvdb) => req(`/series/${tvdb}/policy`),
  setPolicy: (tvdb, body) =>
    req(`/series/${tvdb}/policy`, { method: "PUT", body: JSON.stringify(body) }),
  pauseSeries: (tvdb) => req(`/series/${tvdb}/pause`, { method: "POST" }),
  resumeSeries: (tvdb) => req(`/series/${tvdb}/resume`, { method: "POST" }),
  reconcileSeries: (tvdb) => req(`/series/${tvdb}/reconcile`, { method: "POST" }),
  reconcileTick: () => req("/reconcile/tick", { method: "POST" }),
  reconcilerStatus: () => req("/reconciler/status"),
  defaults: () => req("/settings/defaults"),
  setDefaults: (body) =>
    req("/settings/defaults", { method: "PUT", body: JSON.stringify(body) }),
};

// Open an SSE stream to a GET endpoint and dispatch step/result/error callbacks.
// Returns a cancel function. Handles the auto-reconnect quirk by closing on the
// first terminal event (result or error).
function openStream(path, params, { onStep, onResult, onError }) {
  // Build the query string by hand so array values become repeated params
  // (e.g. monitoredSeasons=[1,2] → ?monitoredSeasons=1&monitoredSeasons=2), which
  // is what FastAPI's list[int] query params expect. null/undefined are skipped.
  const usp = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v === null || v === undefined) return;
    if (Array.isArray(v)) v.forEach((x) => usp.append(k, x));
    else usp.append(k, v);
  });
  const qs = usp.toString();
  const es = new EventSource(`/api${path}?${qs}`);
  let done = false;
  const finish = () => {
    if (!done) {
      done = true;
      es.close();
    }
  };
  es.addEventListener("step", (e) => {
    if (!done) onStep?.(JSON.parse(e.data));
  });
  es.addEventListener("result", (e) => {
    const data = JSON.parse(e.data);
    finish();
    onResult?.(data);
  });
  es.addEventListener("error", (e) => {
    if (done) return; // normal end-of-stream close — ignore
    let msg = "Stream error";
    if (e.data) {
      try {
        msg = JSON.parse(e.data).message;
      } catch {
        /* keep default */
      }
    }
    finish();
    onError?.(msg);
  });
  return finish;
}

export function streamSmartAdd(params, handlers) {
  return openStream("/smart-add/stream", params, handlers);
}

export function streamAdvance(params, handlers) {
  return openStream("/advance-fallback/stream", params, handlers);
}

export function streamReattempt(params, handlers) {
  return openStream("/reattempt/stream", params, handlers);
}

export function streamFillGaps(params, handlers) {
  return openStream("/fill-gaps/stream", params, handlers);
}

export function streamSpillSeason(params, handlers) {
  return openStream("/spill-season/stream", params, handlers);
}

// Map an instance id to its tier CSS class. 1080p -> teal, 4k -> amber.
export function tierClass(instanceId) {
  const id = (instanceId || "").toLowerCase();
  if (id.includes("4k")) return "t4k";
  if (id.includes("1080")) return "t1080p";
  return "tdefault";
}
