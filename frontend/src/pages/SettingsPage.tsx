import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type SecretStatus } from "../api";
import { useAuth } from "../auth";
import { ErrorNote, PageHeader, formatDate } from "../components/ui";

function SecretRow({ s }: { s: SecretStatus }) {
  const qc = useQueryClient();
  const [value, setValue] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["secrets"] });
  const save = useMutation({
    mutationFn: () => api(`/api/secrets/${s.kind}`, { method: "PUT", json: { value } }),
    onSuccess: () => {
      setValue("");
      refresh();
    },
  });
  const clear = useMutation({ mutationFn: () => api(`/api/secrets/${s.kind}`, { method: "DELETE" }), onSuccess: refresh });
  return (
    <tr>
      <td>{s.label}</td>
      <td>{s.is_set ? `Saved ${s.updated_at ? formatDate(s.updated_at) : ""}` : <span className="muted">Not set</span>}</td>
      <td>
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <input
            aria-label={s.label}
            type={s.kind.endsWith("username") ? "text" : "password"}
            autoComplete="off"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={s.is_set ? "Replace…" : "Enter…"}
          />
          <button type="submit" disabled={!value || save.isPending}>
            Save
          </button>
          {s.is_set && (
            <button type="button" className="danger" onClick={() => clear.mutate()}>
              Clear
            </button>
          )}
        </form>
        <ErrorNote error={save.error || clear.error} />
      </td>
    </tr>
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
    <form className="card stack narrow" onSubmit={submit}>
      <label htmlFor="cur">Current password</label>
      <input id="cur" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
      <label htmlFor="new">New password (10+ characters)</label>
      <input id="new" type="password" autoComplete="new-password" minLength={10} value={next} onChange={(e) => setNext(e.target.value)} required />
      <ErrorNote error={change.error} />
      <button className="primary" type="submit" disabled={change.isPending}>
        Change password
      </button>
    </form>
  );
}

export default function SettingsPage() {
  const secrets = useQuery({ queryKey: ["secrets"], queryFn: () => api<SecretStatus[]>("/api/secrets") });
  return (
    <div className="page">
      <PageHeader title="Settings" />
      <h2>Access tokens</h2>
      <p className="muted lede">
        Used to clone your repositories and, later, to push branches as you. Tokens are encrypted at rest and never
        shown again after saving.
      </p>
      <ErrorNote error={secrets.error} />
      <div className="table-wrap">
        <table>
          <tbody>{secrets.data?.map((s) => <SecretRow key={s.kind} s={s} />)}</tbody>
        </table>
      </div>
      <h2>Password</h2>
      <PasswordForm />
    </div>
  );
}
