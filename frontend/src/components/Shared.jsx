import { useTiers } from "./ui/TierProvider.jsx";

export function TierBadge({ instanceId, name }) {
  const { cls, label } = useTiers();
  const tierCls = cls(instanceId);
  const tierLbl = label(instanceId);
  return <span className={`tier ${tierCls}`}>{name || tierLbl || instanceId}</span>;
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

export { bytes } from "../lib/format.js";
