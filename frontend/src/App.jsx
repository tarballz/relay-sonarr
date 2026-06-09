import { useEffect, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api.js";
import SearchAdd from "./pages/SearchAdd.jsx";
import Activity from "./pages/Activity.jsx";
import Library from "./pages/Library.jsx";
import Operations from "./pages/Operations.jsx";
import Settings from "./pages/Settings.jsx";

const NAV = [
  { to: "/", label: "Search & Add", ico: "⌕", end: true },
  { to: "/activity", label: "Activity", ico: "≋" },
  { to: "/library", label: "Library", ico: "▦" },
  { to: "/operations", label: "Operations", ico: "❡" },
  { to: "/settings", label: "Settings", ico: "⚙" },
];

function Sidebar({ open, onClose }) {
  const { data: instances } = useQuery({
    queryKey: ["instances"],
    queryFn: api.instances,
    refetchInterval: 30000,
  });
  const { data: rec } = useQuery({
    queryKey: ["reconciler-status"],
    queryFn: api.reconcilerStatus,
    refetchInterval: 30000,
  });
  const recState = !rec ? null : !rec.enabled ? "off" : !rec.healthy ? "err" : "ok";

  return (
    <aside className={`sidebar${open ? " open" : ""}`}>
      <div className="brand">
        <div className="brand-mark">
          relay<span className="dot">.</span>
        </div>
      </div>
      <div className="brand-sub">unified sonarr</div>

      <nav className="nav">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.end}
            onClick={onClose}
            className={({ isActive }) => (isActive ? "active" : "")}
          >
            <span className="ico">{n.ico}</span>
            {n.label}
          </NavLink>
        ))}
      </nav>

      <div className="sidebar-foot">
        {(instances || []).map((i) => (
          <div key={i.id}>
            {i.online ? "●" : "○"} {i.name}
          </div>
        ))}
        {recState && (
          <div className={`rec-foot rec-${recState}`} title={rec.lastError || ""}>
            <span className="rec-dot" /> reconciler
          </div>
        )}
      </div>
    </aside>
  );
}

export default function App() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const closeDrawer = () => setDrawerOpen(false);

  // Lock body scroll while the mobile drawer is open.
  useEffect(() => {
    document.body.style.overflow = drawerOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [drawerOpen]);

  return (
    <div className="shell">
      {/* Mobile-only top bar (hidden ≥861px via CSS) */}
      <header className="topbar">
        <button
          className="topbar-burger"
          aria-label={drawerOpen ? "Close menu" : "Open menu"}
          aria-expanded={drawerOpen}
          onClick={() => setDrawerOpen((v) => !v)}
        >
          {drawerOpen ? "✕" : "☰"}
        </button>
        <div className="topbar-brand">
          relay<span className="dot">.</span>
        </div>
        <NavLink to="/settings" className="topbar-gear" aria-label="Settings" onClick={closeDrawer}>
          ⚙
        </NavLink>
      </header>

      {/* Dimmer behind the off-canvas drawer */}
      <div
        className={`drawer-scrim${drawerOpen ? " open" : ""}`}
        onClick={closeDrawer}
        aria-hidden="true"
      />

      <Sidebar open={drawerOpen} onClose={closeDrawer} />

      <main className="main">
        <Routes>
          <Route path="/" element={<SearchAdd />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/library" element={<Library />} />
          <Route path="/operations" element={<Operations />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}
