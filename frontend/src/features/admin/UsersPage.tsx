import { useState, type FormEvent } from "react";
import { Navigate } from "react-router-dom";
import { Copy, UserPlus } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type OffboardingPreview, type Usage, type User, type UserWithPassword } from "../../api";
import { useAuth } from "../../auth";
import { usePageTitle } from "../../app/pageTitle";
import { AdminTabs } from "./AdminTabs";
import {
  Banner,
  Button,
  Card,
  ConfirmDialog,
  DataTable,
  Dialog,
  ErrorNote,
  PageHeader,
  Select,
  SkeletonRows,
  StatusPill,
  TextField,
  formatDate,
  useToast,
} from "../../components/ui";

const LABELS: Record<string, string> = {
  documents: "documents",
  wiki_pages: "wiki pages",
  projects: "repositories",
  chat_sessions: "chats",
  episodes: "session summaries",
  artifacts: "files",
  secrets: "saved tokens",
  sign_ins: "sign-ins",
};

function summary(counts: Record<string, number>): string {
  const parts = Object.entries(counts)
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${n} ${LABELS[k] ?? k}`);
  return parts.length ? parts.join(", ") : "nothing";
}

const ROLES = [
  ["member", "Member"],
  ["admin", "Admin"],
] as const;

/** The generated password, shown once with a copy button (07-ux §4 Admin). */
function OneTimePassword({ result, onClose }: { result: UserWithPassword | null; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <Dialog
      open={!!result}
      onOpenChange={(open) => {
        if (!open) {
          setCopied(false);
          onClose();
        }
      }}
      title="One-time password"
      description={
        result && (
          <p>
            For <strong>{result.username}</strong>. It is shown only now; give it to them and ask them to change it
            when they sign in.
          </p>
        )
      }
      actions={
        <Button
          variant="primary"
          onClick={() => {
            setCopied(false);
            onClose();
          }}
        >
          Done
        </Button>
      }
    >
      {result && (
        <p className="row">
          <code className="mono code-inline" aria-label="Password">
            {result.initial_password}
          </code>
          <Button
            size="sm"
            icon={<Copy size={14} aria-hidden />}
            onClick={() => {
              void navigator.clipboard?.writeText(result.initial_password).then(() => setCopied(true));
            }}
          >
            {copied ? "Copied" : "Copy"}
          </Button>
        </p>
      )}
    </Dialog>
  );
}

function CreateUser({ onCreated }: { onCreated: (r: UserWithPassword) => void }) {
  const qc = useQueryClient();
  const empty = { username: "", email: "", display_name: "", role: "member" };
  const [form, setForm] = useState(empty);
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });
  const create = useMutation({
    mutationFn: () =>
      api<UserWithPassword>("/api/admin/users", {
        method: "POST",
        json: { ...form, email: form.email || null, display_name: form.display_name || null },
      }),
    onSuccess: (r) => {
      setForm(empty);
      qc.invalidateQueries({ queryKey: ["users"] });
      onCreated(r);
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    create.mutate();
  }
  return (
    <Card as="form" onSubmit={submit} aria-label="Add a user">
      <div className="form-grid">
        <TextField label="Username" value={form.username} onChange={set("username")} required autoComplete="off" />
        <TextField label="Display name" value={form.display_name} onChange={set("display_name")} />
        <TextField
          label="Email"
          type="email"
          value={form.email}
          onChange={set("email")}
          hint="Used to find their commits."
        />
        <Select label="Role" value={form.role} onChange={set("role")} options={ROLES} />
        <Button variant="primary" type="submit" icon={<UserPlus size={16} aria-hidden />} pending={create.isPending}>
          Add user
        </Button>
      </div>
      <ErrorNote error={create.error} />
    </Card>
  );
}

/** Delete with a preview of what moves and what goes; type the username to confirm (07-ux §4 Admin). */
function Offboard({ target, admins, onDone }: { target: User | null; admins: User[]; onDone: () => void }) {
  const { user } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const [to, setTo] = useState<number>(user?.id ?? admins[0]?.id ?? 0);
  const [typed, setTyped] = useState("");
  const preview = useQuery({
    queryKey: ["offboarding", target?.id, to],
    queryFn: () => api<OffboardingPreview>(`/api/admin/users/${target!.id}/offboarding-preview?transfer_to=${to}`),
    enabled: !!to && !!target,
  });
  const remove = useMutation({
    mutationFn: () => api<OffboardingPreview>(`/api/admin/users/${target!.id}?transfer_to=${to}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      toast({ title: `${target?.username} deleted` });
      close();
    },
  });
  function close() {
    setTyped("");
    remove.reset();
    onDone();
  }
  return (
    <Dialog
      open={!!target}
      onOpenChange={(open) => !open && close()}
      title={`Delete ${target?.username ?? ""}`}
      description="Check what moves to the new owner and what is deleted. This can't be undone."
      actions={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button
            variant="danger"
            disabled={typed !== target?.username || !preview.data}
            pending={remove.isPending}
            onClick={() => remove.mutate()}
          >
            Delete user
          </Button>
        </>
      }
    >
      <div className="stack">
        <Select
          label="Give their team items to"
          value={String(to)}
          onChange={(e) => setTo(Number(e.target.value))}
          options={admins.map((a) => [String(a.id), a.display_name || a.username] as [string, string])}
        />
        <ErrorNote error={preview.error || remove.error} />
        {preview.isLoading && <SkeletonRows rows={2} />}
        {preview.data && (
          <ul>
            <li>Moves to the new owner: {summary(preview.data.transfer)}.</li>
            <li>Deleted: {summary(preview.data.delete)}.</li>
            {preview.data.duplicate_projects.length > 0 && (
              <li>
                {preview.data.duplicate_projects.length} shared repository already added by the new owner is deleted
                instead of moved.
              </li>
            )}
          </ul>
        )}
        <TextField
          label={`Type ${target?.username ?? ""} to confirm`}
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          autoComplete="off"
        />
      </div>
    </Dialog>
  );
}

