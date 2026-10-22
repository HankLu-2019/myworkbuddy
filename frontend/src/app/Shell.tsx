import { useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, NavLink, Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowLeft,
  CalendarClock,
  Inbox,
  Database,
  Home,
  Library,
  LogOut,
  Menu as MenuIcon,
  MessageSquare,
  Search,
  Settings,
  Shield,
  Users,
} from "lucide-react";
import { api, type Home as HomeData, type Integration, type Project } from "../api";
import { ActivityIndicator } from "./ActivityIndicator";
import { librarianUnseen } from "./seen";
import { useAuth } from "../auth";
import { Button, CountBadge, Dialog, Drawer, Dot, TextField, Tooltip } from "../components/ui";
import FirstPasswordPage from "../features/auth/FirstPasswordPage";
import { useCrumbs } from "./pageTitle";
import { SHORTCUTS, useShortcuts } from "./shortcuts";

type NavItem = { to: string; label: string; icon: ReactNode; badge?: ReactNode; end?: boolean };

function NavItemLink({ item }: { item: NavItem }) {
  return (
    <Tooltip content={item.label} side="right">
      <NavLink to={item.to} end={item.end} className="nav-link">
        {item.icon}
        <span className="nav-label">{item.label}</span>
        {item.badge}
      </NavLink>
    </Tooltip>
  );
}

function useBadges(isAdmin: boolean, userId: number | undefined) {
  // Small attention dots (07 §3.1). Polling stops while the tab is hidden (TanStack's default).
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/api/projects"),
    staleTime: 60_000,
  });
  const integrations = useQuery({
    queryKey: ["integrations"],
    queryFn: () => api<Integration[]>("/api/integrations"),
    staleTime: 300_000,
  });
  const ready = useQuery({
    queryKey: ["ready"],
    queryFn: async () => {
      const res = await fetch("/api/ready", { credentials: "same-origin" });
      return res.ok;
    },
    enabled: isAdmin,
    refetchInterval: 60_000,
  });
  // Shares Home's query and cache: one request a minute at most.
  const home = useQuery({ queryKey: ["home"], queryFn: () => api<HomeData>("/api/home"), staleTime: 60_000 });
  const inbox = useQuery({
    queryKey: ["inbox-count"],
    queryFn: () => api<{ unread: number }>("/api/inbox/unread-count"),
    refetchInterval: 30_000,
  });
  return {
    unread: inbox.data?.unread ?? 0,
    schedulePaused: !!home.data?.needs_you.some((n) => n.kind === "schedule_paused"),
    librarianNew: librarianUnseen(userId, home.data?.librarian?.at),
    sourcesFailed: !!projects.data?.some((p) => p.mine && p.status === "failed"),
    settingsUnreadable: !!integrations.data?.some((i) =>
      i.credentials.some((c) => c.status === "unreadable" || c.status === "invalid"),
    ),
    systemFailing: isAdmin && ready.data === false,
  };
}

