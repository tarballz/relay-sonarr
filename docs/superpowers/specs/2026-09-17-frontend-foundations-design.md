# Frontend foundations (Phase 5A)

**Date:** 2026-09-17
**Parent plan:** `~/.claude/plans/i-want-more-features-atomic-sparkle.md` (approved) — Phase 5A
**Status:** Approved design, pending implementation
**Constraint:** no user-visible feature changes beyond the bug fixes listed below; no backend changes.

## Problem

The SPA has no shared foundation, so every page re-implements the same things slightly differently, and
three real defects fall out of that:

1. **Dialogs can strand the user with no controls.** `AddDialog.jsx:231`, `ResolveDialog.jsx:128` and
   `SeasonDialog.jsx:100` each compute `const inFlight = streaming || steps.length > 0`, and `steps` is
   never cleared when a stream ends. `AddDialog.jsx:342` and `SeasonDialog.jsx:188` gate the whole footer
   on `!inFlight`, so after a stream finishes (or errors with no roadblock) the footer is gone.
   `ResolveDialog` has **no footer and no close button at all** (`:131-158`) — Escape or a scrim click are
   the only exits, and on the mobile bottom-sheet layout that is close to a dead end.
2. **Streams leak.** `openStream` returns a cancel function (`api.js:101`) that is discarded at all four
   call sites (`AddDialog.jsx:146,194`, `ResolveDialog.jsx:66`, `SeasonDialog.jsx:68`), and no effect
   returns a cleanup. Closing a dialog mid-stream leaves the `EventSource` open and its handlers set
   state on unmounted components. `JSON.parse` in the `step`/`result` handlers is unguarded.
3. **Toasts fight each other.** Four identical implementations (`SearchAdd.jsx:37-40`,
   `Library.jsx:46-49`, `Operations.jsx:94-97`, `Settings.jsx:12-15`) plus a fifth ad-hoc path
   (`Library.jsx:51-64`). There is no `clearTimeout` anywhere in `src/`, so an older timer dismisses a
   newer toast, and none of them announce to assistive tech.

Underneath that: `req()` (`api.js:2-17`) throws bare `Error(detail)` with no status, calls `res.json()`
unconditionally (a 204 would throw) and ignores `AbortSignal`; `tierClass()` sniffs substrings of the
instance id and the id→label map is re-implemented three times with inconsistent casing
(`Shared.jsx:3`, `Library.jsx:141`, `SeasonDialog.jsx:155,176`); `styles.css` is 1790 lines with 17 tokens,
46 hard-coded colour literals (82 occurrences), 4 same-scope duplicate selectors, dead `.fallback` rules,
**no `:focus-visible` rule anywhere** and no `prefers-reduced-motion`; 66 inline `style={{…}}`; the Add
dialog's tier toggle (`AddDialog.jsx:317`) is a `div` with `onClick` — a functional checkbox that no
keyboard can reach; `Spinner` (`Shared.jsx:10-12`) has `aria-label` on a bare `span` (announced as
nothing); there is no 404 route, no skip link, and no frontend test harness at all.

## Goal

A small, tested foundation the later UI phases build on — one HTTP layer, one dialog, one toast host, one
tier registry, one set of design tokens — and the three defects above fixed. No new pages, no new
features: 5B/5C/5D add those.

Non-goals: the Overview/Timeline pages, the unified Library, the Series detail page, the Settings tab
rewrite, live SSE query invalidation (all later phases); TypeScript; ESLint (the repo has none — the
stale `eslint-disable` comments stay); a design refresh.

## Design

### 1. Test harness (`vitest`)

- Add `vitest@^2` as the only new devDependency, plus scripts `"test": "vitest run"` and
  `"test:watch": "vitest"`. Pure logic lives in `src/lib/*.js` with a sibling `*.test.js`; no jsdom,
  no testing-library — every module below is a pure function or reducer, and hooks stay thin wrappers
  over them. `vite.config.js` gains `test: { environment: "node", include: ["src/**/*.test.js"] }`.
- Repo-root **`.dockerignore`** (new): `frontend/node_modules`, `frontend/dist`, `backend/.venv`,
  `**/__pycache__`, `.superpowers`, `data`. Without it `COPY frontend/ ./` copies a host-built
  `node_modules` into the image.
- `Dockerfile` frontend stage runs `npm test` between install and build, so a red suite fails the image.

### 2. HTTP layer (`src/lib/http.js`)

```js
class ApiError extends Error {}   // .status, .detail, .fields (FastAPI 422 → {field: message})
async function request(path, { method, body, signal, headers } = {})
```

- One `fetch` per call against `/api`; JSON body serialized by the helper (so callers stop hand-writing
  `JSON.stringify` + headers); `Content-Type` only when there is a body.