function Budget({ u, usage }: { u: User; usage?: Usage }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const save = useMutation({
    mutationFn: () =>
      api<User>(`/api/admin/users/${u.id}`, { method: "PATCH", json: { llm_budget_usd: value === "" ? null : value } }),
    onSuccess: () => {
      setEditing(false);
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["usage-all"] });
    },
  });
  const spend = usage?.spend_usd;
  const budget = usage?.budget_usd ?? u.llm_budget_usd;
  if (editing) {
    return (
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <TextField
          label={`Monthly budget for ${u.username} (USD)`}
          hideLabel
          type="number"
          min={0}
          step="0.01"
          value={value}
          placeholder="default"
          onChange={(e) => setValue(e.target.value)}
        />
        <Button size="sm" type="submit" variant="primary" pending={save.isPending}>
          Save
        </Button>
        <Button size="sm" onClick={() => setEditing(false)}>
          Cancel
        </Button>
        <ErrorNote error={save.error} />
      </form>
    );
  }
  return (
    <>
      {spend === null || spend === undefined ? "—" : `USD ${spend.toFixed(2)}`}
      {budget !== null && budget !== undefined && <span className="muted"> / {budget.toFixed(2)}</span>}{" "}
      <Button
        variant="link"
        aria-label={`Change budget for ${u.username}`}
        onClick={() => {
          setValue(u.llm_budget_usd === null || u.llm_budget_usd === undefined ? "" : String(u.llm_budget_usd));
          setEditing(true);
        }}
      >
        Change
      </Button>
    </>
  );
}

