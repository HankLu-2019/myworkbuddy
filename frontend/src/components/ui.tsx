/** Shared components (07-ux §8). Pages import from here; each lives in its own module. */
import type { ReactNode } from "react";

export { Markdown } from "./markdown";
export { Button, FileButton, IconButton, buttonClass } from "./Button";
export { Checkbox, RadioGroup, Select, TextArea, TextField, Toggle } from "./Field";
export { ConfirmDialog, Dialog, Drawer, Menu, Popover, Tooltip, TooltipProvider } from "./Overlay";
export { ToastProvider, useToast } from "./Toast";
export { LinkTabs, Tabs } from "./Tabs";
export { StatusPill, statusLabel, visualState } from "./Status";
export { VisibilityBadge, VisibilityControl } from "./Visibility";
export { Banner, CountBadge, Dot, EmptyState, ErrorNote, Skeleton, SkeletonRows } from "./Feedback";
export { Card, CodeBlock, DataTable, DiffView, KeyValue, type Column } from "./Data";

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <header className="page-header">
      <h1>{title}</h1>
      {children && <div className="page-actions">{children}</div>}
    </header>
  );
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

/** USD with two decimals, four below $0.01 (07 §6.10). */
export function formatUsd(n: number): string {
  return n > 0 && n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}
