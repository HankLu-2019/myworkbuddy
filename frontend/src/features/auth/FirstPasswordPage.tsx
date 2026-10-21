import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../api";
import { useAuth } from "../../auth";
import { usePageTitle } from "../../app/pageTitle";
import { Button, Card, ErrorNote, TextField } from "../../components/ui";

/** Shown instead of the app while the user still has the one-time password an admin gave them (07-ux §5). */
export default function FirstPasswordPage() {
  const { user, logout, passwordChanged } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  usePageTitle("Choose your password");
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
    <main className="login-wrap" id="main">
      <Card as="form" className="login stack" onSubmit={submit} aria-labelledby="first-password-title">
        <h1 id="first-password-title">Choose your password</h1>
        <p className="muted">
          Welcome, {user?.display_name || user?.username}. Your admin gave you a one-time password; choose your own
          to continue.
        </p>
        <TextField
          label="One-time password"
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
        />
        <TextField
          label="Repeat new password"
          type="password"
          autoComplete="new-password"
          value={repeat}
          onChange={(e) => setRepeat(e.target.value)}
          error={mismatch ? "The passwords don't match." : null}
          required
        />
        <ErrorNote error={change.error} />
        <Button variant="primary" type="submit" pending={change.isPending} disabled={mismatch}>
          Save and continue
        </Button>
        <Button variant="link" onClick={logout}>
          Sign out
        </Button>
      </Card>
    </main>
  );
}
