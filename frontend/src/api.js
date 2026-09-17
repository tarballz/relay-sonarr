import { request } from "./lib/http.js";

// Thin adapter so the method bodies below stay one-liners.
const get = (path, opts) => request(path, opts);
const send = (method) => (path, body, opts) => request(path, { method, body, ...opts });
const post = send("POST");
const put = send("PUT");
const del = send("DELETE");

export const api = {
  instances: (opts) => get("/instances", opts),
  settings: (opts) => get("/settings", opts),
  setFallbackChains: (body, opts) => put("/settings/fallback-chains", body, opts),
  search: (term, opts) => get(`/search?term=${encodeURIComponent(term)}`, opts),
  series: (opts) => get("/series", opts),
  queue: (opts) => get("/queue", opts),
  profiles: (id, opts) => get(`/instances/${id}/profiles`, opts),
  rootFolders: (id, opts) => get(`/instances/${id}/root-folders`, opts),
  add: (body, opts) => post("/add", body, opts),
  smartAdd: (body, opts) => post("/smart-add", body, opts),
  advanceFallback: (body, opts) => post("/advance-fallback", body, opts),
  availability: (instanceId, seriesId, opts) =>
    get(`/availability?instanceId=${instanceId}&seriesId=${seriesId}`, opts),
  operations: (opts) => get("/operations", opts),
  removeSeries: (instanceId, seriesId, deleteFiles = false, opts) =>
    del(`/instances/${instanceId}/series/${seriesId}?deleteFiles=${deleteFiles}`, null, opts),
  // Reconciler / policy surface.
  libraryStatus: (opts) => get("/library/status", opts),
  plan: (tvdb, opts) => get(`/series/${tvdb}/plan`, opts),
  planRefresh: (tvdb, target, opts) =>
    get(`/series/${tvdb}/plan?refresh=${encodeURIComponent(target)}`, opts),
  policy: (tvdb, opts) => get(`/series/${tvdb}/policy`, opts),
  setPolicy: (tvdb, body, opts) => put(`/series/${tvdb}/policy`, body, opts),
  pauseSeries: (tvdb, opts) => post(`/series/${tvdb}/pause`, null, opts),
  resumeSeries: (tvdb, opts) => post(`/series/${tvdb}/resume`, null, opts),
  reconcileSeries: (tvdb, opts) => post(`/series/${tvdb}/reconcile`, null, opts),
  reconcileTick: (opts) => post("/reconcile/tick", null, opts),
  reconcilerStatus: (opts) => get("/reconciler/status", opts),
  defaults: (opts) => get("/settings/defaults", opts),
  setDefaults: (body, opts) => put("/settings/defaults", body, opts),
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
