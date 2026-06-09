import { useMemo, useState } from "react";
import { motion } from "framer-motion";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, tierClass } from "../api.js";
import { TierBadge, Spinner, Empty, ErrorState, bytes } from "../components/Shared.jsx";
import ConfirmDialog from "../components/ConfirmDialog.jsx";
import ResolveDialog from "../components/ResolveDialog.jsx";
import PolicyEditor from "../components/PolicyEditor.jsx";
import SeasonDialog from "../components/SeasonDialog.jsx";

function posterUrl(s) {
  const img = (s.images || []).find((i) => i.coverType === "poster");
  return img?.remoteUrl || img?.url || null;
}

// Resolution status badge driven by the reconciler's per-series roll-up.
function StatusBadge({ status, paused }) {
  if (paused) return <span className="status-badge paused">paused</span>;
  if (!status) return null;
  return <span className={`status-badge st-${status}`}>{status.replace("-", " ")}</span>;
}

export default function Library() {
  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ["series"], queryFn: api.series });
  const { data: instances } = useQuery({ queryKey: ["instances"], queryFn: api.instances });
  const { data: statusData } = useQuery({
    queryKey: ["library-status"],
    queryFn: api.libraryStatus,
    refetchInterval: 5000,
  });
  const statusBy = useMemo(
    () => Object.fromEntries((statusData || []).map((s) => [s.tvdbId, s])),
    [statusData]
  );
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState("title"); // title | missing | size | status
  const [view, setView] = useState("grid"); // posters by default — the cooler look
  const [removing, setRemoving] = useState(null); // series pending deletion
  const [resolving, setResolving] = useState(null); // series whose availability we're resolving
  const [editing, setEditing] = useState(null); // series whose policy we're editing
  const [seasonsOf, setSeasonsOf] = useState(null); // series whose per-season status we're viewing
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState(null);
  const showToast = (msg, err) => {
    setToast({ msg, err });
    setTimeout(() => setToast(null), 4000);
  };

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

  const missingOf = (s) => (s.statistics?.episodeCount ?? 0) - (s.statistics?.episodeFileCount ?? 0);
  const q = search.trim().toLowerCase();
  const rows = (data || [])
    .filter((s) => filter === "all" || s.instanceId === filter)
    .filter((s) => !q || (s.title || "").toLowerCase().includes(q))
    .sort((a, b) => {
      if (sort === "missing") return missingOf(b) - missingOf(a);
      if (sort === "size") return (b.statistics?.sizeOnDisk ?? 0) - (a.statistics?.sizeOnDisk ?? 0);
      if (sort === "status") {
        return (statusBy[a.tvdbId]?.status || "~").localeCompare(statusBy[b.tvdbId]?.status || "~");
      }
      return (a.sortTitle || a.title || "").localeCompare(b.sortTitle || b.title || "");
    });

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Library</h1>
          <p className="page-sub">{(data || []).length} series across {tiers.length} tier{tiers.length === 1 ? "" : "s"}.</p>
        </div>
        <div className="health-row">
          <input
            className="input lib-search"
            type="search"
            placeholder="Search library…"
            aria-label="Search library by title"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select
            className="input lib-sort"
            aria-label="Sort library"
            value={sort}
            onChange={(e) => setSort(e.target.value)}
          >
            <option value="title">Title</option>
            <option value="missing">Most missing</option>
            <option value="size">Largest</option>
            <option value="status">Status</option>
          </select>
          <button className={`btn ${filter === "all" ? "primary" : "ghost"}`} onClick={() => setFilter("all")}>All</button>
          {tiers.map((id) => (
            <button key={id} className={`btn ${filter === id ? tierClass(id) : "ghost"}`} onClick={() => setFilter(id)}>
              {nameOf(data, id)}
            </button>
          ))}
          <span className="view-toggle">
            <button className={view === "grid" ? "on" : ""} onClick={() => setView("grid")} title="Poster grid" aria-label="Poster grid view">▦</button>
            <button className={view === "table" ? "on" : ""} onClick={() => setView("table")} title="Table" aria-label="Table view">≣</button>
          </span>
        </div>
      </div>

      {isLoading && <div style={{ padding: 24 }}><Spinner /></div>}
      {isError && <ErrorState what="your library" error={error} onRetry={refetch} />}
      {data && data.length === 0 && <Empty big="Library is empty">Add a show from Search & Add.</Empty>}
      {data && data.length > 0 && rows.length === 0 && (
        <Empty big="No matches">Nothing matches “{search}”{filter !== "all" ? " in this tier" : ""}.</Empty>
      )}

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
                  {url ? <img src={url} alt={`Poster for ${s.title}`} loading="lazy" /> : <div className="poster-ph">{(s.title || "?")[0]}</div>}
                  <span className={`poster-tier ${tcls}`}>{shortTier}</span>
                  <button
                    className="poster-policy"
                    title={`Policy for ${s.title}`}
                    aria-label={`Edit policy for ${s.title}`}
                    onClick={() => setEditing(s)}
                  >
                    ⚙
                  </button>
                  <button
                    className="poster-seasons"
                    title={`Seasons for ${s.title}`}
                    aria-label={`View seasons for ${s.title}`}
                    onClick={() => setSeasonsOf(s)}
                  >
                    ▤
                  </button>
                  {missing > 0 && (
                    <button
                      className="poster-resolve"
                      title={`Resolve ${missing} missing episode${missing === 1 ? "" : "s"}`}
                      aria-label={`Resolve ${missing} missing episode${missing === 1 ? "" : "s"} for ${s.title}`}
                      onClick={() => setResolving(s)}
                    >
                      ⟳
                    </button>
                  )}
                  <button
                    className="poster-del"
                    title={`Remove ${s.title}`}
                    aria-label={`Remove ${s.title}`}
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
                    <div className="poster-status">
                      <StatusBadge
                        status={statusBy[s.tvdbId]?.status}
                        paused={statusBy[s.tvdbId]?.paused}
                      />
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
                <th>Status</th>
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
                    <td data-label="Tier"><TierBadge instanceId={s.instanceId} name={s.instanceName} /></td>
                    <td data-label="Title">
                      {s.title}{" "}
                      {s.year ? <span style={{ color: "var(--ink-faint)" }}>· {s.year}</span> : null}
                    </td>
                    <td className="num" data-label="Seasons">{stats.seasonCount ?? s.seasons?.length ?? "—"}</td>
                    <td className="num" data-label="Episodes">
                      {have}/{total}
                      {missing > 0 && <span style={{ color: "var(--t-4k)" }}> · {missing} missing</span>}
                    </td>
                    <td className="num" data-label="Size">{bytes(stats.sizeOnDisk)}</td>
                    <td data-label="Monitored" style={{ color: s.monitored ? "var(--ok)" : "var(--ink-faint)" }}>
                      {s.monitored ? "yes" : "no"}
                    </td>
                    <td data-label="Status">
                      <StatusBadge
                        status={statusBy[s.tvdbId]?.status}
                        paused={statusBy[s.tvdbId]?.paused}
                      />
                    </td>
                    <td data-label="Actions" className="row-actions">
                      <button className="row-resolve" title={`Policy for ${s.title}`} onClick={() => setEditing(s)}>
                        Policy
                      </button>
                      <button className="row-resolve" title={`Seasons for ${s.title}`} onClick={() => setSeasonsOf(s)}>
                        Seasons
                      </button>
                      {missing > 0 && (
                        <button
                          className="row-resolve"
                          title={`Resolve ${missing} missing episode${missing === 1 ? "" : "s"}`}
                          onClick={() => setResolving(s)}
                        >
                          Resolve
                        </button>
                      )}
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

      {resolving && (
        <ResolveDialog
          series={{
            title: resolving.title,
            tvdbId: resolving.tvdbId,
            instanceId: resolving.instanceId,
            seriesId: resolving.id,
          }}
          chainKey={resolving.instanceId}
          autoReattempt
          onClose={() => setResolving(null)}
          onToast={showToast}
        />
      )}

      {editing && instances && (
        <PolicyEditor
          series={{ title: editing.title, tvdbId: editing.tvdbId }}
          instances={instances}
          onClose={() => setEditing(null)}
          onToast={showToast}
        />
      )}

      {seasonsOf && (
        <SeasonDialog
          series={{
            title: seasonsOf.title,
            tvdbId: seasonsOf.tvdbId,
            instanceId: seasonsOf.instanceId,
          }}
          onClose={() => setSeasonsOf(null)}
          onToast={showToast}
        />
      )}

      {toast && <div className={`toast ${toast.err ? "err" : ""}`}>{toast.msg}</div>}
    </>
  );
}

function nameOf(data, id) {
  return (data || []).find((s) => s.instanceId === id)?.instanceName || id;
}
