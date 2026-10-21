import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type SecretStatus, type SignIn, type Usage } from "../../api";
import { useAuth } from "../../auth";
import { usePageTitle } from "../../app/pageTitle";
import {
  Banner,
  Button,
  Card,
  ConfirmDialog,
  DataTable,
  ErrorNote,
  PageHeader,
  SkeletonRows,
  StatusPill,
  TextField,
  formatDate,
  useToast,
} from "../../components/ui";

function SecretCard({ s }: { s: SecretStatus }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [value, setValue] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: ["secrets"] });
  const save = useMutation({
    mutationFn: () => api(`/api/secrets/${s.kind}`, { method: "PUT", json: { value } }),
    onSuccess: () => {
      setValue("");
      refresh();
      toast({ title: `${s.label} saved`, tone: "success" });
    },
  });
  const clear = useMutation({
    mutationFn: () => api(`/api/secrets/${s.kind}`, { method: "DELETE" }),
    onSuccess: () => {
      setConfirmClear(false);
      refresh();
    },
  });
  const status = s.status === "unreadable" ? "unreadable" : s.is_set ? "set" : "not_set";
  return (
    <li>
      <Card>
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <TextField
            label={s.label}
            type={s.kind.endsWith("username") ? "text" : "password"}
            autoComplete="off"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={s.is_set ? "Replace…" : "Enter…"}
            hint={
              s.status === "unreadable"
                ? "Can't be read any more. Enter it again."
                : s.is_set && s.updated_at
                  ? `Saved ${formatDate(s.updated_at)}`
                  : undefined
            }
          />
          <StatusPill status={status} />
          <Button type="submit" variant="primary" disabled={!value} pending={save.isPending}>
            Save
          </Button>
          {s.is_set && (
            <Button variant="danger" onClick={() => setConfirmClear(true)}>
              Clear
            </Button>
          )}
        </form>
        <ErrorNote error={save.error || clear.error} />
        <ConfirmDialog
          open={confirmClear}
          onOpenChange={setConfirmClear}
          title={`Clear ${s.label}?`}
          body="Anything that uses it, such as cloning your repositories, stops working until you enter it again."
          confirmLabel="Clear"
          danger
          pending={clear.isPending}
          onConfirm={() => clear.mutate()}
        />
      </Card>
    </li>
  );
}

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
      <p className="muted small">Updated every few minutes.</p>
    </Card>
  );
}

export default function SettingsPage() {
  const secrets = useQuery({ queryKey: ["secrets"], queryFn: () => api<SecretStatus[]>("/api/secrets") });
  usePageTitle("Settings");
  return (
    <div className="page">
      <PageHeader title="Settings" />
      <h2>Access tokens</h2>
      <p className="muted lede">
        Used to clone your repositories and, later, to push branches as you. Tokens are encrypted at rest and never
        shown again after saving.
      </p>
      <ErrorNote error={secrets.error} />
      {secrets.isLoading && <SkeletonRows />}
      <ul className="stack plain-list" aria-label="Access tokens">
        {secrets.data?.map((s) => <SecretCard key={s.kind} s={s} />)}
      </ul>
      <h2>AI usage</h2>
      <AiUsage />
      <h2>Sessions</h2>
      <Sessions />
      <h2>Password</h2>
      <PasswordForm />
    </div>
  );
}
