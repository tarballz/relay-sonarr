import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  api,
  streamSmartAdd,
  streamAdvance,
  streamReattempt,
  streamFillGaps,
  tierClass,
} from "../api.js";
import { TierBadge, Spinner } from "./Shared.jsx";
import { useToast } from "./ui/Toast.jsx";
import Dialog from "./ui/Dialog.jsx";
import { useStreamFlow } from "./ui/useStreamFlow.js";
import ProgressStream from "./ProgressStream.jsx";
import ResolutionOptions from "./ResolutionOptions.jsx";
import ConfirmDialog from "./ConfirmDialog.jsx";

// Per-instance add dialog. One toggle per configured tier; each carries its own
// quality-profile + root-folder pickers. Adding a single tier that has a
// configured fallback routes through /smart-add so we can surface the roadblock
// resolution menu ("no release found — here's how to resolve it").
export default function AddDialog({ series, instances, fallbackChains, onClose }) {
  const toast = useToast();
  const [opts, setOpts] = useState({}); // id -> {profiles, rootFolders, profileId, rootFolderPath}
  const [sel, setSel] = useState({}); // id -> bool
  const [busy, setBusy] = useState(false);
  const [roadblock, setRoadblock] = useState(null); // fallback_suggested | exhausted result
  const [confirmRemove, setConfirmRemove] = useState(null); // {instanceId, seriesId} awaiting confirm
  const [monitorAll, setMonitorAll] = useState(true); // monitor every season vs a chosen subset
  const [seasonSel, setSeasonSel] = useState({}); // seasonNumber -> bool (only used when !monitorAll)
  const queryClient = useQueryClient();
  // Renamed from the stream flow's `busy` to avoid colliding with the plain
  // multi-add / remove-confirm `busy` state above.
  const { flow, run, busy: streaming, canShowFooter } = useStreamFlow();

  // The show's seasons from the lookup, sorted; specials (0) last and off by default.
  const seasonList = (series.seasons || [])
    .map((s) => s.seasonNumber)
    .filter((n) => n !== undefined && n !== null)
    .sort((a, b) => a - b);

  // Default the per-season selection: all numbered seasons on, specials off.
  useEffect(() => {
    setSeasonSel(Object.fromEntries(seasonList.map((n) => [n, n !== 0])));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [series.tvdbId]);

  const toggleSeason = (n) => setSeasonSel((s) => ({ ...s, [n]: !s[n] }));
  const selectedSeasons = seasonList.filter((n) => seasonSel[n]);
  // null ⇒ monitor all (backend default); an array ⇒ monitor only those seasons.
  const monitoredSeasons = monitorAll ? null : selectedSeasons;
  const seasonChoiceInvalid = !monitorAll && selectedSeasons.length === 0;

  useEffect(() => {
    let alive = true;
    (async () => {
      const entries = await Promise.all(
        instances.map(async (i) => {
          try {
            const [profiles, rootFolders] = await Promise.all([
              api.profiles(i.id),
              api.rootFolders(i.id),
            ]);
            // Prefer the instance's configured default; rootFolders[0] is the
            // oldest one, which is the wrong pool once storage spans two.
            const preferred =
              rootFolders.find((r) => r.path === i.defaultRootFolder)?.path ??
              rootFolders[0]?.path;
            return [i.id, {
              profiles,
              rootFolders,
              profileId: profiles[0]?.id,
              rootFolderPath: preferred,
            }];
          } catch {
            return [i.id, { profiles: [], rootFolders: [], error: true }];
          }
        })
      );
      if (alive) setOpts(Object.fromEntries(entries));
    })();
    return () => {
      alive = false;
    };
  }, [instances]);

  const toggle = (id) => setSel((s) => ({ ...s, [id]: !s[id] }));
  const setOpt = (id, k, v) => setOpts((o) => ({ ...o, [id]: { ...o[id], [k]: v } }));
  const chosen = instances.filter((i) => sel[i.id]);

  function whereName(id) {
    return instances.find((i) => i.id === id)?.name || id;
  }

  // Terminal handler shared by every stream (smart-add / advance / reattempt / fill-gaps).
  function handleTerminal(res, fallbackToastName) {
    if (res.status === "fallback_suggested" || res.status === "exhausted") {
      setRoadblock(res); // show the resolution menu beneath the completed step list
      return;
    }
    if (res.status === "split") {
      const to = whereName(res.to?.instanceId);
      toast(
        `Split “${series.title}” — ${res.gapCount} missing episode${res.gapCount === 1 ? "" : "s"} ` +
          `now downloading on ${to}; existing episodes kept.`
      );
      queryClient.invalidateQueries({ queryKey: ["series"] });
      onClose();
      return;
    }
    if (res.status === "added" || res.status === "placed") {
      const where = whereName(res.instanceId);
      const prof = res.qualityProfileName ? ` (${res.qualityProfileName})` : "";
      toast(`Downloading “${series.title}” on ${where}${prof} — release found ✓`);
    } else {
      toast(`Added “${series.title}” to ${fallbackToastName}`);
    }
    onClose();
  }

  // Start one of the resolution streams, switching the dialog to the live view.
  function runStream(streamFn, params) {
    setRoadblock(null);
    run(streamFn, { ...params, title: series.title }, {
      onResult: (res) => handleTerminal(res),
    });
  }

  // Route a chosen resolution option to the right endpoint.
  function dispatchOption(o) {
    switch (o.action) {
      case "reattempt":
        runStream(streamReattempt, o.payload);
        break;
      case "walk_chain":
        runStream(streamAdvance, o.payload);
        break;
      case "fill_gaps":
        runStream(streamFillGaps, o.payload);
        break;
      case "leave":
        toast(`Kept “${series.title}” monitored — it'll grab when a release appears.`);
        onClose();
        break;
      case "remove":
        setConfirmRemove(o.payload);
        break;
      default:
        break;
    }
  }

  async function confirmRemoveSeries() {
    setBusy(true);
    try {
      await api.removeSeries(confirmRemove.instanceId, confirmRemove.seriesId);
      await queryClient.invalidateQueries({ queryKey: ["series"] });
      toast(`Removed “${series.title}” — no release was available.`);
      onClose();
    } catch (e) {
      toast(e.message, true);
      setBusy(false);
      setConfirmRemove(null);
    }
  }

  async function handleAdd() {
    // Single tier with a fallback chain configured → live smart path.
    if (chosen.length === 1 && fallbackChains?.[chosen[0].id]?.length) {
      const t = chosen[0];
      const o = opts[t.id];
      run(
        streamSmartAdd,
        {
          tvdbId: series.tvdbId,
          targetInstanceId: t.id,
          targetQualityProfileId: o.profileId,
          targetRootFolderPath: o.rootFolderPath,
          monitoredSeasons,
          title: series.title,
        },
        { onResult: (res) => handleTerminal(res, t.name) }
      );
      return;
    }

    // One or more tiers, explicit → plain multi-add (no chain probing).
    setBusy(true);
    try {
      const targets = chosen.map((i) => ({
        instanceId: i.id,
        qualityProfileId: opts[i.id].profileId,
        rootFolderPath: opts[i.id].rootFolderPath,
        monitoredSeasons,
      }));
      const res = await api.add({ tvdbId: series.tvdbId, targets });
      const ok = res.results.filter((r) => r.ok).map((r) => r.instanceId);
      const bad = res.results.filter((r) => !r.ok);
      if (bad.length) toast(`Added to ${ok.join(", ") || "none"}; failed: ${bad.map((b) => b.instanceId).join(", ")}`, true);
      else toast(`Added “${series.title}” to ${ok.join(" + ")}`);
      onClose();
    } catch (e) {
      toast(e.message, true);
    } finally {
      setBusy(false);
    }
  }

  const loading = Object.keys(opts).length === 0;
  // Once a stream has ever run, stay on the progress/resolution view rather
  // than reverting to the tier picker — mirrors the old steps-never-clear
  // behaviour without keying off steps.length.
  const inFlight = flow.phase !== "idle";

  return (
    <>
      <Dialog
        title={series.title}
        label={`Add ${series.title}`}
        onClose={onClose}
        busy={streaming}
        subtitle={
          <>
            {series.year ? <span>{series.year}</span> : null}
            {series.network ? <span>· {series.network}</span> : null}
            <span className="mono">tvdb {series.tvdbId}</span>
          </>
        }
        footer={
          canShowFooter && !loading ? (
            <>
              <button className="btn ghost" onClick={onClose}>Cancel</button>
              <button
                className="btn primary"
                disabled={busy || chosen.length === 0 || seasonChoiceInvalid}
                onClick={handleAdd}
              >
                {busy ? <Spinner /> : `Add${chosen.length ? ` to ${chosen.length} tier${chosen.length > 1 ? "s" : ""}` : ""}`}
              </button>
            </>
          ) : null
        }
      >
        {inFlight ? (
          <>
            <ProgressStream steps={flow.steps} />
            {roadblock && (
              <ResolutionOptions
                data={roadblock}
                series={series}
                onDispatch={dispatchOption}
              />
            )}
          </>
        ) : loading ? (
          <div style={{ padding: "8px 0" }}>
            <Spinner /> <span style={{ color: "var(--ink-dim)", marginLeft: 8 }}>Loading profiles…</span>
          </div>
        ) : (
          <>
            {seasonList.length > 0 && (
              <>
                <div className="section-label">Monitor</div>
                <div className="monitor-choice">
                  <button
                    className={`seg ${monitorAll ? "on" : ""}`}
                    onClick={() => setMonitorAll(true)}
                  >
                    All episodes
                  </button>
                  <button
                    className={`seg ${!monitorAll ? "on" : ""}`}
                    onClick={() => setMonitorAll(false)}
                  >
                    Choose seasons
                  </button>
                </div>
                {!monitorAll && (
                  <div className="season-picker">
                    {seasonList.map((n) => (
                      <label key={n} className={`season-chip ${seasonSel[n] ? "on" : ""}`}>
                        <input
                          type="checkbox"
                          checked={!!seasonSel[n]}
                          onChange={() => toggleSeason(n)}
                        />
                        {n === 0 ? "Specials" : `S${n}`}
                      </label>
                    ))}
                  </div>
                )}
              </>
            )}
            <div className="section-label">Send to</div>
            {instances.map((i) => {
              const o = opts[i.id] || {};
              const on = !!sel[i.id];
              return (
                <div key={i.id} className={`tier-toggle ${tierClass(i.id)} ${on ? "on" : ""}`} onClick={() => toggle(i.id)}>
                  <span className="check">{on ? "✓" : ""}</span>
                  <TierBadge instanceId={i.id} name={i.name} />
                  {on && !o.error && (
                    <div className="opts" onClick={(e) => e.stopPropagation()}>
                      <select className="input" value={o.profileId} onChange={(e) => setOpt(i.id, "profileId", Number(e.target.value))}>
                        {o.profiles.map((p) => (
                          <option key={p.id} value={p.id}>{p.name}</option>
                        ))}
                      </select>
                      <select className="input" value={o.rootFolderPath} onChange={(e) => setOpt(i.id, "rootFolderPath", e.target.value)}>
                        {o.rootFolders.map((r) => (
                          <option key={r.path} value={r.path}>{r.path}</option>
                        ))}
                      </select>
                    </div>
                  )}
                  {on && o.error && <span style={{ marginLeft: "auto", color: "var(--danger)", fontSize: 12 }}>unreachable</span>}
                </div>
              );
            })}
          </>
        )}
      </Dialog>

      {confirmRemove && (
        <ConfirmDialog
          title={`Remove “${series.title}”?`}
          message={`This removes the empty series from ${whereName(confirmRemove.instanceId)}. Files already on disk are kept.`}
          confirmLabel="Remove"
          danger
          busy={busy}
          onConfirm={confirmRemoveSeries}
          onCancel={() => setConfirmRemove(null)}
        />
      )}
    </>
  );
}
