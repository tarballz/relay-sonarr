import { useMemo, useState } from "react";
import { motion } from "framer-motion";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, tierClass } from "../api.js";
import { TierBadge, Spinner, Empty, bytes } from "../components/Shared.jsx";
import ConfirmDialog from "../components/ConfirmDialog.jsx";

function posterUrl(s) {
  const img = (s.images || []).find((i) => i.coverType === "poster");
  return img?.remoteUrl || img?.url || null;
}

export default function Library() {
  const { data, isLoading } = useQuery({ queryKey: ["series"], queryFn: api.series });
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState("all");
  const [view, setView] = useState("grid"); // posters by default — the cooler look
  const [removing, setRemoving] = useState(null); // series pending deletion
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState(null);

  async function confirmRemove(deleteFiles) {
    setBusy(true);
    try {
      await api.removeSeries(removing.instanceId, removing.id, deleteFiles);
      await queryClient.invalidateQueries({ queryKey: ["series"] });
      setToast({ msg: `Removed “${removing.title}” from ${removing.instanceName}` });
      setRemoving(null);
    } catch (e) {
      setToast({ msg: e.message, err: true });
    } finally {
      setBusy(false);
      setTimeout(() => setToast(null), 4000);
    }
  }

  const tiers = useMemo(() => {
    const ids = new Set((data || []).map((s) => s.instanceId));
    return [...ids];
  }, [data]);

  const rows = (data || [])
    .filter((s) => filter === "all" || s.instanceId === filter)
    .sort((a, b) => (a.sortTitle || a.title || "").localeCompare(b.sortTitle || b.title || ""));

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Library</h1>
          <p className="page-sub">{(data || []).length} series across {tiers.length} tier{tiers.length === 1 ? "" : "s"}.</p>
        </div>
        <div className="health-row">
          <button className={`btn ${filter === "all" ? "primary" : "ghost"}`} onClick={() => setFilter("all")}>All</button>
          {tiers.map((id) => (
            <button key={id} className={`btn ${filter === id ? tierClass(id) : "ghost"}`} onClick={() => setFilter(id)}>
              {nameOf(data, id)}
            </button>
          ))}
          <span className="view-toggle">
            <button className={view === "grid" ? "on" : ""} onClick={() => setView("grid")} title="Poster grid">▦</button>
            <button className={view === "table" ? "on" : ""} onClick={() => setView("table")} title="Table">≣</button>
          </span>
        </div>
      </div>

      {isLoading && <div style={{ padding: 24 }}><Spinner /></div>}
      {data && data.length === 0 && <Empty big="Library is empty">Add a show from Search & Add.</Empty>}

      {rows.length > 0 && view === "grid" && (
        <div className="poster-grid">
          {rows.map((s, idx) => {
            const stats = s.statistics || {};
            const have = stats.episodeFileCount ?? 0;
            const total = stats.episodeCount ?? 0;
            const missing = total - have;
            const url = posterUrl(s);
            const tcls = tierClass(s.instanceId);
            const shortTier = tcls === "t4k" ? "4K" : tcls === "t1080p" ? "1080P" : s.instanceName;
            return (
              <motion.div
                key={`${s.instanceId}-${s.id}`}
                className="poster-card"
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: Math.min(idx * 0.02, 0.3) }}
              >
                <div className="poster-art">
                  {url ? <img src={url} alt="" loading="lazy" /> : <div className="poster-ph">{(s.title || "?")[0]}</div>}
                  <span className={`poster-tier ${tcls}`}>{shortTier}</span>
                  <button
                    className="poster-del"
                    title={`Remove ${s.title}`}
                    onClick={() => setRemoving(s)}
                  >
                    ✕
                  </button>
                  <div className="poster-gradient" />
                  <div className="poster-info">
                    <div className="poster-title">{s.title}</div>
                    <div className="poster-meta">
                      <span className="num">{have}/{total}</span>
                      {missing > 0 && <span style={{ color: "var(--t-4k)" }}>· {missing} missing</span>}
                      {!s.monitored && <span style={{ color: "var(--ink-faint)" }}>· unmonitored</span>}
                    </div>
                  </div>
                </div>
              </motion.div>
            );
          })}
        </div>
      )}

      {rows.length > 0 && view === "table" && (
        <div className="panel" style={{ overflow: "hidden" }}>
          <table className="table">
            <thead>
              <tr>
                <th>Tier</th>
                <th>Title</th>
                <th>Seasons</th>
                <th>Episodes</th>
                <th>Size</th>
                <th>Monitored</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => {
                const stats = s.statistics || {};
                const have = stats.episodeFileCount ?? 0;
                const total = stats.episodeCount ?? 0;
                const missing = total - have;
                return (
                  <tr key={`${s.instanceId}-${s.id}`}>
                    <td><TierBadge instanceId={s.instanceId} name={s.instanceName} /></td>
                    <td>
                      {s.title}{" "}
                      {s.year ? <span style={{ color: "var(--ink-faint)" }}>· {s.year}</span> : null}
                    </td>
                    <td className="num">{stats.seasonCount ?? s.seasons?.length ?? "—"}</td>
                    <td className="num">
                      {have}/{total}
                      {missing > 0 && <span style={{ color: "var(--t-4k)" }}> · {missing} missing</span>}
                    </td>
                    <td className="num">{bytes(stats.sizeOnDisk)}</td>
                    <td style={{ color: s.monitored ? "var(--ok)" : "var(--ink-faint)" }}>
                      {s.monitored ? "yes" : "no"}
                    </td>
                    <td>
                      <button className="row-del" title={`Remove ${s.title}`} onClick={() => setRemoving(s)}>
                        ✕
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {removing && (
        <ConfirmDialog
          title={`Remove “${removing.title}”?`}
          message={`This removes the series from ${removing.instanceName}. It can be re-added later.`}
          confirmLabel="Remove"
          danger
          checkboxLabel="Also delete downloaded files from disk"
          busy={busy}
          onConfirm={confirmRemove}
          onCancel={() => !busy && setRemoving(null)}
        />
      )}

      {toast && <div className={`toast ${toast.err ? "err" : ""}`}>{toast.msg}</div>}
    </>
  );
}

function nameOf(data, id) {
  return (data || []).find((s) => s.instanceId === id)?.instanceName || id;
}
