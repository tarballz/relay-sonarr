// Open an SSE stream to a GET endpoint and dispatch step/result/error callbacks.
// Returns a cancel function. Closes on the first terminal event (result or error)
// to defeat EventSource's automatic reconnect.

function query(params) {
  // Array values become repeated params (monitoredSeasons=1&monitoredSeasons=2),
  // which is what FastAPI's list[int] query params expect. Empty values are skipped.
  const usp = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v === null || v === undefined) return;
    if (Array.isArray(v)) v.forEach((x) => usp.append(k, x));
    else usp.append(k, v);
  });
  return usp.toString();
}

export function openStream(path, params, { onStep, onResult, onError } = {}) {
  const es = new EventSource(`/api${path}?${query(params)}`);
  let done = false;

  const finish = () => {
    if (done) return false;
    done = true;
    es.close();
    return true;
  };

  // A frame we can't parse is a broken stream, not an exception to throw from
  // inside an event handler (where nothing could catch it).
  const parse = (raw) => {
    try {
      return { ok: true, data: JSON.parse(raw) };
    } catch {
      if (finish()) onError?.("Malformed stream frame");
      return { ok: false };
    }
  };

  es.addEventListener("step", (e) => {
    if (done) return;
    const parsed = parse(e.data);
    if (parsed.ok) onStep?.(parsed.data);
  });

  es.addEventListener("result", (e) => {
    if (done) return;
    const parsed = parse(e.data);
    if (!parsed.ok) return;
    finish();
    onResult?.(parsed.data);
  });

  es.addEventListener("error", (e) => {
    if (done) return; // normal end-of-stream close — ignore
    let msg = "Stream error";
    if (e.data) {
      try {
        msg = JSON.parse(e.data).message || msg;
      } catch {
        /* keep the default */
      }
    }
    finish();
    onError?.(msg);
  });

  return finish;
}
