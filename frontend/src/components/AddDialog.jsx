import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { useQueryClient } from "@tanstack/react-query";
import { api, streamSmartAdd, streamAdvance, tierClass } from "../api.js";
import { TierBadge, Spinner } from "./Shared.jsx";
import ProgressStream from "./ProgressStream.jsx";

// Pull a show's fanart (backdrop) URL from its images, if present.
function fanartUrl(series) {
  const img = (series.images || []).find((i) => i.coverType === "fanart");
  return img?.remoteUrl || img?.url || null;
}

// Turn an availability result into a one-line "why nothing qualified" summary.
function reasonText(av) {
  if (!av) return "";
  const total = av.totalReleases || 0;
  if (total === 0) return "No releases found at all.";
  const parts = (av.rejectionSummary || []).map((r) => `${r.count} ${r.reason}`);
  return `${total} found, none qualified${parts.length ? " — " + parts.join(", ") : ""}.`;
}

// Per-instance add dialog. One toggle per configured tier; each carries its own
// quality-profile + root-folder pickers. Adding a single tier that has a
// configured fallback routes through /smart-add so we can surface the
// "no release found — try the other tier?" banner.
export default function AddDialog({ series, instances, fallbackChains, onClose, onToast }) {
  const [opts, setOpts] = useState({}); // id -> {profiles, rootFolders, profileId, rootFolderPath}
  const [sel, setSel] = useState({}); // id -> bool
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState(null); // current fallback_suggested response
  const [streaming, setStreaming] = useState(false);
  const [steps, setSteps] = useState([]); // live progress events for the current op
  const [terminal, setTerminal] = useState(null); // 'exhausted' result awaiting keep/remove
  const queryClient = useQueryClient();

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
            return [i.id, {
              profiles,
              rootFolders,
              profileId: profiles[0]?.id,
              rootFolderPath: rootFolders[0]?.path,
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

  // Terminal handler shared by smart-add and advance streams.
  function handleTerminal(res, fallbackToastName) {
    if (res.status === "fallback_suggested") {
      setStep(res); // show the next-step banner beneath the completed step list
      return;
    }
    if (res.status === "exhausted") {
      // Keep the dialog open so the user can remove the empty series or keep it.
      setTerminal(res);
      return;
    }
    if (res.status === "added" || res.status === "placed") {
      const where = whereName(res.instanceId);
      const prof = res.qualityProfileName ? ` (${res.qualityProfileName})` : "";
      onToast(`Downloading “${series.title}” on ${where}${prof} — release found ✓`);
    } else {
      onToast(`Added “${series.title}” to ${fallbackToastName}`);
    }
    onClose();
  }

  async function removeExhausted() {
    setBusy(true);
    try {
      await api.removeSeries(terminal.instanceId, terminal.seriesId);
      await queryClient.invalidateQueries({ queryKey: ["series"] });
      onToast(`Removed “${series.title}” — no release was available on any tier.`);
      onClose();
    } catch (e) {
      onToast(e.message, true);
      setBusy(false);
    }
  }

  function keepExhausted() {
    const where = whereName(terminal.instanceId);
    onToast(`Kept “${series.title}” monitored on ${where} — it'll grab when a release appears.`);
    onClose();
  }

  async function handleAdd() {
    // Single tier with a fallback chain configured → live smart path.
    if (chosen.length === 1 && fallbackChains?.[chosen[0].id]?.length) {
      const t = chosen[0];
      const o = opts[t.id];
      setSteps([]);
      setStreaming(true);
      streamSmartAdd(
        {
          tvdbId: series.tvdbId,
          targetInstanceId: t.id,
          targetQualityProfileId: o.profileId,
          targetRootFolderPath: o.rootFolderPath,
          title: series.title,
        },
        {
          onStep: (e) => setSteps((s) => [...s, e]),
          onResult: (res) => {
            setStreaming(false);
            handleTerminal(res, t.name);
          },
          onError: (msg) => {
            setStreaming(false);
            onToast(msg, true);
          },
        }
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
      }));
      const res = await api.add({ tvdbId: series.tvdbId, targets });
      const ok = res.results.filter((r) => r.ok).map((r) => r.instanceId);
      const bad = res.results.filter((r) => !r.ok);
      if (bad.length) onToast(`Added to ${ok.join(", ") || "none"}; failed: ${bad.map((b) => b.instanceId).join(", ")}`, true);
      else onToast(`Added “${series.title}” to ${ok.join(" + ")}`);
      onClose();
    } catch (e) {
      onToast(e.message, true);
    } finally {
      setBusy(false);
    }
  }

  // Walk one chain step live. Result is terminal (placed/exhausted) or another
  // suggestion, which re-renders the banner beneath the fresh step list.
  function advance() {
    const adv = step.advance;
    setStep(null);
    setSteps([]);
    setStreaming(true);
    streamAdvance(
      { ...adv, title: series.title },
      {
        onStep: (e) => setSteps((s) => [...s, e]),
        onResult: (res) => {
          setStreaming(false);
          handleTerminal(res);
        },
        onError: (msg) => {
          setStreaming(false);
          onToast(msg, true);
        },
      }
    );
  }

  const loading = Object.keys(opts).length === 0;
  const inFlight = streaming || steps.length > 0; // showing the live play-by-play
  const backdrop = fanartUrl(series);

  return (
    <div className="scrim" onClick={onClose}>
      <motion.div
        className="dialog"
        onClick={(e) => e.stopPropagation()}
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.22, ease: [0.2, 0.7, 0.2, 1] }}
      >
        <div className="dialog-head">
          {backdrop && (
            <div className="dialog-backdrop" style={{ backgroundImage: `url(${backdrop})` }} />
          )}
          <h2>{series.title}</h2>
          <div className="meta" style={{ marginTop: 6 }}>
            {series.year ? <span>{series.year}</span> : null}
            {series.network ? <span>· {series.network}</span> : null}
            <span className="mono">tvdb {series.tvdbId}</span>
          </div>
        </div>

        <div className="dialog-body">
          {inFlight ? (
            <>
              <ProgressStream steps={steps} />
              {step && (
                <FallbackBanner data={step} series={series} onAccept={advance} onKeep={onClose} busy={false} />
              )}
              {terminal && (
                <ExhaustedPanel
                  data={terminal}
                  series={series}
                  where={whereName(terminal.instanceId)}
                  onRemove={removeExhausted}
                  onKeep={keepExhausted}
                  busy={busy}
                />
              )}
            </>
          ) : loading ? (
            <div style={{ padding: "8px 0" }}>
              <Spinner /> <span style={{ color: "var(--ink-dim)", marginLeft: 8 }}>Loading profiles…</span>
            </div>
          ) : (
            <>
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
        </div>

        {!inFlight && !loading && (
          <div className="dialog-foot">
            <button className="btn ghost" onClick={onClose}>Cancel</button>
            <button className="btn primary" disabled={busy || chosen.length === 0} onClick={handleAdd}>
              {busy ? <Spinner /> : `Add${chosen.length ? ` to ${chosen.length} tier${chosen.length > 1 ? "s" : ""}` : ""}`}
            </button>
          </div>
        )}
      </motion.div>
    </div>
  );
}

