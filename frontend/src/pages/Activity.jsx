import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { TierBadge, Spinner, Empty, bytes } from "../components/Shared.jsx";

export default function Activity() {
  const { data, isLoading } = useQuery({
    queryKey: ["queue"],
    queryFn: api.queue,
    refetchInterval: 5000, // live queue
  });

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Activity</h1>
          <p className="page-sub">Live download queue across every tier · refreshes every 5s.</p>
        </div>
      </div>

      {isLoading && <div style={{ padding: 24 }}><Spinner /></div>}

      {data && data.length === 0 && <Empty big="Queue is empty">Nothing downloading right now.</Empty>}

      {data && data.length > 0 && (
        <div className="panel" style={{ overflow: "hidden" }}>
          <table className="table">
            <thead>
              <tr>
                <th>Tier</th>
                <th>Title</th>
                <th>Episode</th>
                <th>Progress</th>
                <th>Size</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.map((r, i) => {
                const pct = r.size ? Math.round(((r.size - (r.sizeleft ?? 0)) / r.size) * 100) : 0;
                const ep = r.episode;
                return (
                  <tr key={`${r.instanceId}-${r.id}-${i}`}>
                    <td><TierBadge instanceId={r.instanceId} name={r.instanceName} /></td>
                    <td>{r.series?.title || r.title || "—"}</td>
                    <td className="num">
                      {ep ? `S${pad(ep.seasonNumber)}E${pad(ep.episodeNumber)}` : "—"}
                    </td>
                    <td>
                      <div className="progress" title={`${pct}%`}>
                        <span style={{ width: `${pct}%` }} />
                      </div>
                    </td>
                    <td className="num">{bytes(r.size)}</td>
                    <td><Status r={r} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function Status({ r }) {
  const s = (r.trackedDownloadState || r.status || "").toLowerCase();
  const color =
    s.includes("download") ? "var(--t-1080)" :
    s.includes("import") ? "var(--ok)" :
    s.includes("warn") || s.includes("fail") ? "var(--danger)" : "var(--ink-dim)";
  return <span style={{ color, fontSize: 13, textTransform: "capitalize" }}>{r.status || s || "—"}</span>;
}

function pad(n) {
  return String(n ?? 0).padStart(2, "0");
}
