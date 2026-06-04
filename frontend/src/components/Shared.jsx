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
