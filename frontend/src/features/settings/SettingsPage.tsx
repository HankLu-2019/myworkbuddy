import { useState, type FormEvent, type ReactElement } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type SignIn, type Usage, type User } from "../../api";
import IntegrationsTab from "./IntegrationsTab";
import { readTheme, saveTheme, type Theme } from "../../app/theme";
import { useAuth } from "../../auth";
import { usePageTitle } from "../../app/pageTitle";
import {
  Banner,
  Toggle,
  Button,
  Card,
  DataTable,
  ErrorNote,
  KeyValue,
  LinkTabs,
  RadioGroup,
  formatUsd,
  PageHeader,
  SkeletonRows,
  TextField,
  formatDate,
  useToast,
} from "../../components/ui";

function PasswordForm() {
  const { clear } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const change = useMutation({
    mutationFn: () => api("/api/auth/password", { method: "POST", json: { current_password: current, new_password: next } }),
    onSuccess: clear, // all sessions are revoked, so sign in again
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    change.mutate();
  }
  return (
    <Card as="form" className="stack narrow" onSubmit={submit} aria-label="Change password">
      <TextField
        label="Current password"
        type="password"
        autoComplete="current-password"
        value={current}
        onChange={(e) => setCurrent(e.target.value)}
        required
      />
      <TextField
        label="New password (10+ characters)"
        type="password"
        autoComplete="new-password"
        minLength={10}
        value={next}
        onChange={(e) => setNext(e.target.value)}
        required
        hint="Changing your password signs you out everywhere, including here."
      />
      <ErrorNote error={change.error} />
      <div>
        <Button variant="primary" type="submit" pending={change.isPending}>
          Change password
        </Button>
      </div>
    </Card>
  );
}

function device(ua: string | null): string {
  if (!ua) return "Unknown device";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : null;
  const os = /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "macOS" : /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Linux/.test(ua) ? "Linux" : null;
  return browser && os ? `${browser} on ${os}` : ua.slice(0, 60);
}

function Sessions() {
  const qc = useQueryClient();
  const sessions = useQuery({ queryKey: ["sign-ins"], queryFn: () => api<SignIn[]>("/api/auth/sessions") });
  const others = useMutation({
    mutationFn: () => api<{ signed_out: number }>("/api/auth/sessions/sign-out-others", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sign-ins"] }),
  });
  const count = sessions.data?.filter((s) => !s.current).length ?? 0;
  return (
    <>
      <ErrorNote error={sessions.error || others.error} />
      <DataTable
        label="Active sign-ins"
        rows={sessions.data}
        loading={sessions.isLoading}
        rowKey={(s) => s.id}
        columns={[
          {
            key: "device",
            header: "Device",
            cell: (s) => (
              <>
                {device(s.user_agent)} {s.current && <span className="badge badge-neutral">This device</span>}
              </>
            ),
          },
          { key: "ip", header: "IP address", cell: (s) => <span className="mono small">{s.ip ?? "—"}</span> },
          { key: "created", header: "Signed in", cell: (s) => formatDate(s.created_at), sortValue: (s) => s.created_at },
          { key: "seen", header: "Last active", cell: (s) => formatDate(s.last_seen_at), sortValue: (s) => s.last_seen_at },
        ]}
      />
      <div className="actions">
        <Button onClick={() => others.mutate()} pending={others.isPending} disabled={count === 0}>
          Sign out other sessions
        </Button>
        {others.data && (
          <span className="muted small" role="status">
            Signed out {others.data.signed_out} other {others.data.signed_out === 1 ? "session" : "sessions"}.
          </span>
        )}
      </div>
    </>
  );
}

export function usd(n: number): string {
  return `USD ${n.toFixed(2)}`;
}

function AiUsage() {
  const usage = useQuery({ queryKey: ["usage"], queryFn: () => api<Usage>("/api/usage") });
  const u = usage.data;
  if (usage.error) return <ErrorNote error={usage.error} />;
  if (!u) return <SkeletonRows rows={2} />;
  const pct = u.spend_usd !== null && u.budget_usd > 0 ? Math.min(100, (u.spend_usd / u.budget_usd) * 100) : null;
  return (
    <Card className="stack narrow">
      {u.spend_usd === null ? (
        <p className="muted">Spend isn't available right now. Your budget is {usd(u.budget_usd)} a month.</p>
      ) : (
        <>
          <p>
            {usd(u.spend_usd)} of {usd(u.budget_usd)} used
            {u.budget_reset_at && <span className="muted"> · resets {formatDate(u.budget_reset_at)}</span>}
          </p>
          <progress max={100} value={pct ?? 0} aria-label="Share of monthly AI budget used" />
          {pct !== null && pct >= 100 && (
            <Banner tone="danger">You've reached your monthly AI budget. An admin can raise it.</Banner>
          )}
        </>
      )}
      {u.chat_usd_month != null && (
        <KeyValue items={[["Chat replies this month", formatUsd(u.chat_usd_month)]]} />
      )}
      <p className="muted small">Updated every few minutes. Schedules and jobs are listed here once they exist.</p>
    </Card>
  );
}

const ZONES: string[] = (() => {
  try {
    return (Intl as unknown as { supportedValuesOf(k: string): string[] }).supportedValuesOf("timeZone");
  } catch {
    return [];
  }
})();

function ProfileTab() {
  const { user, updateUser } = useAuth();
  const toast = useToast();
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const [name, setName] = useState(user?.display_name ?? "");
  const [email, setEmail] = useState(user?.email ?? "");
  const [zone, setZone] = useState(user?.timezone ?? browserZone);
  const [theme, setTheme] = useState<Theme>(readTheme);
  const save = useMutation({
    mutationFn: (body: Record<string, string>) => api<User>("/api/auth/me", { method: "PATCH", json: body }),
    onSuccess: (u) => {
      updateUser(u);
      toast({ title: "Profile saved", tone: "success" });
    },
  });
  const zoneValid = !ZONES.length || ZONES.includes(zone) || zone === "UTC";
  return (
    <div className="stack">
      {user && !user.timezone_confirmed && (
        <Banner tone="info">
          Times are shown in {user.timezone}. Your browser is in {browserZone}.{" "}
          <Button
            size="sm"
            variant="primary"
            pending={save.isPending}
            onClick={() => {
              setZone(browserZone);
              save.mutate({ timezone: browserZone });
            }}
          >
            Use {browserZone}
          </Button>{" "}
          {browserZone !== user.timezone && (
            <Button size="sm" onClick={() => save.mutate({ timezone: user.timezone ?? browserZone })}>
              Keep {user.timezone}
            </Button>
          )}
        </Banner>
      )}
      <Card
        as="form"
        className="stack narrow"
        aria-label="Profile"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          if (zoneValid) save.mutate({ display_name: name, email, timezone: zone });
        }}
      >
        <TextField label="Display name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
        <TextField
          label="Email"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          hint="Used to find your commits in repositories."
        />
        <TextField
          label="Timezone"
          value={zone}
          list="mwb-zones"
          onChange={(e) => setZone(e.target.value)}
          error={zoneValid ? null : "Pick a timezone from the list, e.g. Europe/Stockholm."}
          hint="Dates, the librarian's nightly run and schedules use it."
        />
        <datalist id="mwb-zones">
          {ZONES.map((z) => (
            <option key={z} value={z} />
          ))}
        </datalist>
        <ErrorNote error={save.error} />
        <div>
          <Button variant="primary" type="submit" pending={save.isPending}>
            Save profile
          </Button>
        </div>
      </Card>
      <Card className="stack narrow">
        <RadioGroup
          legend="Theme"
          value={theme}
          options={[
            ["system", "Same as my system"],
            ["light", "Light"],
            ["dark", "Dark"],
          ]}
          onChange={(t) => {
            setTheme(t);
            saveTheme(t);
          }}
        />
        <p className="muted small">Kept in this browser.</p>
      </Card>
    </div>
  );
}

