# Frontend Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the SPA one HTTP layer, one dialog, one toast host, one tier registry and one set of design tokens — with a vitest harness — and fix the three defects that fall out of their absence (dialogs stranded with no controls, leaked EventSource streams, toasts cancelling each other).

**Architecture:** Pure logic moves into `src/lib/*.js` with sibling `*.test.js` (node environment, no jsdom); React components stay thin wrappers over those functions. Shared UI (`Dialog`, `Toast`) lives in `src/components/ui/`. Nothing about the API contract changes and the backend is untouched.

**Tech Stack:** React 18, Vite 5, TanStack Query 5, react-router 6, framer-motion 11, vitest (new devDependency), plain CSS in one global `src/styles.css`.

**Spec:** `docs/superpowers/specs/2026-09-17-frontend-foundations-design.md`

## Global Constraints

- **No backend changes.** `backend/` is out of scope; its suite (360 tests) must not be touched.
- **No new runtime dependencies.** `vitest` is the only new devDependency. No jsdom, no testing-library, no UI/icon/chart library.
- **No user-visible feature changes** beyond the bug fixes named in the spec. Same pages, same routes (plus a 404), same API calls.
- **`api.js` keeps every existing export name and call signature.** Only internals change.
- **`src/pages/Settings.jsx` is excluded from the inline-style sweep** (its `{...data, ...form}` spread at `:182` and the client-side fallbacks at `:167-173` are load-bearing and it changed 2026-09-15). It still loses its local toast and gains `aria-describedby`.
- **CSS class names that JSX already uses stay** (`t1080p`, `t4k`, `tdefault`, `.dialog*`, `.toast`, `.btn`, …) — tokens change values, not selectors JSX depends on.
- Run frontend commands from `frontend/`: `npm test`, `npm run build`. If `node_modules` is missing or root-owned, report it rather than trying to `sudo`.
- Commits: conventional prefix, **no AI attribution / Co-Authored-By lines**.
- TDD: every task with pure logic writes its failing test first.

## File map

| File | Responsibility |
|---|---|
| `frontend/package.json` (modify) | vitest devDependency, `test`/`test:watch` scripts |
| `frontend/vite.config.js` (modify) | vitest `test` block |
| `.dockerignore` (new, repo root) | keep host `node_modules`/`dist` out of the image build |
| `Dockerfile` (modify) | run `npm test` in the frontend stage |
| `frontend/src/lib/format.js` (+test) | `bytes`, `ago`, `when`, `duration` |
| `frontend/src/lib/http.js` (+test) | `ApiError`, `request()` |
| `frontend/src/lib/flow.js` (+test) | dialog stream state reducer |
| `frontend/src/lib/toast.js` (+test) | toast queue reducer |
| `frontend/src/lib/tiers.js` (+test) | instance → `{label, cls}` registry |
| `frontend/src/lib/urlState.js` (+test) | serialize/parse + `useUrlState` |
| `frontend/src/components/ui/Dialog.jsx` (new) | the one dialog skeleton, always-present close button |
| `frontend/src/components/ui/Toast.jsx` (new) | `ToastProvider`, `useToast` |
| `frontend/src/components/ui/NotFound.jsx` (new) | catch-all route panel |
| `frontend/src/api.js` (modify) | rebuilt on `http.js`; `openStream` hardened |
| `frontend/src/components/useDialog.js` (modify) | focus restoration |
| `frontend/src/components/{AddDialog,ResolveDialog,SeasonDialog,PolicyEditor,ConfirmDialog}.jsx` (modify) | use `Dialog`, `flow`, `useToast`, cancel streams |
| `frontend/src/components/{Shared,ProgressStream,ResolutionOptions}.jsx` (modify) | re-export `bytes` from lib, spinner/live-region a11y |
| `frontend/src/pages/*.jsx` (modify) | drop local toasts, use tier registry, Library URL state |
| `frontend/src/App.jsx`, `src/main.jsx` (modify) | providers, skip link, 404 route, `MotionConfig` |
| `frontend/src/styles.css` (modify) | tokens, duplicate-selector merges, focus-visible, reduced-motion |

---

### Task 1: vitest harness, .dockerignore, and `lib/format.js`

**Files:**
- Modify: `frontend/package.json`, `frontend/vite.config.js`, `Dockerfile` (frontend stage, lines 1-7)
- Create: `.dockerignore`, `frontend/src/lib/format.js`, `frontend/src/lib/format.test.js`
- Modify: `frontend/src/components/Shared.jsx` (re-export `bytes` from lib), `frontend/src/pages/Activity.jsx` + `frontend/src/pages/Library.jsx` (imports only, if they import `bytes` from Shared)

**Interfaces:**
- Produces: `npm test` → `vitest run`; `format.bytes(n)`, `format.ago(seconds)`, `format.when(iso, now?)`, `format.duration(ms)` (all named exports).
- `Shared.jsx` keeps exporting `bytes` (re-export) so existing importers don't break.

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/format.test.js`:

```js
import { describe, expect, it } from "vitest";
import { ago, bytes, duration, when } from "./format.js";

