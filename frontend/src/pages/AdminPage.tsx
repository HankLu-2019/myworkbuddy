import { useState, type FormEvent } from "react";
import { Navigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type OffboardingPreview, type Usage, type User, type UserWithPassword } from "../api";
import { useAuth } from "../auth";
import { ErrorNote, PageHeader, formatDate } from "../components/ui";

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

/** The generated password, shown once with a copy button (07-ux §4 Admin). */
function OneTimePassword({ result, onClose }: { result: UserWithPassword; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="card notice" role="dialog" aria-label="One-time password">
      <p>
        One-time password for <strong>{result.username}</strong>. It is shown only now; give it to them and ask them
        to change it when they sign in.
      </p>
      <p>
        <code className="mono" aria-label="Password">
          {result.initial_password}
        </code>{" "}
        <button
          onClick={() => {
            void navigator.clipboard?.writeText(result.initial_password).then(() => setCopied(true));
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </p>
      <button className="primary" onClick={onClose}>
        Done
      </button>
    </div>
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
    <form className="card form-grid" onSubmit={submit}>
      <input aria-label="Username" placeholder="username" value={form.username} onChange={set("username")} required />
      <input aria-label="Display name" placeholder="Display name" value={form.display_name} onChange={set("display_name")} />
      <input aria-label="Email" type="email" placeholder="email (used to find your commits)" value={form.email} onChange={set("email")} />
      <select aria-label="Role" value={form.role} onChange={set("role")}>
        <option value="member">Member</option>
        <option value="admin">Admin</option>
      </select>
      <button className="primary" type="submit" disabled={create.isPending}>
        Add user
      </button>
      <ErrorNote error={create.error} />
    </form>
  );
}

/** Delete with a preview of what moves and what goes; type the username to confirm (07-ux §4 Admin). */
function Offboard({ target, admins, onDone }: { target: User; admins: User[]; onDone: () => void }) {
  const { user } = useAuth();
  const qc = useQueryClient();
  const [to, setTo] = useState<number>(user?.id ?? admins[0]?.id ?? 0);
  const [typed, setTyped] = useState("");
  const preview = useQuery({
    queryKey: ["offboarding", target.id, to],
    queryFn: () => api<OffboardingPreview>(`/api/admin/users/${target.id}/offboarding-preview?transfer_to=${to}`),
    enabled: !!to,
  });
  const remove = useMutation({
    mutationFn: () => api<OffboardingPreview>(`/api/admin/users/${target.id}?transfer_to=${to}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      onDone();
    },
  });
  return (
    <div className="card" role="dialog" aria-label={`Delete ${target.username}`}>
      <h2>Delete {target.username}</h2>
      <label htmlFor="transfer-to">Give their team items to</label>
      <select id="transfer-to" value={to} onChange={(e) => setTo(Number(e.target.value))}>
        {admins.map((a) => (
          <option key={a.id} value={a.id}>
            {a.display_name || a.username}
          </option>
        ))}
      </select>
      <ErrorNote error={preview.error || remove.error} />
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
      <label htmlFor="confirm-username">Type {target.username} to confirm</label>
      <input id="confirm-username" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
      <div className="actions">
        <button
          className="danger"
          disabled={typed !== target.username || !preview.data || remove.isPending}
          onClick={() => remove.mutate()}
        >
          Delete user
        </button>
        <button onClick={onDone}>Cancel</button>
      </div>
    </div>
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
        <input
          aria-label={`Monthly budget for ${u.username} (USD)`}
          type="number"
          min={0}
          step="0.01"
          value={value}
          placeholder="default"
          onChange={(e) => setValue(e.target.value)}
        />
        <button type="submit" disabled={save.isPending}>
          Save
        </button>
        <button type="button" onClick={() => setEditing(false)}>
          Cancel
        </button>
        <ErrorNote error={save.error} />
      </form>
    );
  }
  return (
    <>
      {spend === null || spend === undefined ? "—" : `USD ${spend.toFixed(2)}`}
      {budget !== null && budget !== undefined && <span className="muted"> / {budget.toFixed(2)}</span>}{" "}
      <button
        className="link"
        aria-label={`Change budget for ${u.username}`}
        onClick={() => {
          setValue(u.llm_budget_usd === null || u.llm_budget_usd === undefined ? "" : String(u.llm_budget_usd));
          setEditing(true);
        }}
      >
        Change
      </button>
    </>
  );
}

export default function AdminPage() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const [oneTime, setOneTime] = useState<UserWithPassword | null>(null);
  const [deleting, setDeleting] = useState<User | null>(null);
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
      setOneTime(r);
      qc.invalidateQueries({ queryKey: ["users"] });
    },
  });

  if (user?.role !== "admin") return <Navigate to="/chat" replace />;
  const admins = (users.data ?? []).filter((u) => u.role === "admin" && u.is_active && u.id !== deleting?.id);

  return (
    <div className="page">
      <PageHeader title="Users" />
      <CreateUser onCreated={setOneTime} />
      {oneTime && <OneTimePassword result={oneTime} onClose={() => setOneTime(null)} />}
      {deleting && <Offboard target={deleting} admins={admins} onDone={() => setDeleting(null)} />}
      <ErrorNote error={update.error || reset.error || users.error} />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>User</th>
              <th>Email</th>
              <th>Role</th>
              <th>Status</th>
              <th>Last sign-in</th>
              <th>AI spend / budget (USD)</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {users.data?.map((u) => (
              <tr key={u.id}>
                <td>
                  {u.username}
                  {u.display_name && <div className="muted small">{u.display_name}</div>}
                </td>
                <td>{u.email ?? "—"}</td>
                <td>
                  <select
                    aria-label={`Role of ${u.username}`}
                    value={u.role}
                    disabled={u.id === user.id}
                    onChange={(e) => update.mutate({ id: u.id, body: { role: e.target.value } })}
                  >
                    <option value="member">Member</option>
                    <option value="admin">Admin</option>
                  </select>
                </td>
                <td>
                  {u.is_active ? "Active" : <span className="muted">Disabled</span>}
                  {u.must_change_password && <div className="muted small">Must change password</div>}
                </td>
                <td>{u.last_login_at ? formatDate(u.last_login_at) : <span className="muted">Never</span>}</td>
                <td>
                  <Budget u={u} usage={usageOf.get(u.id)} />
                </td>
                <td className="row-actions">
                  <button
                    onClick={() =>
                      window.confirm(`Reset the password for ${u.username}? They will be signed out everywhere.`) &&
                      reset.mutate(u.id)
                    }
                  >
                    Reset password
                  </button>
                  {u.id !== user.id && (
                    <>
                      <button onClick={() => update.mutate({ id: u.id, body: { is_active: !u.is_active } })}>
                        {u.is_active ? "Disable" : "Enable"}
                      </button>
                      <button className="danger" onClick={() => setDeleting(u)}>
                        Delete
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