/** Grouped sidebar, top bar with breadcrumb and global search, mobile tab bar (07 §3.1, §7). */
export default function Shell() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const crumbs = useCrumbs();
  const search = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [help, setHelp] = useState(false);
  const [more, setMore] = useState(false);
  const isAdmin = user?.role === "admin";
  const badges = useBadges(!!isAdmin, user?.id);
  const fromStart = new URLSearchParams(location.search).get("from") === "getting-started";

  useShortcuts({
    focusSearch: () => search.current?.focus(),
    newChat: () => navigate("/chat?new=1"),
    go: (where) => {
      if (where === "wiki") navigate("/wiki");
      else if (where === "home") navigate("/");
      else if (where === "inbox") navigate("/inbox");
      // Board arrives with Phase 3.
    },
    help: () => setHelp(true),
  });

  if (!user) return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  if (user.must_change_password) return <FirstPasswordPage />;

  const groups: { label?: string; items: NavItem[] }[] = [
    {
      label: "Work",
      items: [
        { to: "/", label: "Home", icon: <Home size={18} aria-hidden />, end: true },
        {
          to: "/inbox",
          label: "Inbox",
          icon: <Inbox size={18} aria-hidden />,
          badge: badges.unread ? <CountBadge count={badges.unread} label="unread" /> : undefined,
        },
        { to: "/chat", label: "Chat", icon: <MessageSquare size={18} aria-hidden /> },
      ],
    },
    {
      label: "Knowledge",
      items: [
        { to: "/search", label: "Search", icon: <Search size={18} aria-hidden /> },
        {
          to: "/wiki",
          label: "Wiki",
          icon: <Library size={18} aria-hidden />,
          badge: badges.librarianNew ? <Dot label="The librarian changed pages" /> : undefined,
        },
        {
          to: "/sources",
          label: "Sources",
          icon: <Database size={18} aria-hidden />,
          badge: badges.sourcesFailed ? <Dot label="A repository failed to index" /> : undefined,
        },
      ],
    },
    {
      label: "Automation",
      items: [
        {
          to: "/schedules",
          label: "Schedules",
          icon: <CalendarClock size={18} aria-hidden />,
          badge: badges.schedulePaused ? <Dot label="A schedule was paused" /> : undefined,
        },
      ],
    },
  ];
  const footer: NavItem[] = [
    {
      to: "/settings",
      label: "Settings",
      icon: <Settings size={18} aria-hidden />,
      badge: badges.settingsUnreadable ? <Dot label="A credential needs your attention" /> : undefined,
    },
    ...(isAdmin
      ? [
          { to: "/admin", label: "Users", icon: <Users size={18} aria-hidden />, end: true },
          {
            to: "/admin/system",
            label: "System",
            icon: <Activity size={18} aria-hidden />,
            badge: badges.systemFailing ? <Dot label="A system check is failing" /> : undefined,
          },
        ]
      : []),
  ];

  function submitSearch(e: FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (q) navigate(`/search?q=${encodeURIComponent(q)}`);
  }

  return (
    <div className="shell">
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <aside className="sidebar" aria-label="Main">
        <Link to="/chat" className="brand">
          <Shield size={20} aria-hidden />
          <span className="nav-label">MyWorkBuddy</span>
        </Link>
        <nav aria-label="Primary">
          {groups.map((g) => (
            <div key={g.label} className="nav-group">
              {g.label && <div className="nav-group-label">{g.label}</div>}
              {g.items.map((item) => (
                <NavItemLink key={item.to} item={item} />
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <nav aria-label="Account" className="nav-group">
            {footer.map((item) => (
              <NavItemLink key={item.to} item={item} />
            ))}
          </nav>
          <div className="who">
            <span>{user.display_name || user.username}</span>
            {user.timezone && <span className="muted small">{user.timezone}</span>}
          </div>
          <Button variant="ghost" icon={<LogOut size={18} aria-hidden />} onClick={logout}>
            Sign out
          </Button>
        </div>
      </aside>

      <header className="topbar">
        <nav aria-label="Breadcrumb" className="breadcrumb">
          <ol>
            {crumbs.map((c, i) => (
              <li key={i}>
                {c.to && i < crumbs.length - 1 ? <Link to={c.to}>{c.label}</Link> : <span aria-current={i === crumbs.length - 1 ? "page" : undefined}>{c.label}</span>}
              </li>
            ))}
          </ol>
        </nav>
        {fromStart && location.pathname !== "/" && (
          <Link to="/" className="back-link">
            <ArrowLeft size={14} aria-hidden /> Back to getting started
          </Link>
        )}
        <ActivityIndicator />
        <form className="global-search" role="search" onSubmit={submitSearch}>
          <TextField
            ref={search}
            label="Search everything"
            hideLabel
            type="search"
            placeholder="Search everything"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <span className="kbd" aria-hidden>
            /
          </span>
        </form>
      </header>

      <main id="main" className="content" tabIndex={-1}>
        <Outlet />
      </main>

      <nav className="tabbar" aria-label="Tabs">
        <NavLink to="/" end>
          <Home size={20} aria-hidden />
          Home
        </NavLink>
        <NavLink to="/inbox">
          <Inbox size={20} aria-hidden />
          Inbox
          {badges.unread > 0 && <CountBadge count={badges.unread} label="unread" />}
        </NavLink>
        <NavLink to="/chat">
          <MessageSquare size={20} aria-hidden />
          Chat
        </NavLink>
        <NavLink to="/search">
          <Search size={20} aria-hidden />
          Search
        </NavLink>
        <button type="button" onClick={() => setMore(true)}>
          <MenuIcon size={20} aria-hidden />
          More
        </button>
      </nav>

      <Drawer open={more} onOpenChange={setMore} title="More">
        <ul className="plain-list stack">
          {[
            { to: "/wiki", label: "Wiki", icon: <Library size={18} aria-hidden /> },
            { to: "/sources", label: "Sources", icon: <Database size={18} aria-hidden /> },
            { to: "/schedules", label: "Schedules", icon: <CalendarClock size={18} aria-hidden /> },
          ].map((item) => (
            <li key={item.to}>
              <NavLink to={item.to} className="list-item" onClick={() => setMore(false)}>
                {item.icon} {item.label}
              </NavLink>
            </li>
          ))}
          {footer.map((item) => (
            <li key={item.to}>
              <NavLink to={item.to} end={item.end} className="list-item" onClick={() => setMore(false)}>
                {item.icon} {item.label}
              </NavLink>
            </li>
          ))}
          <li>
            <Button variant="ghost" icon={<LogOut size={18} aria-hidden />} onClick={logout}>
              Sign out
            </Button>
          </li>
        </ul>
      </Drawer>

      <Dialog open={help} onOpenChange={setHelp} title="Keyboard shortcuts">
        <dl className="shortcut-list">
          {SHORTCUTS.map(([keys, what]) => (
            <div key={keys} style={{ display: "contents" }}>
              <dt>
                <span className="kbd">{keys}</span>
              </dt>
              <dd>{what}</dd>
            </div>
          ))}
        </dl>
      </Dialog>
    </div>
  );
}
