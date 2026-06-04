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

function Sidebar() {
  const { data: instances } = useQuery({
    queryKey: ["instances"],
    queryFn: api.instances,
    refetchInterval: 30000,
  });

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">
          relay<span className="dot">.</span>
        </div>
      </div>
      <div className="brand-sub">unified sonarr</div>

      <nav className="nav">
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => (isActive ? "active" : "")}>
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
      </div>
    </aside>
  );
}

export default function App() {
  return (
    <div className="shell">
      <Sidebar />
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
