import type { ReactNode } from "react";
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

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div className="error-note" role="alert">
      {error instanceof Error ? error.message : String(error)}
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

export function SkeletonRows({ rows = 3 }: { rows?: number }) {
  return (
    <div className="stack" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} height={18} width={`${90 - i * 12}%`} />
      ))}
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
