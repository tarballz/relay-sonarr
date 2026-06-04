import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { TierBadge, Spinner } from "../components/Shared.jsx";

export default function Settings() {
  const { data: instances, isLoading } = useQuery({ queryKey: ["instances"], queryFn: api.instances });
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.settings });

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Settings</h1>
          <p className="page-sub">Configured instances and smart-add fallback routing.</p>
        </div>
      </div>

      {isLoading && <div style={{ padding: 24 }}><Spinner /></div>}

      <div className="section-label">Instances</div>
      <div className="result-grid" style={{ marginBottom: 32 }}>
        {(instances || []).map((i) => (
          <div key={i.id} className="panel" style={{ padding: "16px 18px", display: "flex", alignItems: "center", gap: 14 }}>
            <span className={`pill ${i.online ? "online" : "offline"}`}><span className="led" /></span>
            <TierBadge instanceId={i.id} name={i.name} />
            <span className="num" style={{ color: "var(--ink-dim)" }}>{i.url}</span>
            <span style={{ marginLeft: "auto", color: i.online ? "var(--ok)" : "var(--danger)", fontSize: 13 }}>
              {i.online ? `online · v${i.version || "?"}` : "offline"}
            </span>
          </div>
        ))}
      </div>

      <div className="section-label">Smart-add fallback chain</div>
      <div className="panel" style={{ padding: "18px 20px" }}>
        {settings && Object.keys(settings.fallbackChains || {}).length > 0 ? (
          Object.entries(settings.fallbackChains).map(([from, steps]) => (
            <div key={from} style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 0", flexWrap: "wrap" }}>
              <TierBadge instanceId={from} name={nameOf(instances, from)} />
              {steps.map((s, i) => (
                <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                  <span style={{ color: "var(--ink-faint)" }}>→</span>
                  <TierBadge instanceId={s.instanceId} name={nameOf(instances, s.instanceId)} />
                  {s.profile && (
                    <span className="num" style={{ color: "var(--ink-dim)" }}>· {s.profile}</span>
                  )}
                </span>
              ))}
            </div>
          ))
        ) : (
          <span style={{ color: "var(--ink-faint)" }}>No fallback chains configured.</span>
        )}
        <p style={{ color: "var(--ink-faint)", fontSize: 12.5, marginTop: 14, marginBottom: 0 }}>
          Each arrow is tried only if the previous tier had no qualifying release. Edit chains in{" "}
          <span className="num">config.yaml</span>.
        </p>
      </div>
    </>
  );
}

function nameOf(instances, id) {
  return (instances || []).find((i) => i.id === id)?.name || id;
}
