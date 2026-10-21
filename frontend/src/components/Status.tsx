import { AlertTriangle, CheckCircle2, Clock, Hand, Loader2, MinusCircle } from "lucide-react";

/** The six visual states every entity status maps to (07 §6.1). */
export type VisualState = "waiting" | "working" | "needs" | "done" | "problem" | "off";

const STATE_OF: Record<string, VisualState> = {
  queued: "waiting",
  created: "waiting",
  pending: "waiting",
  running: "working",
  drafting: "working",
  preparing: "working",
  verifying: "working",
  pushing: "working",
  summarizing: "working",
  needs_decision: "needs",
  awaiting_review: "needs",
  paused: "needs",
  unreadable: "needs",
  unverified: "waiting",
  warning: "needs",
  ready: "done",
  succeeded: "done",
  ok: "done",
  complete: "done",
  approved: "done",
  pushed: "done",
  merged: "done",
  imported: "done",
  unchanged: "done",
  set: "done",
  active: "done",
  failed: "problem",
  invalid: "problem",
  timed_out: "problem",
  budget_exceeded: "problem",
  alert: "problem",
  partial: "problem",
  disabled: "off",
  cancelled: "off",
  discarded: "off",
  dismissed: "off",
  closed: "off",
  skipped: "off",
  not_set: "off",
  not_configured: "off",
};

/** Words shown next to the colour and icon (never colour alone). */
const LABELS: Record<string, string> = {
  queued: "Queued",
  running: "Running",
  ready: "Ready",
  failed: "Failed",
  ok: "OK",
  succeeded: "Succeeded",
  partial: "Partial",
  skipped: "Skipped",
  imported: "Imported",
  unchanged: "Unchanged",
  needs_decision: "Needs decision",
  awaiting_review: "Awaiting review",
  timed_out: "Timed out",
  budget_exceeded: "Budget reached",
  warning: "Warning",
  not_configured: "Not configured",
  not_set: "Not set",
  unreadable: "Can't be read",
  set: "Saved",
  unverified: "Not tested",
  invalid: "Rejected",
  active: "Active",
  disabled: "Disabled",
};

export function visualState(status: string): VisualState {
  return STATE_OF[status] ?? "waiting";
}

export function statusLabel(status: string): string {
  return LABELS[status] ?? status.charAt(0).toUpperCase() + status.slice(1).replaceAll("_", " ");
}

const ICONS = {
  waiting: Clock,
  working: Loader2,
  needs: Hand,
  done: CheckCircle2,
  problem: AlertTriangle,
  off: MinusCircle,
} as const;

export function StatusPill({ status, label }: { status: string; label?: string }) {
  const state = visualState(status);
  const Icon = ICONS[state];
  return (
    <span className={`pill st-${state}`} data-status={status} data-state={state}>
      <Icon size={12} aria-hidden className={state === "working" ? "spin" : undefined} />
      {label ?? statusLabel(status)}
    </span>
  );
}
