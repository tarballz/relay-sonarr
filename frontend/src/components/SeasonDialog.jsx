import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { useQueryClient } from "@tanstack/react-query";
import { api, streamSpillSeason, tierClass } from "../api.js";
import { Spinner } from "./Shared.jsx";
import { useDialog } from "./useDialog.js";
import ProgressStream from "./ProgressStream.jsx";

// Per-season status + "lower the resolution for this season" action.
//
// Shows each season's roll-up from the placement plan: its status, where its
// episodes were obtained from, and per-state counts. For a season that can't be
// found at the desired resolution, "Get at lower res" spills just that season to
// the lower tier (the rest of the show stays put), streaming progress live.
//
// `series` needs {title, tvdbId, instanceId?}.
export default function SeasonDialog({ series, onClose, onToast }) {
  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [steps, setSteps] = useState([]);
  const [streaming, setStreaming] = useState(false);
  const [activeSeason, setActiveSeason] = useState(null);
  const qc = useQueryClient();
  const dialogRef = useDialog(onClose);

  async function load(refresh) {
    try {
      const p = refresh
        ? await api.planRefresh(series.tvdbId, "chain")
        : await api.plan(series.tvdbId);
      setPlan(p);
    } catch (e) {
      onToast(e.message, true);
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
    setSteps([]);
    setStreaming(true);
    streamSpillSeason(
      {
        tvdbId: series.tvdbId,
        season: season.season,
        originInstanceId: plan.desiredTier,
        fallbackInstanceId: target,
        title: series.title,
      },
      {
        onStep: (e) => setSteps((s) => [...s, e]),
        onResult: async (res) => {
          setStreaming(false);
          setActiveSeason(null);
          if (res.status === "spilled") {
            onToast(
              `Season ${res.season}: ${res.episodeCount} episode${res.episodeCount === 1 ? "" : "s"} ` +
                `now downloading at lower resolution.`
            );
            qc.invalidateQueries({ queryKey: ["series"] });
            await load(false); // reflect the new split
          }
        },
        onError: (m) => {
          setStreaming(false);
          setActiveSeason(null);
          onToast(m, true);
        },
      }
    );
  }

  const seasons = plan?.seasons || [];
  const inFlight = streaming || steps.length > 0;

  return (
    <div className="scrim" onClick={onClose}>
      <motion.div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Seasons for ${series.title}`}
        tabIndex={-1}
        className="dialog"
        onClick={(e) => e.stopPropagation()}
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.2, ease: [0.2, 0.7, 0.2, 1] }}
      >
        <div className="dialog-head">
          <h2>{series.title}</h2>
          <div className="meta" style={{ marginTop: 6 }}>
            <span>Seasons</span>
            <span className="mono">tvdb {series.tvdbId}</span>
          </div>
        </div>

        <div className="dialog-body">
          {inFlight && <ProgressStream steps={steps} />}

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
                          disabled={streaming}
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
        </div>

        {!inFlight && !loading && (
          <div className="dialog-foot">
            <button className="btn ghost" onClick={onClose}>
              Close
            </button>
            <button className="btn" disabled={checking} onClick={checkLowerTiers}>
              {checking ? <Spinner /> : "Check lower tiers"}
            </button>
          </div>
        )}
      </motion.div>
    </div>
  );
}
