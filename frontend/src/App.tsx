import { NavLink, Navigate, Outlet, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import LoginPage from "./pages/LoginPage";
import ChatPage from "./pages/ChatPage";
import SearchPage from "./pages/SearchPage";
import DocumentsPage from "./pages/DocumentsPage";
import WikiPage from "./pages/WikiPage";
import ProjectsPage from "./pages/ProjectsPage";
import ImportsPage from "./pages/ImportsPage";
import SettingsPage from "./pages/SettingsPage";
import AdminPage from "./pages/AdminPage";
import FirstPasswordPage from "./pages/FirstPasswordPage";

const NAV = [
  { to: "/chat", label: "Chat" },
  { to: "/search", label: "Search" },
  { to: "/wiki", label: "Wiki" },
  { to: "/projects", label: "Projects" },
  { to: "/documents", label: "Documents" },
  { to: "/imports", label: "Imports" },
];

function Layout() {
  const { user, logout } = useAuth();
  if (!user) return <Navigate to="/login" replace />;
  if (user.must_change_password) return <FirstPasswordPage />;
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">MyWorkBuddy</div>
        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to}>
              {n.label}
            </NavLink>
          ))}
          <div className="nav-sep" />
          <NavLink to="/settings">Settings</NavLink>
          {user.role === "admin" && <NavLink to="/admin">Users</NavLink>}
        </nav>
        <div className="sidebar-foot">
          <span className="who">{user.display_name || user.username}</span>
          <button className="link" onClick={logout}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}

export default function App() {
  const { loading } = useAuth();
  if (loading) return <div className="boot">Loading…</div>;
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<Layout />}>
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/:sessionId" element={<ChatPage />} />
        <Route path="/search" element={<SearchPage />} />
        <Route path="/wiki" element={<WikiPage />} />
        <Route path="/wiki/:pageId" element={<WikiPage />} />
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/documents" element={<DocumentsPage />} />
        <Route path="/imports" element={<ImportsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/admin" element={<AdminPage />} />
        <Route path="*" element={<Navigate to="/chat" replace />} />
      </Route>
    </Routes>
  );
}
