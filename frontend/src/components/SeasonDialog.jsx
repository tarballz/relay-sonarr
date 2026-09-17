import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, streamSpillSeason, tierClass } from "../api.js";
import { Spinner } from "./Shared.jsx";
import { useToast } from "./ui/Toast.jsx";
import Dialog from "./ui/Dialog.jsx";
import { useStreamFlow } from "./ui/useStreamFlow.js";
import ProgressStream from "./ProgressStream.jsx";

// Per-season status + "lower the resolution for this season" action.
//
// Shows each season's roll-up from the placement plan: its status, where its
// episodes were obtained from, and per-state counts. For a season that can't be
// found at the desired resolution, "Get at lower res" spills just that season to
// the lower tier (the rest of the show stays put), streaming progress live.
//
// `series` needs {title, tvdbId, instanceId?}.
export default function SeasonDialog({ series, onClose }) {
  const toast = useToast();
  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [activeSeason, setActiveSeason] = useState(null);
  const qc = useQueryClient();
  const { flow, run, busy, canShowFooter } = useStreamFlow();

  async function load(refresh) {
    try {
      const p = refresh
        ? await api.planRefresh(series.tvdbId, "chain")
        : await api.plan(series.tvdbId);
      setPlan(p);
    } catch (e) {
      toast(e.message, true);
    }
  }

  // Cheap load on open (cached availability — no fresh indexer hits).
  useEffect(() => {
    (async () => {
      await load(false);
      setLoading(false);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function checkLowerTiers() {
    setChecking(true);
    await load(true);
    setChecking(false);
  }

  // The lower tier a season should spill to: its known obtainable tier, else the
  // next tier after the desired one in the priority order.
  function spillTarget(season) {
    if (season.obtainableTier) return season.obtainableTier;
    const prio = plan?.tierPriority || [];
    const i = prio.indexOf(plan?.desiredTier);
    return i >= 0 && i + 1 < prio.length ? prio[i + 1] : null;
  }

  function spill(season) {
    const target = spillTarget(season);
    if (!target) return;
    setActiveSeason(season.season);
    run(
      streamSpillSeason,
      {
        tvdbId: series.tvdbId,
        season: season.season,
        originInstanceId: plan.desiredTier,
        fallbackInstanceId: target,
        title: series.title,
      },
      {
        onResult: async (res) => {
          setActiveSeason(null);
          if (res.status === "spilled") {
            toast(
              `Season ${res.season}: ${res.episodeCount} episode${res.episodeCount === 1 ? "" : "s"} ` +
                `now downloading at lower resolution.`
            );
            qc.invalidateQueries({ queryKey: ["series"] });
            await load(false); // reflect the new split
          }
        },
        onError: (m) => {
          setActiveSeason(null);
          toast(m, true);
        },
      }
    );
  }

  const seasons = plan?.seasons || [];
  // Once a stream has ever run, stay on the progress view rather than
  // reverting to the season list.
  const inFlight = flow.phase !== "idle";

  return (
    <Dialog
      title={series.title}
      label={`Seasons for ${series.title}`}
      onClose={onClose}
      busy={busy}
      subtitle={
        <>
          <span>Seasons</span>
          <span className="mono">tvdb {series.tvdbId}</span>
        </>
      }
      footer={
        canShowFooter && !loading ? (
          <>
            <button className="btn ghost" onClick={onClose}>
              Close
            </button>
            <button className="btn" disabled={checking} onClick={checkLowerTiers}>
              {checking ? <Spinner /> : "Check lower tiers"}
            </button>
          </>
        ) : null
      }
    >
      {inFlight && <ProgressStream steps={flow.steps} />}

      {!inFlight && loading && (
        <div style={{ padding: "8px 0", color: "var(--ink-dim)" }}>
          <Spinner /> <span style={{ marginLeft: 8 }}>Loading season status…</span>
        </div>
      )}

      {!inFlight && !loading && (
        <>
          {seasons.length === 0 && (
            <div style={{ color: "var(--ink-dim)", padding: "8px 0" }}>
              No placement data yet — check lower tiers to probe availability.
            </div>
          )}
          <div className="season-list">
            {seasons.map((s) => {
              const target = spillTarget(s);
              const counts = s.counts || {};
              return (
                <div key={s.season} className="season-row">
                  <div className="season-row-main">
                    <span className="season-num">
                      {s.season === 0 ? "Specials" : `Season ${s.season}`}
                    </span>
                    <span className={`status-badge st-${s.status}`}>
                      {String(s.status).replace("-", " ")}
                    </span>
                    {(s.obtainedTiers || []).map((t) => (
                      <span key={t} className={`tier ${tierClass(t)}`}>
                        {tierClass(t) === "t4k" ? "4K" : tierClass(t) === "t1080p" ? "1080P" : t}
                      </span>
                    ))}
                  </div>
                  <div className="season-row-meta">
                    <span className="mono">{s.episodeCount} ep</span>
                    {Object.entries(counts).map(([state, n]) => (
                      <span key={state} className="count-pill">
                        {n} {state}
                      </span>
                    ))}
                  </div>
                  {s.canLowerRes && target && (
                    <button
                      className={`btn ${tierClass(target)}`}
                      disabled={busy}
                      onClick={() => spill(s)}
                    >
                      {activeSeason === s.season ? (
                        <Spinner />
                      ) : (
                        `Get at lower res (${tierClass(target) === "t1080p" ? "1080p" : tierClass(target) === "t4k" ? "4K" : target})`
                      )}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
    </Dialog>
  );
}
