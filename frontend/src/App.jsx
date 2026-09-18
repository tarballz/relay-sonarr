import { forwardRef, useEffect, useRef, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { MotionConfig } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api.js";
import { ToastProvider } from "./components/ui/Toast.jsx";
import { TierProvider } from "./components/ui/TierProvider.jsx";
import NotFound from "./components/ui/NotFound.jsx";
import { useDialog } from "./components/useDialog.js";
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

const SIDEBAR_ID = "sidebar-nav";

// Traps focus inside the mobile drawer and closes it on Escape, mirroring the
// dialog pattern (`useDialog`). Mounted only while the drawer is open, since
// `useDialog`'s effect runs once per mount — the drawer's own DOM node (held
// in `panelRef`, always mounted for its CSS slide transition) is attached to
// the hook's ref during render, before that effect fires.
function DrawerFocusTrap({ onClose, panelRef }) {
  const dialogRef = useDialog(onClose);
  dialogRef.current = panelRef.current;
  return null;
}

const Sidebar = forwardRef(function Sidebar({ open, onClose }, ref) {
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
    <aside id={SIDEBAR_ID} ref={ref} className={`sidebar${open ? " open" : ""}`}>
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
            <span className="ico" aria-hidden="true">{n.ico}</span>
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
});

export default function App() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const closeDrawer = () => setDrawerOpen(false);
  const sidebarRef = useRef(null);

  // Lock body scroll while the mobile drawer is open.
  useEffect(() => {
    document.body.style.overflow = drawerOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [drawerOpen]);

  return (
    <MotionConfig reducedMotion="user">
      <ToastProvider>
        <TierProvider>
          <div className="shell">
            <a className="skip-link" href="#main">Skip to content</a>
            {/* Mobile-only top bar (hidden ≥861px via CSS) */}
            <header className="topbar">
              <button
                className="topbar-burger"
                aria-label={drawerOpen ? "Close menu" : "Open menu"}
                aria-expanded={drawerOpen}
                aria-controls={SIDEBAR_ID}
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

            <Sidebar ref={sidebarRef} open={drawerOpen} onClose={closeDrawer} />
            {drawerOpen && <DrawerFocusTrap onClose={closeDrawer} panelRef={sidebarRef} />}

            <main className="main" id="main" tabIndex={-1}>
              <Routes>
                <Route path="/" element={<SearchAdd />} />
                <Route path="/activity" element={<Activity />} />
                <Route path="/library" element={<Library />} />
                <Route path="/operations" element={<Operations />} />
                <Route path="/settings" element={<Settings />} />
                <Route path="*" element={<NotFound />} />
              </Routes>
            </main>
          </div>
        </TierProvider>
      </ToastProvider>
    </MotionConfig>
  );
}
