import type { WorkItem, WorkItemState } from "../../api";

/** Board columns in order (07 §4.9). Dismissed tickets are behind a filter, not a column. */
export const COLUMNS: readonly (readonly [WorkItemState, string])[] = [
  ["new", "New"],
  ["planning", "Planning"],
  ["needs_decision", "Needs decision"],
  ["approved", "Approved"],
  ["in_progress", "In progress"],
  ["in_review", "In review"],
  ["done", "Done"],
];

export const COLUMN_LABEL: Record<string, string> = Object.fromEntries([...COLUMNS, ["dismissed", "Dismissed"]]);

/** The work item hub's progress strip (07 §4.10). */
export const STEPS = ["Ticket", "Plan", "Approved", "Job", "Review", "Merged"] as const;

const STEP_OF: Record<WorkItemState, number> = {
  new: 0,
  dismissed: 0,
  planning: 1,
  needs_decision: 1,
  approved: 2,
  in_progress: 3,
  in_review: 4,
  done: 5,
};

export function stepOf(state: WorkItemState): number {
  return STEP_OF[state];
}

/** "PROJ-123 · repo", the glossary's name for a ticket (07 §9). */
export function ticketName(item: Pick<WorkItem, "key" | "project">): string {
  return item.project ? `${item.key} · ${item.project.name}` : item.key;
}

/** The one next action for a ticket (07 §4.9 cards, §4.10 header). */
export type NextAction =
  | { kind: "draft"; label: "Draft plan" }
  | { kind: "start_job"; label: "Start job" }
  | { kind: "choose_repo"; label: "Choose repository" }
  | { kind: "link"; label: string; to: string }
  | { kind: "wait"; label: string }
  | { kind: "none" };

export function nextAction(item: WorkItem): NextAction {
  switch (item.state) {
    case "new":
      return item.project ? { kind: "draft", label: "Draft plan" } : { kind: "choose_repo", label: "Choose repository" };
    case "planning":
      return { kind: "wait", label: "Planner is working… usually 1–3 min" };
    case "needs_decision":
      return { kind: "link", label: "Review plan", to: `/work/${item.id}/plan` };
    case "approved":
      return { kind: "start_job", label: "Start job" };
    case "in_progress":
      return { kind: "link", label: "Open job", to: `/work/${item.id}/job` };
    case "in_review":
      return { kind: "link", label: "Review changes", to: `/work/${item.id}/job` };
    case "dismissed":
      return { kind: "none" };
    default:
      return { kind: "link", label: "Open", to: `/work/${item.id}` };
  }
}

/** Human names for validation acknowledgement keys (03 §4: `ci_config:<path>`, `optional_host:<host>`, `large_change`). */
export function ackLabel(key: string): string {
  const [kind, rest] = [key.split(":", 1)[0], key.slice(key.indexOf(":") + 1)];
  if (kind === "ci_config") return `Changes CI or deployment configuration (${rest})`;
  if (kind === "optional_host") return `Lets the sandbox reach ${rest}`;
  if (kind === "large_change") return "Large change: more than 30 files or estimated XL";
  if (key === "services") return "Verification runs its own Docker, with twice the CPU, memory and disk";
  if (kind === "protected_path") return `Changes a path the repository's conventions protect (${rest})`;
  return key;
}
