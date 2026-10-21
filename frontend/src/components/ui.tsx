import type { ReactNode } from "react";
import type { Visibility } from "../api";

export { Markdown } from "./markdown";

export function VisibilityBadge({ visibility }: { visibility: Visibility }) {
  return visibility === "public" ? (
    <span className="badge badge-public" title="Visible to the whole team">Team</span>
  ) : (
    <span className="badge badge-private" title="Only you can see this">Private</span>
  );
}

export function StatusPill({ status }: { status: string }) {
  return <span className={`pill pill-${status}`}>{status}</span>;
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="error-note">{error instanceof Error ? error.message : String(error)}</div>;
}

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <header className="page-header">
      <h1>{title}</h1>
      {children && <div className="page-actions">{children}</div>}
    </header>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export const SOURCE_LABELS: Record<string, string> = {
  wiki: "Wiki",
  episode: "Session",
  code: "Code",
  repo_doc: "Repo doc",
  upload: "Document",
};

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