- `204`/empty body → `null` instead of throwing.
- Error path: parse a JSON body when present; `detail` may be a string (FastAPI `HTTPException`) or a
  list of validation errors (422) → `fields`; always set `status`. `ApiError.message` keeps today's
  text (the string detail, else `statusText`) so existing `ErrorState` rendering is unchanged.
- `signal` is passed through; `api.js` methods accept an optional `{ signal }` so TanStack Query can
  hand over its signal later (5B), but no call site is required to.
- `api.js` keeps **every existing export name and call signature**; only the internals change. Dead
  methods stay (`smartAdd`, `advanceFallback`, `availability`, `reconcileTick`) — 5B/5D use them.

### 3. Streams (`src/api.js`)

- `openStream` keeps its contract (build repeated query params, listen for `step`/`result`/`error`,
  close on the first terminal event, return `cancel`), and gains: `try/catch` around every
  `JSON.parse` (a malformed frame becomes an `onError("Malformed stream frame")` instead of an
  exception inside an event handler), and `cancel()` being idempotent (already true via `done`).
- Every call site stores the handle and cancels it on unmount/close: the three dialogs keep a
  `useRef` for the live cancel and a `useEffect(() => () => cancelRef.current?.(), [])`; starting a new
  stream cancels the previous one first. This is what the flow reducer (§4) drives.

### 4. Dialog flow (`src/lib/flow.js` + `src/components/ui/Dialog.jsx`)

- **`flow.js`** is a pure reducer: state `{ phase: "idle" | "streaming" | "done" | "error", steps: [],
  result: null, error: null }`; actions `start`, `step`, `result`, `error`, `reset`. `phase` — never
  `steps.length` — decides what a dialog renders, and `done`/`error` always render controls. Selectors:
  `isBusy(state)` (`phase === "streaming"`), `showFooter(state)` (`phase !== "streaming"`).
- **`Dialog.jsx`** owns the skeleton every dialog copies today (scrim with `aria-hidden` on the backdrop
  behaviour, `motion.div role="dialog" aria-modal="true"`, head/body/foot), plus a header close button
  (`aria-label="Close"`) that is **always present**. Props: `title`, `subtitle`, `onClose`, `busy`
  (disables the close button while a stream runs), `footer`, `children`, `maxWidth`. It calls `useDialog`
  internally. Animation comes from one shared transition constant, so the 0.2/0.22 drift disappears.
- `useDialog.js` gains **focus restoration**: capture `document.activeElement` on open and refocus it on
  close (guarded for a removed node), fixing focus dropping to `<body>` on the Library poster grid.
- All five dialogs (`AddDialog`, `ResolveDialog`, `SeasonDialog`, `PolicyEditor`, `ConfirmDialog`) are
  converted to `<Dialog>`; the three streaming ones adopt `flow.js`. `ResolveDialog` therefore gains a
  close button and a footer. Their behaviour is otherwise unchanged.

### 5. Toasts (`src/lib/toast.js` + `src/components/ui/Toast.jsx`)

- `toast.js` is a pure reducer over a queue: `{ items: [{ id, msg, err, ttl }] }` with `push`/`dismiss`.
- `Toast.jsx` provides `ToastProvider` (mounted once in `App.jsx`) and `useToast()` → `toast(msg, err?)`.
  One `setTimeout` per item, **cleared on dismiss and on unmount**; the host is a
  `role="status" aria-live="polite"` region so toasts are announced; up to three stack.
- The four page implementations and `Library`'s ad-hoc path are deleted. Dialogs call `useToast()`
  directly and the `onToast` prop is removed from all five.

### 6. Tier registry (`src/lib/tiers.js`)

- `buildTiers(instances)` → `{ [id]: { id, name, label, cls } }` where `cls` is `t1080p` / `t4k` /
  `tdefault` (unchanged CSS classes) and `label` is the short badge text. Assignment is driven by the
  instance list from `GET /api/settings`, with today's substring sniff as the fallback for an id the
  registry doesn't know. `TierProvider` (fed by the existing `settings` query) + `useTiers()` expose it;
  `tierClass(id)` and a new `tierLabel(id)` keep working as plain functions for non-component code.
- The three inlined label maps and the `"1080p"`/`"1080P"` casing split collapse into this. Adding a
  third instance stops needing code edits — the registry assigns the next `--tier-n` class.

### 7. Formatting and URL state

- **`src/lib/format.js`**: `bytes` (moved from `Shared.jsx`), `ago(seconds)`, `when(iso)` — includes the
  date when it isn't today, which fixes Operations showing time-only — and `duration(ms)`.