export default function UsersPage() {
  const { user } = useAuth();
  const qc = useQueryClient();
  usePageTitle({ label: "Admin", to: "/admin" }, "Users");
  const [oneTime, setOneTime] = useState<UserWithPassword | null>(null);
  const [deleting, setDeleting] = useState<User | null>(null);
  const [resetting, setResetting] = useState<User | null>(null);
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<User[]>("/api/admin/users"), enabled: user?.role === "admin" });
  const usage = useQuery({
    queryKey: ["usage-all"],
    queryFn: () => api<Usage[]>("/api/admin/usage"),
    enabled: user?.role === "admin",
  });
  const usageOf = new Map((usage.data ?? []).map((x) => [x.user_id, x]));
  const update = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) =>
      api<User>(`/api/admin/users/${id}`, { method: "PATCH", json: body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });
  const reset = useMutation({
    mutationFn: (id: number) => api<UserWithPassword>(`/api/admin/users/${id}/reset-password`, { method: "POST" }),
    onSuccess: (r) => {
      setResetting(null);
      setOneTime(r);
      qc.invalidateQueries({ queryKey: ["users"] });
    },
  });

  if (user?.role !== "admin") return <Navigate to="/chat" replace />;
  const admins = (users.data ?? []).filter((u) => u.role === "admin" && u.is_active && u.id !== deleting?.id);

  return (
    <div className="page">
      <PageHeader title="Admin" />
      <AdminTabs />
      <CreateUser onCreated={setOneTime} />
      <OneTimePassword result={oneTime} onClose={() => setOneTime(null)} />
      {deleting && <Offboard key={deleting.id} target={deleting} admins={admins} onDone={() => setDeleting(null)} />}
      <ConfirmDialog
        open={!!resetting}
        onOpenChange={(open) => !open && setResetting(null)}
        title={`Reset the password for ${resetting?.username ?? ""}?`}
        body="They are signed out everywhere and must sign in with a new one-time password."
        confirmLabel="Reset password"
        danger
        pending={reset.isPending}
        onConfirm={() => resetting && reset.mutate(resetting.id)}
      />
      <ErrorNote error={update.error || reset.error || users.error} />
      {usage.error && <Banner tone="warning">AI spend isn't available right now.</Banner>}
      <DataTable
        label="Users"
        rows={users.data}
        loading={users.isLoading}
        rowKey={(u) => u.id}
        rowLabel={(u) => u.username}
        columns={[
          {
            key: "user",
            header: "User",
            sortValue: (u) => u.username,
            cell: (u) => (
              <>
                {u.username}
                {u.display_name && <div className="muted small">{u.display_name}</div>}
              </>
            ),
          },
          { key: "email", header: "Email", cell: (u) => u.email ?? "—" },
          {
            key: "role",
            header: "Role",
            cell: (u) => (
              <Select
                label={`Role of ${u.username}`}
                hideLabel
                value={u.role}
                disabled={u.id === user.id}
                options={ROLES}
                onChange={(e) => update.mutate({ id: u.id, body: { role: e.target.value } })}
              />
            ),
          },
          {
            key: "status",
            header: "Status",
            cell: (u) => (
              <>
                <StatusPill status={u.is_active ? "active" : "disabled"} />
                {u.must_change_password && <div className="muted small">Must change password</div>}
              </>
            ),
          },
          {
            key: "login",
            header: "Last sign-in",
            sortValue: (u) => u.last_login_at ?? "",
            cell: (u) => (u.last_login_at ? formatDate(u.last_login_at) : <span className="muted">Never</span>),
          },
          { key: "budget", header: "AI spend / budget (USD)", cell: (u) => <Budget u={u} usage={usageOf.get(u.id)} /> },
          {
            key: "actions",
            header: <span className="visually-hidden">Actions</span>,
            className: "row-actions",
            cell: (u) => (
              <>
                <Button size="sm" onClick={() => setResetting(u)}>
                  Reset password
                </Button>
                {u.id !== user.id && (
                  <>
                    <Button size="sm" onClick={() => update.mutate({ id: u.id, body: { is_active: !u.is_active } })}>
                      {u.is_active ? "Disable" : "Enable"}
                    </Button>
                    <Button size="sm" variant="danger" onClick={() => setDeleting(u)}>
                      Delete
                    </Button>
                  </>
                )}
              </>
            ),
          },
        ]}
      />
    </div>
  );
}
