import { useState, type FormEvent } from "react";
import { Navigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type User } from "../api";
import { useAuth } from "../auth";
import { ErrorNote, PageHeader } from "../components/ui";

function CreateUser() {
  const qc = useQueryClient();
  const [form, setForm] = useState({ username: "", email: "", display_name: "", password: "", role: "member" });
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });
  const create = useMutation({
    mutationFn: () => api<User>("/api/admin/users", { method: "POST", json: { ...form, email: form.email || null } }),
    onSuccess: () => {
      setForm({ username: "", email: "", display_name: "", password: "", role: "member" });
      qc.invalidateQueries({ queryKey: ["users"] });
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
      <input aria-label="Initial password" type="password" placeholder="initial password (10+)" value={form.password} onChange={set("password")} required />
      <select aria-label="Role" value={form.role} onChange={set("role")}>
        <option value="member">Member</option>
        <option value="admin">Admin</option>
      </select>
      <button className="primary" type="submit" disabled={create.isPending}>
        Create user
      </button>
      <ErrorNote error={create.error} />
    </form>
  );
}

export default function AdminPage() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<User[]>("/api/admin/users"), enabled: user?.role === "admin" });
  const update = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) =>
      api<User>(`/api/admin/users/${id}`, { method: "PATCH", json: body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });

  if (user?.role !== "admin") return <Navigate to="/chat" replace />;

  function resetPassword(u: User) {
    const password = window.prompt(`New password for ${u.username} (10+ characters)`);
    if (password) update.mutate({ id: u.id, body: { password } });
  }

  return (
    <div className="page">
      <PageHeader title="Users" />
      <CreateUser />
      <ErrorNote error={update.error || users.error} />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>User</th>
              <th>Email</th>
              <th>Role</th>
              <th>Status</th>
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
                <td>{u.is_active ? "Active" : <span className="muted">Disabled</span>}</td>
                <td className="row-actions">
                  <button onClick={() => resetPassword(u)}>Reset password</button>
                  {u.id !== user.id && (
                    <button onClick={() => update.mutate({ id: u.id, body: { is_active: !u.is_active } })}>
                      {u.is_active ? "Disable" : "Enable"}
                    </button>
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
