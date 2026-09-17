import { request } from "./lib/http.js";
import { openStream } from "./lib/stream.js";

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
    del(`/instances/${instanceId}/series/${seriesId}?deleteFiles=${deleteFiles}`, undefined, opts),
  // Reconciler / policy surface.
  libraryStatus: (opts) => get("/library/status", opts),
  plan: (tvdb, opts) => get(`/series/${tvdb}/plan`, opts),
  planRefresh: (tvdb, target, opts) =>
    get(`/series/${tvdb}/plan?refresh=${encodeURIComponent(target)}`, opts),
  policy: (tvdb, opts) => get(`/series/${tvdb}/policy`, opts),
  setPolicy: (tvdb, body, opts) => put(`/series/${tvdb}/policy`, body, opts),
  pauseSeries: (tvdb, opts) => post(`/series/${tvdb}/pause`, undefined, opts),
  resumeSeries: (tvdb, opts) => post(`/series/${tvdb}/resume`, undefined, opts),
  reconcileSeries: (tvdb, opts) => post(`/series/${tvdb}/reconcile`, undefined, opts),
  reconcileTick: (opts) => post("/reconcile/tick", undefined, opts),
  reconcilerStatus: (opts) => get("/reconciler/status", opts),
  defaults: (opts) => get("/settings/defaults", opts),
  setDefaults: (body, opts) => put("/settings/defaults", body, opts),
};

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