- **`src/lib/urlState.js`**: pure `serialize(state, defaults)` / `parse(searchParams, defaults)` plus a
  thin `useUrlState(defaults)` over `useSearchParams`. Wired into **Library** now (search text, sort,
  tier filter, grid/table view) so filters survive reload and are linkable; other pages adopt it later.

### 8. Design tokens and CSS hygiene (`src/styles.css`)

- Add to `:root`: spacing `--s-1…--s-8` (4/8/12/16/24/32/48/64), type scale `--fs-xs…--fs-xl`, status
  tokens `--st-ok/warn/err/info/muted`, episode-state tokens `--ep-*` (for 5C), `--focus-ring`,
  `--z-scrim/--z-dialog/--z-toast`, and alpha helpers for the three accents so
  `rgba(45,212,191,…)`-style literals stop being retyped.
- Replace the hard-coded literals that duplicate existing tokens (`#0c0d11` = `--bg`,
  `rgba(74,222,128,…)` vs `--ok`, `rgba(239,68,68,.4)` vs `--danger`) and collapse the three
  "dark overlay" colours into one token. The remaining intentional one-offs (poster chips, gradient
  stops) get a comment, not a token.
- Fix the four same-scope duplicate selectors (`.page-title` 170/723, `.tier` 222/741,
  `.dialog-head` 458/878, `.op-card` 734/1007) by merging each into one rule, and delete the dead
  `.fallback` rules.
- Add a single `:focus-visible` rule using `--focus-ring` covering `.btn`, nav links, `.seg`,
  `.op-summary`, the poster overlay buttons and the view toggle; add `:focus-within` so poster
  overlay buttons appear for keyboard users.
- Add `@media (prefers-reduced-motion: reduce)` neutralising transitions, the `spin` keyframe and the
  grain/mesh overlays, and wrap the app in framer-motion's `<MotionConfig reducedMotion="user">`.
- **`Settings.jsx` is excluded from the inline-style sweep** (30 of 66 inline styles): it was changed
  two days ago and its `{...data, ...form}` spread plus client-side fallbacks are load-bearing;
  Phase 5D rewrites that page. It still loses its local toast and gains `aria-describedby` on the help
  text. The other ~36 inline styles move into classes (the repeated spinner padding, dialog `.meta`
  spacing, panel overflow, muted help blocks).

### 9. Accessibility fixes

- `AddDialog.jsx:317` tier toggle becomes a real `<label><input type="checkbox">` (matching the season
  chips at `:299`), so it is reachable and operable by keyboard; the nested selects gain
  `aria-label`s (`"Quality profile"`, `"Root folder"`), as do `PolicyEditor`'s four controls and
  `SearchAdd`'s search input.
- `Spinner` becomes `role="status"` with an `aria-label`, and `ProgressStream` gets
  `aria-live="polite"` so streaming steps are announced.
- Nav glyph icons get `aria-hidden="true"`; a skip link jumps to `<main>`; the mobile drawer gets
  Escape-to-close and focus containment via `useDialog`.
- A catch-all `<Route path="*">` renders a NotFound panel with a link home.

## Error handling

- `ApiError` carries `status`/`fields`; existing `ErrorState` keeps rendering `error.message`.
- A malformed SSE frame surfaces as a normal stream error, never an uncaught exception.
- Toast timers and stream cancels are always cleared on unmount.
- A dialog whose stream errors shows the error plus its footer, so the user can retry or close.

## Testing

`vitest run` (node environment), pure-logic only:
`format.test.js` (bytes/ago/when-with-date/duration incl. boundaries), `toast.test.js` (push/dismiss,
id uniqueness, cap of three), `flow.test.js` (every transition; `showFooter` true in idle/done/error and
false while streaming — the regression test for the stranded-dialog bug), `urlState.test.js`
(round-trip, defaults omitted from the query string, unknown keys preserved), `tiers.test.js`
(registry from instances, fallback sniff, label casing), `http.test.js` (stubbed `fetch`: 200 JSON, 204
→ null, string detail, 422 → `fields`, non-JSON error body, abort propagation), `stream.test.js`
(stubbed `EventSource`: step/result/error dispatch, close on terminal event, idempotent cancel,
malformed frame → onError).

Manual verification (documented, run once): open the Add dialog and drive a smart-add to completion —
the footer is present at the end; close a dialog mid-stream and confirm no further state updates; tab
through Library and the Add dialog with visible focus; toggle OS reduced-motion and confirm entrances
are neutralised; reload Library with filters applied and confirm they persist.

## Rollout

Frontend-only; the backend and its 360 tests are untouched. The built SPA ships in the image, so the
change reaches the user with the next `docker compose up -d --build`. Nothing about the API contract
changes, so an older container serves the new SPA and vice versa.