function ExhaustedPanel({ data, series, where, onRemove, onKeep, busy }) {
  return (
    <motion.div
      className="fallback exhausted-panel"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
    >
      <div className="glyph">∅</div>
      <div className="copy">
        <strong>No release found on any tier for “{series.title}.”</strong>
        <p>
          It's sitting empty on {where}. Remove it, or keep it monitored so Sonarr grabs it
          automatically if a release shows up later.
        </p>
      </div>
      <div className="actions">
        <button className="btn ghost" onClick={onKeep} disabled={busy}>Keep monitored</button>
        <button className="btn danger" onClick={onRemove} disabled={busy}>
          {busy ? <Spinner /> : `Remove from ${where}`}
        </button>
      </div>
    </motion.div>
  );
}

function FallbackBanner({ data, series, onAccept, onKeep, busy }) {
  const next = data.next;
  // Same instance → it's a quality-profile retry; different instance → a move.
  const action = next.sameInstance
    ? `Try ${next.qualityProfileName}`
    : `Move to ${next.instanceName}`;
  const target = next.sameInstance
    ? `${next.instanceName} with the ${next.qualityProfileName} profile`
    : `${next.instanceName} (${next.qualityProfileName})`;
  return (
    <motion.div className="fallback" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}>
      <div className="glyph">⚠</div>
      <div className="copy">
        <strong>No qualifying release for “{series.title}” yet.</strong>
        {data.availability && (
          <p style={{ color: "var(--ink-faint)", fontFamily: "var(--font-mono)", fontSize: 12 }}>
            {reasonText(data.availability)}
          </p>
        )}
        <p>
          Next: search <strong>{target}</strong>.{" "}
          {next.sameInstance
            ? "The series stays put; only its quality profile changes."
            : "If found there, the empty entry on the current tier is removed."}
        </p>
      </div>
      <div className="actions">
        <button className="btn ghost" onClick={onKeep} disabled={busy}>Stop here</button>
        <button className="btn t1080p" onClick={onAccept} disabled={busy}>
          {busy ? <Spinner /> : action}
        </button>
      </div>
    </motion.div>
  );
}
