import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorNote } from "../components/ui";

/** Shown instead of the app while the user still has the one-time password an admin gave them (07-ux §5). */
export default function FirstPasswordPage() {
  const { user, logout, passwordChanged } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const change = useMutation({
    mutationFn: () => api("/api/auth/password", { method: "POST", json: { current_password: current, new_password: next } }),
    onSuccess: passwordChanged,
  });
  const mismatch = repeat.length > 0 && repeat !== next;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!mismatch) change.mutate();
  }

  return (
    <div className="login-wrap">
      <form className="login card" onSubmit={submit} aria-labelledby="first-password-title">
        <h1 id="first-password-title">Choose your password</h1>
        <p className="muted">
          Welcome, {user?.display_name || user?.username}. Your admin gave you a one-time password; choose your own
          to continue.
        </p>
        <label htmlFor="fp-current">One-time password</label>
        <input id="fp-current" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
        <label htmlFor="fp-new">New password (10+ characters)</label>
        <input id="fp-new" type="password" autoComplete="new-password" minLength={10} value={next} onChange={(e) => setNext(e.target.value)} required />
        <label htmlFor="fp-repeat">Repeat new password</label>
        <input
          id="fp-repeat"
          type="password"
          autoComplete="new-password"
          value={repeat}
          onChange={(e) => setRepeat(e.target.value)}
          aria-invalid={mismatch}
          aria-describedby={mismatch ? "fp-mismatch" : undefined}
          required
        />
        {mismatch && (
          <div id="fp-mismatch" className="error-text small">
            The passwords don't match.
          </div>
        )}
        <ErrorNote error={change.error} />
        <button className="primary" type="submit" disabled={change.isPending || mismatch}>
          Save and continue
        </button>
        <button type="button" className="link" onClick={logout}>
          Sign out
        </button>
      </form>
    </div>
  );
}
