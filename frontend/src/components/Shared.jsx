import { tierClass } from "../api.js";

const LABELS = { t1080p: "1080P", t4k: "4K", tdefault: "" };

export function TierBadge({ instanceId, name }) {
  const cls = tierClass(instanceId);
  return <span className={`tier ${cls}`}>{name || LABELS[cls] || instanceId}</span>;
}

export function Spinner() {
  return <span className="spinner" aria-label="loading" />;
}

export function Empty({ big, children }) {
  return (
    <div className="empty">
      <div className="big">{big}</div>
      <div>{children}</div>
    </div>
  );
}

// Distinct from Empty: a query actually failed (offline Sonarr, backend down).
// Shows the error and a retry button instead of silently looking "empty".
export function ErrorState({ error, onRetry, what = "data" }) {
  return (
    <div className="empty error-state" role="alert">
      <div className="big">Couldn’t load {what}</div>
      <div className="error-detail">{error?.message || "Something went wrong."}</div>
      {onRetry && (
        <button className="btn" style={{ marginTop: 14 }} onClick={() => onRetry()}>
          Retry
        </button>
      )}
    </div>
  );
}

export function bytes(n) {
  if (!n && n !== 0) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
}
