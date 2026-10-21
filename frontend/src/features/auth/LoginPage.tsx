import { useState, type FormEvent } from "react";
import { Navigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../../auth";
import { usePageTitle } from "../../app/pageTitle";
import { Button, Card, ErrorNote, TextField } from "../../components/ui";

/** Only same-app paths: "/x" but not "//host" or "/\\host", so `next` can't send anyone off-site, and never back
    to /login, which would redirect forever once signed in. */
export function safeNext(next: string | null): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\") || /^\/login(\/|\?|$)/.test(next))
    return "/chat";
  return next;
}

export default function LoginPage() {
  const { user, login } = useAuth();
  const [params] = useSearchParams();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  usePageTitle("Sign in");

  if (user) return <Navigate to={safeNext(params.get("next"))} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-wrap" id="main">
      <Card as="form" className="login stack" onSubmit={submit} aria-labelledby="login-title">
        <h1 id="login-title">MyWorkBuddy</h1>
        <p className="muted">Sign in with the account your admin created for you.</p>
        <TextField label="Username" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
        <TextField
          label="Password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        <ErrorNote error={error} />
        <Button variant="primary" type="submit" pending={busy}>
          Sign in
        </Button>
      </Card>
    </main>
  );
}
