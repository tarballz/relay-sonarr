// Display formatting shared by every page. Pure functions, no React.

const DASH = "—";

export function bytes(n) {
  if (n === null || n === undefined) return DASH;
  const value = Number(n);
  if (!Number.isFinite(value)) return DASH;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let scaled = value;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) {
    scaled /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${Math.round(scaled)} ${units[unit]}` : `${scaled.toFixed(scaled >= 10 ? 0 : 1)} ${units[unit]}`;
}

export function ago(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return DASH;
  const s = Math.max(0, Math.floor(seconds));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function when(iso, now = new Date()) {
  if (!iso) return DASH;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return DASH;
  const time = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const sameDay =
    at.getFullYear() === now.getFullYear() &&
    at.getMonth() === now.getMonth() &&
    at.getDate() === now.getDate();
  if (sameDay) return time;
  return `${at.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

export function duration(ms) {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return DASH;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.round((ms % 60000) / 1000);
  return `${minutes}m ${seconds}s`;
}
