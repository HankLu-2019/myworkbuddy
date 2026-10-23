import { useState, type FormEvent } from "react";
import { Navigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type OidcConfig } from "../../api";
import { useAuth } from "../../auth";
import { usePageTitle } from "../../app/pageTitle";
import { Banner, Button, Card, ErrorNote, TextField, buttonClass } from "../../components/ui";

/** Why single sign-on sent the user back here (08 §F11: `?sso_error=` from the callback). */
export const SSO_ERRORS: Record<string, string> = {
  no_account: "There's no MyWorkBuddy account for you yet. Ask an admin to add you.",
  inactive: "Your account is deactivated. Ask an admin to reactivate it.",
  ambiguous: "More than one account has your email address. Ask an admin to sort it out.",
  denied: "Sign-in was cancelled at the identity provider.",
  expired: "The sign-in took too long or was started in another browser. Try again.",
  provider_unreachable: "The identity provider isn't reachable right now. Try again in a minute.",
  admin_link:
    "That email address belongs to an admin account, which isn't linked by email. Sign in with your password, then link single sign-on in Settings → Profile.",
  taken: "That single sign-on identity is already linked to another account.",
};
const ssoError = (code: string) => SSO_ERRORS[code] ?? "Single sign-on didn't work. Try again, or ask an admin if it keeps happening.";

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
  const sso = useQuery({ queryKey: ["oidc-config"], queryFn: () => api<OidcConfig>("/api/auth/oidc/config"), retry: false });
  const sso_error = params.get("sso_error");

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
        {params.get("reason") === "expired" ? (
          <Banner tone="info">Your session expired. Sign in to continue where you left off; unsaved text is kept.</Banner>
        ) : (
          <p className="muted">Sign in with the account your admin created for you.</p>
        )}
        {sso_error && <Banner tone="danger">{ssoError(sso_error)}</Banner>}
        {sso.data?.enabled && (
          <>
            <a className={buttonClass("primary")} href={`/api/auth/oidc/start?next=${encodeURIComponent(safeNext(params.get("next")))}`}>
              Sign in with {sso.data.name}
            </a>
            <p className="muted small">{sso.data.local_login === "admins" ? "Admins can also sign in with a password:" : "Or with your username and password:"}</p>
          </>
        )}
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
        <Button variant={sso.data?.enabled ? "secondary" : "primary"} type="submit" pending={busy}>
          Sign in
        </Button>
      </Card>
    </main>
  );
}