/** Notifications (07 §4.12): email on or off, and what the daily cap means. */
function NotificationsTab() {
  const { user, updateUser } = useAuth();
  const toast = useToast();
  const save = useMutation({
    mutationFn: (on: boolean) => api<User>("/api/auth/me", { method: "PATCH", json: { email_notifications: on } }),
    onSuccess: (u) => {
      updateUser(u);
      toast({ title: u.email_notifications ? "Email notifications on" : "Email notifications off", tone: "success" });
    },
  });
  return (
    <Card className="stack narrow" aria-label="Notifications">
      <Toggle
        label="Email me about alerts, recoveries and paused schedules"
        checked={!!user?.email_notifications}
        disabled={save.isPending}
        onChange={(on) => save.mutate(on)}
      />
      {!user?.email && (
        <Banner tone="warning">
          Add your email address in <Link to="/settings/profile">Profile</Link> to get emails.
        </Banner>
      )}
      <p className="muted">
        Everything also arrives in your inbox. Each schedule chooses when it notifies (on change, on alert, or every
        run) and whether it emails. There is a daily limit on emails (30 unless your admin changed it); after that, notifications for the rest of
        the day are in the inbox only.
      </p>
      <ErrorNote error={save.error} />
    </Card>
  );
}

const TABS: Record<string, { label: string; element: () => ReactElement | null }> = {
  profile: { label: "Profile", element: ProfileTab },
  integrations: { label: "Integrations", element: IntegrationsTab },
  notifications: { label: "Notifications", element: NotificationsTab },
  usage: { label: "Usage", element: AiUsage },
  sessions: { label: "Sessions", element: Sessions },
  password: { label: "Password", element: PasswordForm },
};

/** Settings tabs (07 §4.12); each tab has its own URL. */
export default function SettingsPage() {
  const { tab = "" } = useParams();
  const current = TABS[tab];
  usePageTitle({ label: "Settings", to: "/settings/profile" }, current?.label);
  if (!current) return <Navigate to="/settings/profile" replace />;
  const Tab = current.element;
  return (
    <div className="page">
      <PageHeader title="Settings" />
      <LinkTabs label="Settings" tabs={Object.entries(TABS).map(([key, t]) => ({ to: `/settings/${key}`, label: t.label }))} />
      <Tab />
    </div>
  );
}
