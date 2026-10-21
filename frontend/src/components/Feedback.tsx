import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { describeError } from "../app/errors";
import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react";

export type BannerTone = "info" | "warning" | "danger" | "success";

const BANNER_ICON = { info: Info, warning: AlertTriangle, danger: XCircle, success: CheckCircle2 } as const;

/** Page-level state (07 §6.5): read-only item, budget reached, session expired, results of an action. */
export function Banner({ tone = "info", children, role }: { tone?: BannerTone; children: ReactNode; role?: "status" | "alert" | "note" }) {
  const Icon = BANNER_ICON[tone];
  return (
    <div className={`banner banner-${tone}`} role={role ?? (tone === "danger" ? "alert" : "status")}>
      <Icon size={16} aria-hidden />
      <div className="grow">{children}</div>
    </div>
  );
}

/** An error with the same wording and fix button wherever it appears (07 §5.10, §6.8). */
export function ErrorNote({ error }: { error: unknown }) {
  const [copied, setCopied] = useState(false);
  if (!error) return null;
  const view = describeError(error);
  return (
    <div className="error-note" role="alert">
      <span>{view.message}</span>
      {view.ref && (
        <>
          {" "}
          <span className="mono small">(ref {view.ref})</span>{" "}
          <button
            type="button"
            className="btn btn-link btn-sm"
            onClick={() => void navigator.clipboard?.writeText(view.ref!).then(() => setCopied(true))}
          >
            <span className="btn-label">{copied ? "Copied" : "Copy ref"}</span>
          </button>
        </>
      )}
      {view.action && (
        <>
          {" "}
          <Link to={view.action.href} className="error-action">
            {view.action.label}
          </Link>
        </>
      )}
    </div>
  );
}

/** One sentence and, where useful, one primary action (07 §6.9). */
export function EmptyState({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div>{children}</div>
      {action}
    </div>
  );
}

export function Skeleton({ width = "100%", height = 16, label }: { width?: number | string; height?: number; label?: string }) {
  return <span className="skeleton" style={{ width, height }} role={label ? "status" : undefined} aria-label={label} />;
}

/** Elapsed milliseconds since mount, ticking every `step` ms. */
function useElapsed(step: number): number {
  const [start] = useState(() => Date.now());
  const [now, setNow] = useState(start);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), step);
    return () => clearInterval(t);
  }, [step]);
  return now - start;
}

export const SKELETON_DELAY_MS = 300;
export const LONG_WAIT_MS = 10_000;

/** Loading rows: nothing for fast responses, skeletons after 300 ms, an explanation after 10 s (07 §6.9). */
export function SkeletonRows({ rows = 3, longWait = "Still working — this can take a minute." }: { rows?: number; longWait?: string }) {
  const elapsed = useElapsed(SKELETON_DELAY_MS);
  if (elapsed < SKELETON_DELAY_MS) return <div role="status" aria-label="Loading" />;
  return (
    <div className="stack" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} height={18} width={`${90 - i * 12}%`} />
      ))}
      {elapsed >= LONG_WAIT_MS && <p className="muted small">{longWait}</p>}
    </div>
  );
}

export function CountBadge({ count, label }: { count: number; label: string }) {
  if (count <= 0) return null;
  return (
    <span className="count-badge" aria-label={`${count} ${label}`}>
      {count > 99 ? "99+" : count}
    </span>
  );
}

/** A small attention dot with a text alternative. */
export function Dot({ label }: { label: string }) {
  return <span className="dot" role="img" aria-label={label} title={label} />;
}
