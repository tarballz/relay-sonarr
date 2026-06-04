import { useState } from "react";
import { motion } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { TierBadge, Spinner, Empty } from "../components/Shared.jsx";
import AddDialog from "../components/AddDialog.jsx";

function HealthPills() {
  const { data } = useQuery({ queryKey: ["instances"], queryFn: api.instances, refetchInterval: 30000 });
  return (
    <div className="health-row">
      {(data || []).map((i) => (
        <span key={i.id} className={`pill ${i.online ? "online" : "offline"}`}>
          <span className="led" />
          {i.name}
          <span className="ver">{i.online ? `v${i.version || "?"}` : "offline"}</span>
        </span>
      ))}
    </div>
  );
}

export default function SearchAdd() {
  const [term, setTerm] = useState("");
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(null);
  const [toast, setToast] = useState(null);

  const { data: instances } = useQuery({ queryKey: ["instances"], queryFn: api.instances });
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const results = useQuery({
    queryKey: ["search", query],
    queryFn: () => api.search(query),
    enabled: query.length > 1,
  });

  const showToast = (msg, err) => {
    setToast({ msg, err });
    setTimeout(() => setToast(null), 4200);
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Search & Add</h1>
          <p className="page-sub">Find a show and route it to the right tier — or both.</p>
        </div>
        <HealthPills />
      </div>

      <form
        className="search-bar"
        onSubmit={(e) => {
          e.preventDefault();
          setQuery(term.trim());
        }}
      >
        <input
          className="input"
          placeholder="Search for a TV show…"
          value={term}
          onChange={(e) => setTerm(e.target.value)}
          autoFocus
        />
        <button className="btn primary" type="submit">Search</button>
      </form>

      {results.isFetching && <div style={{ padding: 24 }}><Spinner /></div>}

      {results.data && results.data.length > 0 && (
        <div className="result-grid">
          {results.data.map((r, idx) => (
            <motion.div
              key={r.tvdbId || idx}
              className="panel result"
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: Math.min(idx * 0.035, 0.4) }}
            >
              <Poster series={r} />
              <div>
                <h3>{r.title}</h3>
                <div className="meta">
                  {r.year ? <span>{r.year}</span> : null}
                  {r.network ? <span>· {r.network}</span> : null}
                  {r.status ? <span>· {r.status}</span> : null}
                  <span className="mono">tvdb {r.tvdbId}</span>
                </div>
                {r.existsOn?.length > 0 && (
                  <div className="exists-tags">
                    <span style={{ fontSize: 12, color: "var(--ink-faint)" }}>in library:</span>
                    {r.existsOn.map((id) => (
                      <TierBadge key={id} instanceId={id} name={nameOf(instances, id)} />
                    ))}
                  </div>
                )}
              </div>
              <button className="btn" onClick={() => setAdding(r)}>Add</button>
            </motion.div>
          ))}
        </div>
      )}

      {results.data && results.data.length === 0 && (
        <Empty big="Nothing found">Try a different title.</Empty>
      )}
      {!query && <Empty big="Start typing">Search any series to add it to your tiers.</Empty>}

      {adding && instances && (
        <AddDialog
          series={adding}
          instances={instances}
          fallbackChains={settings?.fallbackChains || {}}
          onClose={() => setAdding(null)}
          onToast={showToast}
        />
      )}

      {toast && <div className={`toast ${toast.err ? "err" : ""}`}>{toast.msg}</div>}
    </>
  );
}

function nameOf(instances, id) {
  return (instances || []).find((i) => i.id === id)?.name;
}

function Poster({ series }) {
  const img = (series.images || []).find((i) => i.coverType === "poster");
  const url = img?.remoteUrl || img?.url;
  if (url) return <img className="poster" src={url} alt="" loading="lazy" />;
  return <div className="poster ph">{(series.title || "?")[0]}</div>;
}