describe("bytes", () => {
  it("formats sizes with one decimal from MB up", () => {
    expect(bytes(0)).toBe("0 B");
    expect(bytes(900)).toBe("900 B");
    expect(bytes(1536)).toBe("1.5 KB");
    expect(bytes(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(bytes(2.5 * 1024 ** 3)).toBe("2.5 GB");
  });

  it("returns a dash for missing values", () => {
    expect(bytes(null)).toBe("—");
    expect(bytes(undefined)).toBe("—");
  });
});

describe("ago", () => {
  it("uses the largest whole unit", () => {
    expect(ago(5)).toBe("just now");
    expect(ago(90)).toBe("1m ago");
    expect(ago(3600)).toBe("1h ago");
    expect(ago(3600 * 5 + 60)).toBe("5h ago");
    expect(ago(86400 * 3)).toBe("3d ago");
  });

  it("returns a dash for missing values", () => {
    expect(ago(null)).toBe("—");
  });
});

describe("when", () => {
  const now = new Date("2026-09-17T12:00:00Z");

  it("shows only the time for today", () => {
    const out = when("2026-09-17T09:30:00Z", now);
    expect(out).not.toMatch(/Sep/);
    expect(out).toMatch(/\d/);
  });

  it("includes the date for another day", () => {
    expect(when("2026-09-15T09:30:00Z", now)).toMatch(/Sep/);
  });

  it("returns a dash for missing or unparseable values", () => {
    expect(when(null, now)).toBe("—");
    expect(when("not-a-date", now)).toBe("—");
  });
});

describe("duration", () => {
  it("formats sub-second, seconds and minutes", () => {
    expect(duration(0)).toBe("0ms");
    expect(duration(850)).toBe("850ms");
    expect(duration(1500)).toBe("1.5s");
    expect(duration(65000)).toBe("1m 5s");
    expect(duration(null)).toBe("—");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npm test`
Expected: FAIL — no `test` script yet (`npm error Missing script: "test"`). After Step 3's package.json edit, it fails with `Cannot find module './format.js'`.

- [ ] **Step 3: Add the harness**

`frontend/package.json` — add the two scripts and the devDependency (keep everything else byte-identical):

```json
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview",
    "test": "vitest run",
    "test:watch": "vitest"
  },
```

```json
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.1",
    "vite": "^5.3.0",
    "vitest": "^5.0.0"
  }
```

`frontend/vite.config.js` — add a `test` block after `server`:

```js
  test: {
    environment: "node",
    include: ["src/**/*.test.js"],
  },
```

`.dockerignore` at the repo root (new file):

```
# Keep host build artifacts out of the image build context: a root-owned or
# platform-specific frontend/node_modules must never shadow the image's own.
frontend/node_modules*
frontend/dist
backend/.venv
**/__pycache__
.superpowers
data
```

`Dockerfile` — frontend stage gains a test gate between install and build:

```dockerfile
# --- Stage 1: build the React SPA -------------------------------------------
FROM node:20-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json ./
RUN npm install
COPY frontend/ ./
# A red suite fails the image rather than shipping a broken SPA.
RUN npm test
RUN npm run build
```

Then install locally: `cd frontend && npm install`.

- [ ] **Step 4: Implement `frontend/src/lib/format.js`**

```js
// Display formatting shared by every page. Pure functions, no React.

const DASH = "—";

export function bytes(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return DASH;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(n);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${Math.round(value)} ${units[unit]}` : `${value.toFixed(1)} ${units[unit]}`;
}

export function ago(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return DASH;
  const s = Math.max(0, Math.floor(seconds));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function when(iso, now = new Date()) {
  if (!iso) return DASH;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return DASH;
  const time = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const sameDay =
    at.getFullYear() === now.getFullYear() &&
    at.getMonth() === now.getMonth() &&
    at.getDate() === now.getDate();
  if (sameDay) return time;
  return `${at.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

export function duration(ms) {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return DASH;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.round((ms % 60000) / 1000);
  return `${minutes}m ${seconds}s`;
}
```

- [ ] **Step 5: Point `Shared.jsx` at the lib**

In `frontend/src/components/Shared.jsx`, delete its local `bytes` implementation and re-export instead, so every existing `import { bytes } from "../components/Shared.jsx"` keeps working:

```js
export { bytes } from "../lib/format.js";
```

(Keep `TierBadge`, `Spinner`, `Empty` and `ErrorState` exactly as they are — later tasks touch them.)

- [ ] **Step 6: Run the tests and the build**

Run: `cd frontend && npm test && npm run build`
Expected: format tests pass; the SPA still builds.

- [ ] **Step 7: Commit**

```bash
git add frontend/package.json frontend/vite.config.js frontend/src/lib/format.js frontend/src/lib/format.test.js frontend/src/components/Shared.jsx .dockerignore Dockerfile
git commit -m "test(frontend): add vitest harness and a shared format module"
```

---

### Task 2: HTTP layer (`lib/http.js`) and `api.js` rebuild

**Files:**
- Create: `frontend/src/lib/http.js`, `frontend/src/lib/http.test.js`
- Modify: `frontend/src/api.js` (the `req` helper and the `api` object's method bodies; `openStream` is Task 3)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `ApiError` (with `.status`, `.detail`, `.fields`), `request(path, { method, body, signal, headers }) -> parsed JSON | null`.
- `api.js` exports are unchanged in name and signature; each method may now take a trailing optional `{ signal }`.

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/http.test.js`:

```js
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, request } from "./http.js";

function mockFetch(response) {
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function jsonResponse(status, body, { ok = status < 400 } = {}) {
  return {
    ok,
    status,
    statusText: `status ${status}`,
    json: async () => body,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("request", () => {
  it("prefixes /api and returns parsed JSON", async () => {
    const fetchMock = mockFetch(jsonResponse(200, { ok: true }));
    await expect(request("/series")).resolves.toEqual({ ok: true });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/series");
  });

  it("serializes a JSON body and sets the content type", async () => {
    const fetchMock = mockFetch(jsonResponse(200, {}));
    await request("/add", { method: "POST", body: { tvdbId: 7 } });
    const [, opts] = fetchMock.mock.calls[0];
    expect(opts.method).toBe("POST");
    expect(opts.body).toBe(JSON.stringify({ tvdbId: 7 }));
    expect(opts.headers["Content-Type"]).toBe("application/json");
  });

  it("sends no content type when there is no body", async () => {
    const fetchMock = mockFetch(jsonResponse(200, {}));
    await request("/series");
    expect(fetchMock.mock.calls[0][1].headers["Content-Type"]).toBeUndefined();
  });

  it("returns null for 204 and never parses the body", async () => {
    const json = vi.fn();
    mockFetch({ ok: true, status: 204, statusText: "No Content", json });
    await expect(request("/thing", { method: "DELETE" })).resolves.toBeNull();
    expect(json).not.toHaveBeenCalled();
  });

  it("raises ApiError carrying the status and string detail", async () => {
    mockFetch(jsonResponse(404, { detail: "Series not found" }));
    const error = await request("/series/1").catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(404);
    expect(error.message).toBe("Series not found");
    expect(error.detail).toBe("Series not found");
    expect(error.fields).toEqual({});
  });

  it("maps a 422 validation body to fields", async () => {
    mockFetch(
      jsonResponse(422, {
        detail: [
          { loc: ["body", "minSeeders"], msg: "Input should be >= 0", type: "greater_than_equal" },
          { loc: ["body"], msg: "Extra inputs are not permitted", type: "extra_forbidden" },
        ],
      }),
    );
    const error = await request("/settings/defaults", { method: "PUT", body: {} }).catch((e) => e);
    expect(error.status).toBe(422);
    expect(error.fields).toEqual({
      minSeeders: "Input should be >= 0",
      body: "Extra inputs are not permitted",
    });
    expect(error.message).toMatch(/minSeeders/);
  });

  it("falls back to statusText when the error body is not JSON", async () => {
    mockFetch({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      json: async () => {
        throw new Error("not json");
      },
    });
    const error = await request("/series").catch((e) => e);
    expect(error.status).toBe(502);
    expect(error.message).toBe("Bad Gateway");
  });

  it("passes an abort signal through to fetch", async () => {
    const fetchMock = mockFetch(jsonResponse(200, {}));
    const controller = new AbortController();
    await request("/series", { signal: controller.signal });
    expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npm test -- src/lib/http.test.js`
Expected: FAIL — `Cannot find module './http.js'`.

- [ ] **Step 3: Implement `frontend/src/lib/http.js`**

```js
// The single fetch wrapper over the backend's same-origin /api surface.
// Everything the UI knows about an HTTP failure comes from ApiError.

export class ApiError extends Error {
  constructor(message, { status, detail, fields } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status ?? 0;
    this.detail = detail ?? message;
    this.fields = fields ?? {};
  }
}

// FastAPI validation errors arrive as a list of {loc, msg}; flatten them to
// {field: message} so a form can show each message next to its own input.
function validationFields(detail) {
  const fields = {};
  for (const item of detail) {
    const loc = Array.isArray(item?.loc) ? item.loc : [];
    const name = loc.length > 1 ? String(loc[loc.length - 1]) : String(loc[0] ?? "body");
    if (item?.msg) fields[name] = item.msg;
  }
  return fields;
}

export async function request(path, { method = "GET", body, signal, headers } = {}) {
  const init = { method, headers: { ...headers } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  if (signal) init.signal = signal;

  const res = await fetch(`/api${path}`, init);

  if (!res.ok) {
    let payload = null;
    try {
      payload = await res.json();
    } catch {
      /* not a JSON body — fall back to statusText */
    }
    const detail = payload?.detail;
    if (Array.isArray(detail)) {
      const fields = validationFields(detail);
      const summary = Object.entries(fields)
        .map(([field, message]) => `${field}: ${message}`)
        .join("; ");
      throw new ApiError(summary || res.statusText, { status: res.status, detail, fields });
    }
    const message = typeof detail === "string" && detail ? detail : res.statusText;
    throw new ApiError(message, { status: res.status, detail: message });
  }

  // 204 (and any empty body) is a success with nothing to parse.
  if (res.status === 204) return null;
  return res.json();
}
```

- [ ] **Step 4: Rebuild `frontend/src/api.js` on top of it**

Replace the local `req` helper (`api.js:1-17`) with an import and a thin adapter, keeping every method name and call signature. Read the current file first and port each method body mechanically:

```js
import { request } from "./lib/http.js";

// Thin adapter so the method bodies below stay one-liners.
const get = (path, opts) => request(path, opts);
const send = (method) => (path, body, opts) => request(path, { method, body, ...opts });
const post = send("POST");
const put = send("PUT");
const del = send("DELETE");
```

Each method keeps its exact name, parameters and path building, e.g.:

```js
export const api = {
  instances: (opts) => get("/instances", opts),
  settings: (opts) => get("/settings", opts),
  setFallbackChains: (payload, opts) => put("/settings/fallback-chains", payload, opts),
  // …every existing method, ported the same way…
};
```

Rules while porting:
- Do not rename, remove or add methods (the four currently-unused ones stay).
- Keep each path string and query-string construction byte-identical.
- Methods that previously passed `{ method: "POST", body: JSON.stringify(x) }` now pass the object as `body`.
- `removeSeries` keeps its `deleteFiles` query parameter exactly as today.

- [ ] **Step 5: Run the tests and the build**

Run: `cd frontend && npm test && npm run build`
Expected: PASS. Then grep for stragglers: `grep -rn "JSON.stringify" src/api.js` should return nothing, and `grep -rn "new Error(" src/api.js` nothing.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/http.js frontend/src/lib/http.test.js frontend/src/api.js
git commit -m "refactor(frontend): one HTTP layer with typed ApiError and 204 handling"
```

---

### Task 3: Move and harden the SSE helper (`lib/stream.js`)

**Files:**
- Create: `frontend/src/lib/stream.js`, `frontend/src/lib/stream.test.js`
- Modify: `frontend/src/api.js` (delete the private `openStream`, import it instead; the five `stream*` wrappers keep their exact signatures and keep returning the cancel handle)

**Interfaces:**
- Produces: `openStream(path, params, { onStep, onResult, onError }) -> cancel` — repeated query params for array values, `null`/`undefined` skipped, closes on the first terminal event, idempotent `cancel`, and a malformed frame reported through `onError` instead of throwing inside an event handler.
- `api.js` still exports `streamSmartAdd`, `streamAdvance`, `streamReattempt`, `streamFillGaps`, `streamSpillSeason`.

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/stream.test.js`:

```js
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { openStream } from "./stream.js";

class FakeEventSource {
  static last = null;

  constructor(url) {
    this.url = url;
    this.listeners = {};
    this.closeCount = 0;
    FakeEventSource.last = this;
  }

  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }

  close() {
    this.closeCount += 1;
  }

  emit(type, data) {
    for (const fn of this.listeners[type] || []) fn({ data });
  }
}

beforeEach(() => {
  FakeEventSource.last = null;
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function open(handlers = {}) {
  const cancel = openStream("/smart-add/stream", { tvdbId: 7, seasons: [1, 2], skip: null }, handlers);
  return { cancel, es: FakeEventSource.last };
}

describe("openStream", () => {
  it("builds the url with repeated params and skips empty values", () => {
    const { es } = open();
    expect(es.url).toBe("/api/smart-add/stream?tvdbId=7&seasons=1&seasons=2");
  });

  it("dispatches step events until a terminal event arrives", () => {
    const onStep = vi.fn();
    const onResult = vi.fn();
    const { es } = open({ onStep, onResult });

    es.emit("step", JSON.stringify({ phase: "add" }));
    es.emit("result", JSON.stringify({ status: "added" }));
    es.emit("step", JSON.stringify({ phase: "late" }));

    expect(onStep).toHaveBeenCalledTimes(1);
    expect(onStep).toHaveBeenCalledWith({ phase: "add" });
    expect(onResult).toHaveBeenCalledWith({ status: "added" });
    expect(es.closeCount).toBe(1);
  });

  it("reports a server error frame's message", () => {
    const onError = vi.fn();
    const { es } = open({ onError });
    es.emit("error", JSON.stringify({ message: "profile 'SD' not found" }));
    expect(onError).toHaveBeenCalledWith("profile 'SD' not found");
    expect(es.closeCount).toBe(1);
  });

  it("falls back to a generic message for a transport error with no data", () => {
    const onError = vi.fn();
    const { es } = open({ onError });
    es.emit("error", undefined);
    expect(onError).toHaveBeenCalledWith("Stream error");
  });

  it("ignores a transport error after the stream already finished", () => {
    const onError = vi.fn();
    const { es } = open({ onResult: () => {}, onError });
    es.emit("result", JSON.stringify({ status: "added" }));
    es.emit("error", undefined);
    expect(onError).not.toHaveBeenCalled();
  });

  it("turns a malformed frame into a stream error instead of throwing", () => {
    const onStep = vi.fn();
    const onError = vi.fn();
    const { es } = open({ onStep, onError });

    expect(() => es.emit("step", "{not json")).not.toThrow();

    expect(onStep).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith("Malformed stream frame");
    expect(es.closeCount).toBe(1);
  });

  it("cancels once, however many times it is called", () => {
    const { cancel, es } = open();
    cancel();
    cancel();
    expect(es.closeCount).toBe(1);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npm test -- src/lib/stream.test.js`
Expected: FAIL — `Cannot find module './stream.js'`.

- [ ] **Step 3: Implement `frontend/src/lib/stream.js`**

```js
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
```

- [ ] **Step 4: Point `api.js` at it**

Delete the private `openStream` from `frontend/src/api.js` and import instead; the five wrappers are otherwise untouched:

```js
import { openStream } from "./lib/stream.js";
```

- [ ] **Step 5: Run the tests and the build**

Run: `cd frontend && npm test && npm run build`
Expected: PASS. `grep -n "new EventSource" src/api.js` returns nothing.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/stream.js frontend/src/lib/stream.test.js frontend/src/api.js
git commit -m "refactor(frontend): move the SSE helper to lib and survive malformed frames"
```

---

### Task 4: Dialog stream state reducer (`lib/flow.js`)

**Files:**
- Create: `frontend/src/lib/flow.js`, `frontend/src/lib/flow.test.js`

**Interfaces:**
- Produces: `initialFlow`, `flowReducer(state, action)`, selectors `isBusy(state)`, `showFooter(state)`.
- State: `{ phase: "idle" | "streaming" | "done" | "error", steps: [], result: null, error: null }`.
- Actions: `{ type: "start" }`, `{ type: "step", step }`, `{ type: "result", result }`, `{ type: "error", error }`, `{ type: "reset" }`.
- This is the fix for the stranded-dialog bug: `showFooter` depends on `phase`, never on `steps.length`.

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/flow.test.js`:

```js
import { describe, expect, it } from "vitest";
import { flowReducer, initialFlow, isBusy, showFooter } from "./flow.js";

const run = (actions, state = initialFlow) => actions.reduce(flowReducer, state);

describe("flowReducer", () => {
  it("starts empty and idle", () => {
    expect(initialFlow).toEqual({ phase: "idle", steps: [], result: null, error: null });
  });

  it("collects steps while streaming", () => {
    const state = run([{ type: "start" }, { type: "step", step: { phase: "add" } }, { type: "step", step: { phase: "search" } }]);
    expect(state.phase).toBe("streaming");
    expect(state.steps).toEqual([{ phase: "add" }, { phase: "search" }]);
  });

  it("start clears a previous run's steps and error", () => {
    const finished = run([{ type: "start" }, { type: "step", step: { phase: "add" } }, { type: "error", error: "boom" }]);
    const restarted = flowReducer(finished, { type: "start" });
    expect(restarted).toEqual({ phase: "streaming", steps: [], result: null, error: null });
  });

  it("keeps the steps when a result arrives", () => {
    const state = run([{ type: "start" }, { type: "step", step: { phase: "add" } }, { type: "result", result: { status: "added" } }]);
    expect(state.phase).toBe("done");
    expect(state.result).toEqual({ status: "added" });
    expect(state.steps).toHaveLength(1);
  });

  it("records an error without losing the steps so far", () => {
    const state = run([{ type: "start" }, { type: "step", step: { phase: "add" } }, { type: "error", error: "no releases" }]);
    expect(state.phase).toBe("error");
    expect(state.error).toBe("no releases");
    expect(state.steps).toHaveLength(1);
  });

  it("ignores late frames after a terminal event", () => {
    const done = run([{ type: "start" }, { type: "result", result: { status: "added" } }]);
    expect(flowReducer(done, { type: "step", step: { phase: "late" } })).toBe(done);
    expect(flowReducer(done, { type: "error", error: "late" })).toBe(done);
  });

  it("reset returns to idle", () => {
    const done = run([{ type: "start" }, { type: "result", result: {} }]);
    expect(flowReducer(done, { type: "reset" })).toEqual(initialFlow);
  });

  it("ignores unknown actions", () => {
    expect(flowReducer(initialFlow, { type: "nope" })).toBe(initialFlow);
  });
});

describe("selectors", () => {
  it("is busy only while streaming", () => {
    expect(isBusy({ phase: "streaming" })).toBe(true);
    expect(isBusy({ phase: "idle" })).toBe(false);
    expect(isBusy({ phase: "done" })).toBe(false);
    expect(isBusy({ phase: "error" })).toBe(false);
  });

  it("shows the footer whenever a stream is not running", () => {
    // The stranded-dialog regression: a finished or failed run must still offer
    // controls, even though its steps are still on screen.
    expect(showFooter({ phase: "idle", steps: [] })).toBe(true);
    expect(showFooter({ phase: "done", steps: [{ phase: "add" }] })).toBe(true);
    expect(showFooter({ phase: "error", steps: [{ phase: "add" }] })).toBe(true);
    expect(showFooter({ phase: "streaming", steps: [] })).toBe(false);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npm test -- src/lib/flow.test.js`
Expected: FAIL — `Cannot find module './flow.js'`.

- [ ] **Step 3: Implement `frontend/src/lib/flow.js`**

```js
// The state of one streaming dialog operation.
//
// `phase` — not `steps.length` — decides what the dialog renders, so a finished
// or failed run still shows its controls instead of stranding the user with the
// play-by-play and no way forward.

export const initialFlow = { phase: "idle", steps: [], result: null, error: null };

const TERMINAL = new Set(["done", "error"]);

export function flowReducer(state, action) {
  switch (action.type) {
    case "start":
      return { phase: "streaming", steps: [], result: null, error: null };
    case "step":
      if (state.phase !== "streaming") return state;
      return { ...state, steps: [...state.steps, action.step] };
    case "result":
      if (TERMINAL.has(state.phase)) return state;
      return { ...state, phase: "done", result: action.result ?? null };
    case "error":
      if (TERMINAL.has(state.phase)) return state;
      return { ...state, phase: "error", error: action.error ?? "Failed" };
    case "reset":
      return initialFlow;
    default:
      return state;
  }
}

export const isBusy = (state) => state.phase === "streaming";

export const showFooter = (state) => state.phase !== "streaming";
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && npm test -- src/lib/flow.test.js`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/flow.js frontend/src/lib/flow.test.js
git commit -m "feat(frontend): reducer for dialog stream state"
```

---

### Task 5: One toast host (`lib/toast.js` + `components/ui/Toast.jsx`)

**Files:**
- Create: `frontend/src/lib/toast.js`, `frontend/src/lib/toast.test.js`, `frontend/src/components/ui/Toast.jsx`
- Modify: `frontend/src/App.jsx` (mount `ToastProvider`)
- Modify: `frontend/src/pages/{SearchAdd,Library,Operations,Settings}.jsx` (delete local toast state, timers and render; use `useToast`)
- Modify: `frontend/src/components/{AddDialog,ResolveDialog,SeasonDialog,PolicyEditor}.jsx` (drop the `onToast` prop, call `useToast`)

**Interfaces:**
- Consumes: nothing.
- Produces: `initialToasts`, `toastReducer(state, action)`, `MAX_TOASTS`, `DEFAULT_TTL`; `ToastProvider`, `useToast() -> toast(msg, err?)`.
- Toast items: `{ id, msg, err }`. The host is `role="status" aria-live="polite"`; timers are cleared on dismiss and on unmount; at most `MAX_TOASTS` (3) are shown, oldest dropped.

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/toast.test.js`:

```js
import { describe, expect, it } from "vitest";
import { MAX_TOASTS, initialToasts, toastReducer } from "./toast.js";

// The id comes from the caller (the provider's ref counter), so two pushes in
// one tick can never collide the way a state-derived id would.
const push = (state, id, msg, err) => toastReducer(state, { type: "push", id, msg, err });

describe("toastReducer", () => {
  it("starts empty", () => {
    expect(initialToasts.items).toEqual([]);
  });

  it("appends the item under the id it was given", () => {
    const one = push(initialToasts, 1, "saved");
    const two = push(one, 2, "removed", true);
    expect(one.items[0]).toEqual({ id: 1, msg: "saved", err: false });
    expect(two.items.map((t) => t.id)).toEqual([1, 2]);
    expect(two.items[1].err).toBe(true);
  });

  it("drops the oldest past the cap", () => {
    let state = initialToasts;
    ["a", "b", "c", "d"].forEach((msg, i) => {
      state = push(state, i + 1, msg);
    });
    expect(state.items).toHaveLength(MAX_TOASTS);
    expect(state.items.map((t) => t.msg)).toEqual(["b", "c", "d"]);
  });

  it("dismisses by id and ignores unknown ids", () => {
    const two = push(push(initialToasts, 1, "a"), 2, "b");
    const one = toastReducer(two, { type: "dismiss", id: 1 });
    expect(one.items.map((t) => t.msg)).toEqual(["b"]);
    expect(toastReducer(one, { type: "dismiss", id: 99 })).toBe(one);
  });

  it("ignores unknown actions", () => {
    expect(toastReducer(initialToasts, { type: "nope" })).toBe(initialToasts);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npm test -- src/lib/toast.test.js`
Expected: FAIL — `Cannot find module './toast.js'`.

- [ ] **Step 3: Implement `frontend/src/lib/toast.js`**

```js
// Toast queue state. Pure: the provider owns the ids and the timers, this owns
// the list. Ids are supplied by the caller so a push never has to derive one
// from state it may not have seen yet.

export const MAX_TOASTS = 3;
export const DEFAULT_TTL = 4200;

export const initialToasts = { items: [] };

export function toastReducer(state, action) {
  switch (action.type) {
    case "push": {
      const items = [...state.items, { id: action.id, msg: action.msg, err: Boolean(action.err) }];
      return { items: items.slice(-MAX_TOASTS) };
    }
    case "dismiss": {
      const items = state.items.filter((t) => t.id !== action.id);
      return items.length === state.items.length ? state : { items };
    }
    default:
      return state;
  }
}
```

- [ ] **Step 4: Implement `frontend/src/components/ui/Toast.jsx`**

```jsx
import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef } from "react";
import { DEFAULT_TTL, initialToasts, toastReducer } from "../../lib/toast.js";

const ToastContext = createContext(() => {});

export function useToast() {
  return useContext(ToastContext);
}

export function ToastProvider({ children }) {
  const [state, dispatch] = useReducer(toastReducer, initialToasts);
  const timers = useRef(new Map());

  const dismiss = useCallback((id) => {
    const handle = timers.current.get(id);
    if (handle) {
      clearTimeout(handle);
      timers.current.delete(id);
    }
    dispatch({ type: "dismiss", id });
  }, []);

  // Ids come from a ref, not from state: two toasts raised in the same tick must
  // get different ids, or one timer would dismiss the other's toast.
  const lastId = useRef(0);

  const toast = useCallback(
    (msg, err) => {
      const id = (lastId.current += 1);
      dispatch({ type: "push", id, msg, err });
      timers.current.set(id, setTimeout(() => dismiss(id), DEFAULT_TTL));
      return id;
    },
    [dismiss],
  );

  // Every pending timer dies with the provider: a late timer must never fire
  // into an unmounted tree (the old per-page toasts never cleared theirs).
  useEffect(
    () => () => {
      timers.current.forEach((handle) => clearTimeout(handle));
      timers.current.clear();
    },
    [],
  );

  const value = useMemo(() => toast, [toast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-host" role="status" aria-live="polite">
        {state.items.map((t) => (
          <div key={t.id} className={`toast ${t.err ? "err" : ""}`} onClick={() => dismiss(t.id)}>
            {t.msg}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
```

`styles.css`: keep the existing `.toast` rules; add a `.toast-host` wrapper that stacks them (`position: fixed`, bottom-right, `display: flex; flex-direction: column; gap: var(--s-2)`, `z-index: var(--z-toast)`) and make `.toast` `position: static` inside it. Task 9 owns the tokens; if it has not run yet, use the existing literals and let Task 9 tokenise.

- [ ] **Step 5: Mount the provider and migrate every caller**

`App.jsx`: wrap the shell contents in `<ToastProvider>` (inside the router, outside `<Routes>`).

In each of `pages/SearchAdd.jsx`, `pages/Library.jsx`, `pages/Operations.jsx`, `pages/Settings.jsx`:
- delete the `const [toast, setToast] = useState(null)` declaration, the `showToast` helper and its `setTimeout`, and the `{toast && <div className="toast" …>}` render;
- add `const toast = useToast();` and replace every `showToast(msg, err)` call with `toast(msg, err)`;
- in `Library.jsx` also replace `confirmRemove`'s direct `setToast({...})` calls and its `finally` `setTimeout` with `toast(...)` calls.

In `AddDialog.jsx`, `ResolveDialog.jsx`, `SeasonDialog.jsx`, `PolicyEditor.jsx`:
- remove `onToast` from the props list and use `const toast = useToast();`, replacing `onToast?.(…)`/`onToast(…)` with `toast(…)`;
- remove the now-unused `onToast={…}` props where the pages render these dialogs.

- [ ] **Step 6: Verify**

Run: `cd frontend && npm test && npm run build`
Then confirm the migration is complete:
- `grep -rn "setToast\|showToast\|onToast" src/` → no matches.
- `grep -rn "setTimeout" src/pages src/components` → only `useDialog.js` (if any) and nothing toast-related.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/toast.js frontend/src/lib/toast.test.js frontend/src/components/ui/Toast.jsx frontend/src/App.jsx frontend/src/pages frontend/src/components frontend/src/styles.css
git commit -m "refactor(frontend): one toast host with cleared timers and a live region"
```

---

### Task 6: One dialog skeleton, focus restoration, and no more leaked streams

**Files:**
- Create: `frontend/src/components/ui/Dialog.jsx`
- Modify: `frontend/src/components/useDialog.js` (restore focus to the opener)
- Modify: `frontend/src/components/ConfirmDialog.jsx`, `PolicyEditor.jsx` (skeleton only), `AddDialog.jsx`, `ResolveDialog.jsx`, `SeasonDialog.jsx` (skeleton + `flow` + stream cancel)
- Modify: `frontend/src/styles.css` (two new rules: `.dialog-head` layout for the close button, `.dialog-close`)

**Interfaces:**
- Consumes: `flow.js` (Task 4), `useToast` (Task 5).
- Produces: `Dialog` (default export) with props `{ title, subtitle, label, onClose, busy, footer, maxWidth, children }` and the named export `DIALOG_TRANSITION`.
- Every dialog renders a header close button. `ResolveDialog` gains both a close button and a footer.
- No dialog computes `streaming || steps.length > 0` any more; footers are gated by `showFooter(flow)`.

- [ ] **Step 1: Create `frontend/src/components/ui/Dialog.jsx`**

Import `useDialog` exactly the way the existing dialogs do (match its export style).

```jsx
import { motion } from "framer-motion";
import useDialog from "../useDialog.js";
import { Spinner } from "../Shared.jsx";

export const DIALOG_TRANSITION = { duration: 0.2, ease: [0.2, 0.7, 0.2, 1] };

// The one dialog skeleton: scrim, focus-trapped panel, head/body/foot. The close
// button is always rendered — a dialog must never leave the user without an exit.
export default function Dialog({ title, subtitle, label, onClose, busy = false, footer, maxWidth, children }) {
  const dialogRef = useDialog(onClose);

  return (
    <div className="scrim" onClick={onClose}>
      <motion.div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={label || title}
        tabIndex={-1}
        className="dialog"
        style={maxWidth ? { maxWidth } : undefined}
        onClick={(e) => e.stopPropagation()}
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={DIALOG_TRANSITION}
      >
        <div className="dialog-head">
          <div className="dialog-head-text">
            <h2>{title}</h2>
            {subtitle ? <div className="meta dialog-sub">{subtitle}</div> : null}
          </div>
          {busy ? <Spinner /> : null}
          <button type="button" className="dialog-close" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="dialog-body">{children}</div>
        {footer ? <div className="dialog-foot">{footer}</div> : null}
      </motion.div>
    </div>
  );
}
```

`styles.css` — add next to the existing `.dialog-head` rule:

```css
.dialog-head { display: flex; align-items: flex-start; gap: 12px; }
.dialog-head-text { flex: 1; min-width: 0; }
.dialog-sub { margin-top: 6px; }
.dialog-close {
  background: transparent; border: 1px solid var(--line); color: var(--ink-dim);
  border-radius: var(--radius-sm); width: 30px; height: 30px; cursor: pointer; flex: none;
}
.dialog-close:hover { color: var(--ink); border-color: var(--ink-faint); }
```

(Fold these into the single merged `.dialog-head` rule Task 9 produces if Task 9 has already run.)

- [ ] **Step 2: Restore focus in `useDialog.js`**

Capture the opener before focusing into the dialog and restore it on close — today focus drops to `<body>`, which is visible on the Library poster grid:

```js
  useEffect(() => {
    const opener = document.activeElement;
    // …existing stack push, initial focus, keydown listener…
    return () => {
      // …existing listener removal and stack splice…
      if (opener && document.contains(opener) && typeof opener.focus === "function") {
        opener.focus();
      }
    };
  }, []);
```

- [ ] **Step 3: Convert `ConfirmDialog.jsx` (the worked example)**

Replace its scrim/motion/head/body/foot skeleton with `Dialog`, keeping every prop, label and behaviour it has today:

```jsx
import { useState } from "react";
import Dialog from "./ui/Dialog.jsx";

export default function ConfirmDialog({
  title, body, confirmLabel = "Confirm", cancelLabel = "Cancel",
  checkboxLabel, danger, busy, onConfirm, onCancel,
}) {
  const [checked, setChecked] = useState(false);

  return (
    <Dialog
      title={title}
      onClose={onCancel}
      maxWidth={440}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`btn ${danger ? "danger" : "primary"}`}
            onClick={() => onConfirm(checked)}
            disabled={busy}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      <p className="muted">{body}</p>
      {checkboxLabel ? (
        <label className="check-row">
          <input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} />
          {checkboxLabel}
        </label>
      ) : null}
    </Dialog>
  );
}
```

Read the current file first and keep its exact prop names, defaults, labels and the `danger`/`busy` handling; only the skeleton changes.

- [ ] **Step 4: Convert `PolicyEditor.jsx`**

Same mechanical change: `Dialog` with `title`/`subtitle` from its current `.dialog-head` content, its existing footer buttons passed as `footer`, and its body unchanged. It has no streaming, so no `flow`.

- [ ] **Step 5: Convert the three streaming dialogs**

For `AddDialog.jsx`, `ResolveDialog.jsx` and `SeasonDialog.jsx`, in each file:

1. First create the shared hook `frontend/src/components/ui/useStreamFlow.js`, so the run/cancel logic exists once rather than being pasted into three dialogs:

```jsx
import { useCallback, useEffect, useReducer, useRef } from "react";
import { flowReducer, initialFlow, isBusy, showFooter } from "../../lib/flow.js";
import { useToast } from "./Toast.jsx";

// One streaming operation for a dialog: reducer state plus the live EventSource's
// cancel handle. Unmounting cancels the stream, so its handlers can never fire
// into a tree that is gone, and starting a run cancels any previous one.
export function useStreamFlow() {
  const [flow, dispatch] = useReducer(flowReducer, initialFlow);
  const cancelRef = useRef(null);
  const toast = useToast();

  const cancel = useCallback(() => {
    cancelRef.current?.();
    cancelRef.current = null;
  }, []);

  useEffect(() => cancel, [cancel]);

  const run = useCallback(
    (streamFn, params, { onResult, onError } = {}) => {
      cancel();
      dispatch({ type: "start" });
      cancelRef.current = streamFn(params, {
        onStep: (step) => dispatch({ type: "step", step }),
        onResult: (result) => {
          cancelRef.current = null;
          dispatch({ type: "result", result });
          onResult?.(result);
        },
        onError: (error) => {
          cancelRef.current = null;
          dispatch({ type: "error", error });
          if (onError) onError(error);
          else toast(error, true);
        },
      });
    },
    [cancel, toast],
  );

  return { flow, run, cancel, busy: isBusy(flow), canShowFooter: showFooter(flow) };
}
```

2. In each dialog, delete the local `streaming`/`steps` state and the `const inFlight = streaming || steps.length > 0` line (`AddDialog.jsx:231`, `ResolveDialog.jsx:128`, `SeasonDialog.jsx:100`), take `const { flow, run, busy, canShowFooter } = useStreamFlow();`, and replace every read: `steps` → `flow.steps`, `streaming` → `busy`, `!inFlight && !loading` → `canShowFooter && !loading`. Pass `busy={busy}` to `Dialog`.
3. Route every existing stream call through `run(streamFn, params, { onResult: … })` (`AddDialog`'s smart-add at `:194` and its `runStream` at `:142-147`; `ResolveDialog`'s auto-reattempt effect at `:80-92` and `dispatchOption`; `SeasonDialog`'s spill at `:68`), keeping each one's terminal handling (`handleTerminal`, query invalidations, toasts) exactly as it is today. Where a dialog wants its own error handling instead of the default toast, pass `onError`.
4. `ResolveDialog` gains a footer: a single `Close` button when `showFooter(flow)`.
5. Keep the existing `useRef` latch that guards `ResolveDialog`'s auto-reattempt against StrictMode double-effects, and keep each file's `eslint-disable-next-line react-hooks/exhaustive-deps` comments where the deps are deliberately narrow.

- [ ] **Step 6: Verify**

Run: `cd frontend && npm test && npm run build`

Then confirm the conversion is complete:
- `grep -rn 'role="dialog"' src/` → only `src/components/ui/Dialog.jsx`.
- `grep -rn "steps.length > 0" src/` → no matches.
- `grep -rn "className=\"scrim\"" src/components` → only `ui/Dialog.jsx`.
- `grep -rn "dialog-close" src/components/ui/Dialog.jsx` → one match (every dialog now has an exit).
- `grep -rn "cancelRef" src/components` → three matches (the three streaming dialogs).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components frontend/src/styles.css
git commit -m "fix(frontend): one dialog with an always-present exit; cancel streams on close"
```

---

### Task 7: Tier registry (`lib/tiers.js`)

**Files:**
- Create: `frontend/src/lib/tiers.js`, `frontend/src/lib/tiers.test.js`, `frontend/src/components/ui/TierProvider.jsx`
- Modify: `frontend/src/api.js` (re-export `tierClass` from the lib instead of defining it), `frontend/src/components/Shared.jsx` (drop the local `LABELS` map), `frontend/src/pages/Library.jsx:141`, `frontend/src/components/SeasonDialog.jsx:155,176`, `frontend/src/App.jsx` (mount the provider)

**Interfaces:**
- Produces: `buildTiers(instances) -> { [id]: { id, name, label, cls } }`, `tierClass(id, registry?)`, `tierLabel(id, registry?)`; `TierProvider` + `useTiers()`.
- `api.js` keeps exporting `tierClass` with its current signature, so the ten existing import sites do not change.
- **Ruling recorded in the spec:** the registry does *not* invent new `--tier-n` palette classes (the CSS has three tier classes and inventing more is 5B/5C's business). It removes the duplicated label maps and the casing split, and gives an unknown instance its own name as a label instead of an empty string.

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/tiers.test.js`:

```js
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
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npm test -- src/lib/tiers.test.js`
Expected: FAIL — `Cannot find module './tiers.js'`.

- [ ] **Step 3: Implement `frontend/src/lib/tiers.js`**

```js
// Which instance is which, in one place: CSS class plus the short badge label.
// The registry is built from the instance list the backend reports; the id sniff
// below is the fallback for an instance the registry hasn't loaded yet.

const KNOWN = [
  { test: (id) => id.includes("4k"), cls: "t4k", label: "4K" },
  { test: (id) => id.includes("1080"), cls: "t1080p", label: "1080P" },
];

function sniff(instanceId) {
  const id = (instanceId || "").toLowerCase();
  return KNOWN.find((k) => k.test(id)) || { cls: "tdefault", label: null };
}

export function buildTiers(instances = []) {
  const registry = {};
  for (const inst of instances || []) {
    if (!inst?.id) continue;
    const { cls, label } = sniff(inst.id);
    registry[inst.id] = { id: inst.id, name: inst.name || inst.id, label: label || inst.name || inst.id, cls };
  }
  return registry;
}

export function tierClass(instanceId, registry) {
  return registry?.[instanceId]?.cls || sniff(instanceId).cls;
}

export function tierLabel(instanceId, registry) {
  const known = registry?.[instanceId];
  if (known) return known.label;
  return sniff(instanceId).label || instanceId || "";
}
```

- [ ] **Step 4: Create `frontend/src/components/ui/TierProvider.jsx`**

```jsx
import { createContext, useContext, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api.js";
import { buildTiers, tierClass, tierLabel } from "../../lib/tiers.js";

const TierContext = createContext({});

export function useTiers() {
  const registry = useContext(TierContext);
  return useMemo(
    () => ({
      registry,
      cls: (id) => tierClass(id, registry),
      label: (id) => tierLabel(id, registry),
    }),
    [registry],
  );
}

export function TierProvider({ children }) {
  // Reuses the settings query the Settings page and AddDialog already run.
  const { data } = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const registry = useMemo(() => buildTiers(data?.instances), [data]);
  return <TierContext.Provider value={registry}>{children}</TierContext.Provider>;
}
```

- [ ] **Step 5: Replace the duplicated maps**

- `api.js`: delete the `tierClass` function body and re-export instead, so every existing importer is untouched:
  ```js
  export { tierClass } from "./lib/tiers.js";
  ```
- `Shared.jsx`: delete the local `const LABELS = {...}` and have `TierBadge` call `tierLabel(instanceId)` (keep the component's props and markup).
- `Library.jsx:141`: replace the inlined ternary `shortTier` with `label(s.instanceId)` from `useTiers()`.
- `SeasonDialog.jsx:155` and `:176`: replace both inlined ternaries with `label(t)` / `label(target)`; this also removes the double `tierClass()` calls per render and fixes `"1080p"` vs `"1080P"`.
- `App.jsx`: mount `<TierProvider>` inside the router (it may sit inside `ToastProvider`).

- [ ] **Step 6: Verify**

Run: `cd frontend && npm test && npm run build`
Then: `grep -rn '"1080P"\|"1080p"' src/ | grep -v lib/tiers` → no matches outside the registry (the CSS class string `t1080p` is fine), and `grep -rn "LABELS" src/` → no matches.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/tiers.js frontend/src/lib/tiers.test.js frontend/src/components/ui/TierProvider.jsx frontend/src/api.js frontend/src/components frontend/src/pages frontend/src/App.jsx
git commit -m "refactor(frontend): one tier registry for classes and labels"
```

---

### Task 8: URL state (`lib/urlState.js`) and Library filters that survive a reload

**Files:**
- Create: `frontend/src/lib/urlState.js`, `frontend/src/lib/urlState.test.js`
- Modify: `frontend/src/pages/Library.jsx` (its search text, sort, tier filter and grid/table toggle move into the URL)

**Interfaces:**
- Produces: `parse(searchParams, defaults)`, `serialize(state, defaults, existing?)`, `useUrlState(defaults) -> [state, setState]`.
- Types are coerced from the shape of `defaults` (string / number / boolean). A value equal to its default is omitted from the query string. Query keys the app doesn't own are preserved.

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/urlState.test.js`:

```js
import { describe, expect, it } from "vitest";
import { parse, serialize } from "./urlState.js";

const DEFAULTS = { q: "", sort: "title", view: "grid", tier: "all", page: 1, missingOnly: false };

describe("parse", () => {
  it("returns the defaults for an empty query", () => {
    expect(parse(new URLSearchParams(""), DEFAULTS)).toEqual(DEFAULTS);
  });

  it("coerces each value to the type of its default", () => {
    const state = parse(new URLSearchParams("q=mad&page=3&missingOnly=1"), DEFAULTS);
    expect(state.q).toBe("mad");
    expect(state.page).toBe(3);
    expect(state.missingOnly).toBe(true);
  });

  it("falls back to the default for an unparseable number or unknown key", () => {
    const state = parse(new URLSearchParams("page=abc&nope=1"), DEFAULTS);
    expect(state.page).toBe(1);
    expect(state).not.toHaveProperty("nope");
  });

  it("treats 0 and false as false for booleans", () => {
    expect(parse(new URLSearchParams("missingOnly=0"), DEFAULTS).missingOnly).toBe(false);
    expect(parse(new URLSearchParams("missingOnly=false"), DEFAULTS).missingOnly).toBe(false);
  });
});

describe("serialize", () => {
  it("omits values equal to their default", () => {
    expect(serialize(DEFAULTS, DEFAULTS).toString()).toBe("");
  });

  it("writes only what differs", () => {
    const qs = serialize({ ...DEFAULTS, sort: "missing", missingOnly: true }, DEFAULTS);
    expect(qs.get("sort")).toBe("missing");
    expect(qs.get("missingOnly")).toBe("1");
    expect(qs.get("view")).toBeNull();
  });

  it("round-trips through parse", () => {
    const state = { ...DEFAULTS, q: "mad men", page: 2, view: "table" };
    expect(parse(serialize(state, DEFAULTS), DEFAULTS)).toEqual(state);
  });

  it("keeps query keys it doesn't own", () => {
    const existing = new URLSearchParams("ref=email&sort=stale");
    const qs = serialize({ ...DEFAULTS, sort: "missing" }, DEFAULTS, existing);
    expect(qs.get("ref")).toBe("email");
    expect(qs.get("sort")).toBe("missing");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npm test -- src/lib/urlState.test.js`
Expected: FAIL — `Cannot find module './urlState.js'`.

- [ ] **Step 3: Implement `frontend/src/lib/urlState.js`**

```js
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
```

- [ ] **Step 4: Wire `Library.jsx`**

Replace the four pieces of local view state with one URL-backed object. Define the defaults **outside** the component (a new object each render would churn the memo):

```jsx
import { useUrlState } from "../lib/urlState.js";

const LIBRARY_DEFAULTS = { q: "", sort: "title", tier: "all", view: "grid" };

  const [view, setView] = useUrlState(LIBRARY_DEFAULTS);
```

Then read `view.q` / `view.sort` / `view.tier` / `view.view` where the old state was read, and call `setView({ q: e.target.value })` etc. where it was set. Keep every filter's behaviour, option list and sort comparator exactly as it is today — this task moves where the state lives, nothing else. Dialog-open state stays in `useState` (it is not linkable yet; the Series detail route in 5C changes that).

- [ ] **Step 5: Verify**

Run: `cd frontend && npm test && npm run build`
Manual: open `/library`, type a search, switch to the table view, pick a tier, reload — the filters survive and the URL shows only what differs from the defaults.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/urlState.js frontend/src/lib/urlState.test.js frontend/src/pages/Library.jsx
git commit -m "feat(frontend): keep Library filters in the URL"
```

---

### Task 9: Design tokens and CSS hygiene

**Files:**
- Modify: `frontend/src/styles.css`, `frontend/src/main.jsx` (or `App.jsx`) for `MotionConfig`
- Modify (inline styles only): `frontend/src/pages/{SearchAdd,Library,Operations,Activity}.jsx`, `frontend/src/components/{AddDialog,ResolveDialog,SeasonDialog,PolicyEditor,ConfirmDialog,Shared}.jsx`
- **Not** `frontend/src/pages/Settings.jsx` (excluded by the Global Constraints)

**Interfaces:**
- Produces: spacing, type-scale, status, episode-state, focus-ring and z-index tokens; a `:focus-visible` rule; a `prefers-reduced-motion` block; utility classes replacing the repeated inline styles.
- No selector JSX depends on is renamed.

- [ ] **Step 1: Extend `:root`**

Append to the existing `:root` block (keep all 17 current tokens as they are):

```css
  /* Spacing scale — every gap/padding in the app comes from here. */
  --s-1: 4px;  --s-2: 8px;  --s-3: 12px; --s-4: 16px;
  --s-5: 24px; --s-6: 32px; --s-7: 48px; --s-8: 64px;

  /* Type scale */
  --fs-xs: 11px; --fs-sm: 12px; --fs-md: 14px; --fs-lg: 18px; --fs-xl: 24px;

  /* Status semantics (distinct from tier identity) */
  --st-ok: var(--ok);
  --st-warn: var(--t-4k);
  --st-err: var(--danger);
  --st-info: var(--t-1080);
  --st-muted: var(--ink-faint);

  /* Per-episode states — consumed by the Series detail grid in a later phase */
  --ep-imported: var(--ok);
  --ep-wanted: var(--ink-dim);
  --ep-searching: var(--t-1080);
  --ep-grabbed: var(--t-1080-dim);
  --ep-failed: var(--danger);
  --ep-unavailable: var(--t-4k);
  --ep-unmonitored: var(--ink-faint);

  /* One dark overlay instead of three near-identical rgba() literals */
  --overlay: rgba(8, 9, 12, 0.9);
  --scrim: rgba(6, 7, 10, 0.66);

  /* Accent alphas, so rgba(45,212,191,…) stops being retyped */
  --t-1080-a08: rgba(45, 212, 191, 0.08);
  --t-1080-a16: rgba(45, 212, 191, 0.16);
  --t-4k-a08: rgba(245, 166, 35, 0.08);
  --t-4k-a16: rgba(245, 166, 35, 0.16);
  --danger-a10: rgba(239, 91, 110, 0.1);
  --danger-a20: rgba(239, 91, 110, 0.2);
  --danger-line: #6e2230;

  --focus-ring: 0 0 0 2px var(--bg), 0 0 0 4px var(--t-1080);
  --z-scrim: 50; --z-dialog: 51; --z-toast: 80; --z-grain: 999;
```

- [ ] **Step 2: Replace the literals that duplicate a token**

Mechanical, one commit-able sweep (`grep -n` each literal):

| Literal | Replace with | Occurrences |
|---|---|---|
| `#0c0d11` (outside `:root`) | `var(--bg)` | 2 |
| `rgba(74, 222, 128, …)` | `var(--ok)` at the same alpha via `color-mix` is overkill — use `var(--st-ok)` for solid, keep alpha variants as `rgba(78,201,154,…)` | 2 |
| `rgba(239, 68, 68, 0.4)` | `var(--danger-a20)` | 1 |
| `rgba(6, 7, 10, 0.66)` | `var(--scrim)` | 1 |
| `rgba(8, 9, 12, 0.88)` / `rgba(12, 13, 17, 0.82)` | `var(--overlay)` | 3 |
| `#6e2230` | `var(--danger-line)` | 6 |
| `rgba(45, 212, 191, 0.08\|0.16)` | `var(--t-1080-a08)` / `var(--t-1080-a16)` | the exact-match subset |
| `rgba(245, 166, 35, 0.08\|0.16)` | `var(--t-4k-a08)` / `var(--t-4k-a16)` | the exact-match subset |
| `z-index: 50\|80\|999` | `var(--z-scrim)` / `var(--z-toast)` / `var(--z-grain)` | 3 |

Leave the deliberate one-offs (`#b9c2cf` gradient stop, `#ffc14d`/`#4fe3d0`/`#58e0cf` poster chips, `#313947` hover border, `#232834`) and add a one-line comment above each saying it is intentional.

- [ ] **Step 3: Merge the duplicate selectors and delete dead rules**

Merge each of these into a single rule at the **first** location, moving the later declarations in (later wins where they conflict, which is what renders today):

- `.page-title` — `:170` + `:723` (keep the `:723` font-size and gradient)
- `.tier` — `:222` + `:741`
- `.dialog-head` — `:458` + `:878` (plus the flex rule Task 6 added)
- `.op-card` — `:734` + `:1007`

Delete the dead `.fallback` and `.fallback .actions` rules (`:539-567`, `:1729`) — no JSX uses that class. Leave the `@media` overrides alone; those are intentional.

- [ ] **Step 4: Add focus-visible and reduced-motion**

```css
/* Keyboard focus was invisible everywhere: only .input:focus existed. */
.btn:focus-visible,
.nav a:focus-visible,
.seg:focus-visible,
.op-summary:focus-visible,
.view-toggle button:focus-visible,
.dialog-close:focus-visible,
.topbar-burger:focus-visible,
.poster-policy:focus-visible,
.poster-seasons:focus-visible,
.poster-resolve:focus-visible,
.poster-del:focus-visible,
.row-resolve:focus-visible,
.row-del:focus-visible,
.skip-link:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

/* Hover-only poster buttons must also appear for keyboard users. */
.poster-card:focus-within .poster-policy,
.poster-card:focus-within .poster-seasons,
.poster-card:focus-within .poster-resolve,
.poster-card:focus-within .poster-del {
  opacity: 1;
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    animation-duration: 0.001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.001ms !important;
  }
  body::before,
  body::after {
    display: none; /* gradient mesh + grain */
  }
}
```

Check the poster card's actual class name before writing the `:focus-within` block and use whatever it is.

- [ ] **Step 5: Honour reduced motion in framer-motion**

Wrap the app once (in `main.jsx` around `<App />`, or in `App.jsx` around the shell):

```jsx
import { MotionConfig } from "framer-motion";

  <MotionConfig reducedMotion="user">…</MotionConfig>
```

- [ ] **Step 6: Move the repeated inline styles into classes**

Add these utilities and replace the inline styles listed (all outside `Settings.jsx`):

```css
.pad-lg { padding: var(--s-5); }
.panel-clip { overflow: hidden; }
.stack-row { display: flex; gap: var(--s-2); }
.note-inset { padding: var(--s-2) 0; color: var(--ink-dim); }
```

| Inline style | Replace with | Sites |
|---|---|---|
| `style={{ padding: 24 }}` (spinner wrapper) | `className="pad-lg"` | `SearchAdd.jsx:69`, `Library.jsx:125`, `Operations.jsx:125` |
| `style={{ overflow: "hidden" }}` on `.panel` | add `panel-clip` | `Activity.jsx:27`, `Library.jsx:210` |
| `style={{ padding: "8px 0", color: "var(--ink-dim)" }}` | `className="note-inset"` | `ResolveDialog.jsx:156`, `SeasonDialog.jsx:128,136`, `AddDialog.jsx:274` |
| `style={{ display: "flex", gap: 8 }}` | `className="stack-row"` | `PolicyEditor.jsx:186,194` |
| `style={{ marginTop: 6 }}` on dialog `.meta` | `.dialog-sub` (Task 6) | the four dialog heads |
| `style={{ maxWidth: 440 }}` | `Dialog`'s `maxWidth` prop (Task 6) | `ConfirmDialog.jsx:29` |

Leave the genuinely one-off inline colours (`Library.jsx:192,240,243`, `Activity.jsx:70-74`, `AddDialog.jsx:334`) — those become tokens in 5C when those views are rebuilt.

- [ ] **Step 7: Verify**

Run: `cd frontend && npm test && npm run build`
Then:
- `grep -c "style={{" src/pages/*.jsx src/components/*.jsx` → `Settings.jsx` still 30, every other file reduced (~36 → ~12 remaining one-offs).
- `grep -n "focus-visible" src/styles.css` → the new block exists.
- `grep -n "prefers-reduced-motion" src/styles.css` → one block.
- `grep -cn "#6e2230\|rgba(6, 7, 10" src/styles.css` → 0.
- Manual: tab through Library and a dialog (visible focus ring), enable the OS "reduce motion" setting and reload (no entrance animations, no grain).

- [ ] **Step 8: Commit**

```bash
git add frontend/src/styles.css frontend/src/main.jsx frontend/src/App.jsx frontend/src/pages frontend/src/components
git commit -m "style(frontend): design tokens, focus-visible, reduced motion, fewer inline styles"
```

---

### Task 10: Accessibility fixes and a 404 route

**Files:**
- Create: `frontend/src/components/ui/NotFound.jsx`
- Modify: `frontend/src/components/AddDialog.jsx` (tier toggle + selects), `PolicyEditor.jsx` (four controls), `Shared.jsx` (`Spinner`), `ProgressStream.jsx` (live region), `App.jsx` (skip link, nav glyphs, 404 route, drawer), `frontend/src/pages/SearchAdd.jsx` (search input), `frontend/src/pages/Settings.jsx` (`aria-describedby` only), `frontend/src/styles.css` (`.skip-link`)

**Interfaces:**
- Produces: a keyboard-operable tier toggle, named form controls, an announcing spinner and progress list, a skip link, a focus-contained mobile drawer, and a catch-all route.

- [ ] **Step 1: Make the Add dialog's tier toggle a real checkbox**

`AddDialog.jsx:317` is a `div` with `onClick` — a functional multi-select that no keyboard can reach. Convert it to the same pattern the season chips already use at `:299`, keeping the class names so the CSS still applies:

```jsx
<label key={i.id} className={`tier-toggle ${tierClass(i.id)} ${on ? "on" : ""}`}>
  <input
    type="checkbox"
    className="tier-toggle-input"
    checked={on}
    onChange={() => toggle(i.id)}
  />
  <span className="tier-toggle-label">{label(i.id)}</span>
  …existing meta/opts markup…
</label>
```

- `label` here is from `const { label } = useTiers();` (Task 7's hook, imported from `./ui/TierProvider.jsx`); keep whatever the toggle renders today if Task 7 has not run yet.
- The nested `.opts` block keeps its `onClick={(e) => e.stopPropagation()}` so changing a select doesn't toggle the tier.
- Add `.tier-toggle-input { position: absolute; opacity: 0; pointer-events: none; }` and a `.tier-toggle:has(.tier-toggle-input:focus-visible) { box-shadow: var(--focus-ring); }` rule; keep the existing `✓` indicator driven by the `on` class.
- Give the two nested selects `aria-label="Quality profile"` (`:322`) and `aria-label="Root folder"` (`:327`).

- [ ] **Step 2: Name the remaining controls**

- `PolicyEditor.jsx`: `aria-label="Preferred tier"` (`:129`), `aria-label="Fallback instance"` (`:151`), `aria-label="Quality profile name"` (`:160`), `aria-label="Spill after this many days"` (`:166`).
- `SearchAdd.jsx:59`: `aria-label="Search for a TV show"`.
- `Settings.jsx`: give each help `<div className="muted">` an `id` and point its field's control at it with `aria-describedby` (`:242, 258, 272, 288, 305`). **Touch nothing else in this file.**

- [ ] **Step 3: Make status announce**

- `Shared.jsx` `Spinner`: `<span className="spinner" role="status" aria-label="Loading" />` (an `aria-label` on a role-less `span` is ignored today).
- `ProgressStream.jsx:47`: add `role="status" aria-live="polite"` to the steps container so each new phase is announced.

- [ ] **Step 4: Shell fixes in `App.jsx`**

- Skip link as the first child of `.shell`: `<a className="skip-link" href="#main">Skip to content</a>`, and give `<main className="main" id="main" tabIndex={-1}>` the target.
- Nav icons: `<span className="ico" aria-hidden="true">{n.ico}</span>`.
- Mobile drawer: call `useDialog(closeDrawer)` when `drawerOpen` is true so Escape closes it and focus is contained (the drawer is a modal overlay today with neither), and give the burger `aria-controls` pointing at the sidebar's `id`.
- Catch-all route, last inside `<Routes>`: `<Route path="*" element={<NotFound />} />`.

`styles.css`:

```css
.skip-link {
  position: absolute; left: -9999px; top: var(--s-2);
  background: var(--panel); color: var(--ink);
  padding: var(--s-2) var(--s-3); border-radius: var(--radius-sm);
  border: 1px solid var(--line); z-index: var(--z-toast);
}
.skip-link:focus { left: var(--s-2); }
```

- [ ] **Step 5: Create `frontend/src/components/ui/NotFound.jsx`**

```jsx
import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">Not found</h1>
      </div>
      <div className="panel pad-lg">
        <p className="muted">That page doesn’t exist.</p>
        <Link className="btn" to="/">
          Back to search
        </Link>
      </div>
    </>
  );
}
```

- [ ] **Step 6: Verify**

Run: `cd frontend && npm test && npm run build`

Manual checks (record them in the report):
- In the Add dialog, Tab reaches each tier toggle and Space toggles it; the selects announce their names.
- With a screen reader or the browser's accessibility inspector, the spinner exposes a "Loading" status and new progress steps are announced.
- Tab from the top of any page reveals "Skip to content" and it jumps to `<main>`.
- On a narrow window, open the drawer: Escape closes it and Tab stays inside it.
- Visit `/nope` → the Not found panel renders instead of a blank page.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components frontend/src/pages frontend/src/App.jsx frontend/src/styles.css
git commit -m "fix(frontend): keyboard-operable controls, announced status, skip link, 404 route"
```

---

## Verification (whole phase)

1. `cd frontend && npm test` — all lib suites green (format, http, stream, flow, toast, tiers, urlState).
2. `cd frontend && npm run build` — SPA builds.
3. `cd backend && uv run pytest -q` — unchanged (360 passing); proves the frontend work didn't touch the API contract.
4. `docker compose build` — the image builds with the new `npm test` gate and the `.dockerignore` in place.
5. Manual pass in `npm run dev` (backend running separately): drive a smart-add to completion and confirm the footer is present afterwards; close a dialog mid-stream and confirm no errors appear in the console afterwards; raise two toasts quickly and confirm both appear and each disappears on its own timer; reload `/library` with filters set; tab through with visible focus; toggle OS reduced motion; visit `/nope`.
