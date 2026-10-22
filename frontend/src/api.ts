import type { components } from "./api-schema";

let csrfToken = "";

export function setCsrfToken(token: string) {
  csrfToken = token;
}

/** Error codes from the API (backend app/errors.py, rule R12). */
export type ErrorCode =
  | "not_signed_in"
  | "invalid_credentials"
  | "csrf_invalid"
  | "rate_limited"
  | "forbidden"
  | "not_found"
  | "conflict"
  | "bad_request"
  | "wiki_conflict"
  | "session_busy"
  | "plan_changed"
  | "budget_exceeded"
  | "llm_unavailable"
  | "unsupported_file"
  | "file_too_large"
  | "disk_full"
  | "credential_invalid"
  | "password_change_required"
  | "validation_error"
  | "internal";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code: ErrorCode | string = `http_${status}`,
    public body: unknown = null,
    /** X-Request-ID of the failed request, shown as "ref" so an admin can find it in the logs. */
    public requestId: string | null = null,
    /** Seconds to wait, from Retry-After (rate_limited). */
    public retryAfter: number | null = null,
  ) {
    super(message);
  }
}

type Options = { method?: string; json?: unknown; form?: FormData; signal?: AbortSignal };

function errorMessage(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((d) => d?.msg ?? String(d)).join("; ");
  }
  return `Request failed (${status})`;
}

async function send(path: string, { method = "GET", json, form, signal }: Options = {}): Promise<Response> {
  const headers: Record<string, string> = {};
  if (json !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const res = await fetch(path, {
    method,
    headers,
    body: form ?? (json !== undefined ? JSON.stringify(json) : undefined),
    credentials: "same-origin",
    signal,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const code = body && typeof body === "object" && "code" in body ? String((body as { code: unknown }).code) : undefined;
    const retry = Number(res.headers.get("Retry-After"));
    throw new ApiError(
      res.status,
      errorMessage(body, res.status),
      code,
      body,
      res.headers.get("X-Request-ID"),
      Number.isFinite(retry) && retry > 0 ? retry : null,
    );
  }
  return res;
}

export async function api<T>(path: string, opts: Options = {}): Promise<T> {
  const res = await send(path, opts);
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

/** GET a text body (patches, logs); errors as `api`. */
export async function apiText(path: string): Promise<string> {
  return (await send(path)).text();
}

export type StreamEvent = { event: string; data: any };

/** POST and read a server-sent-event response, calling onEvent for each event. */
export async function streamPost(path: string, json: unknown, onEvent: (e: StreamEvent) => void, signal?: AbortSignal) {
  const res = await send(path, { method: "POST", json, signal });
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      // Lines starting with ":" are SSE comments: the server's keep-alive pings during slow model calls.
      if (block.split("\n").every((line) => line.startsWith(":") || line === "")) continue;
      onEvent(parseEvent(block));
    }
  }
}

export function parseEvent(block: string): StreamEvent {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  const raw = data.join("\n");
  try {
    return { event, data: JSON.parse(raw) };
  } catch {
    return { event, data: raw };
  }
}

// ---- types mirrored from the API ----

/** Response and request shapes come from the backend's OpenAPI schema (src/api-schema.ts, `npm run api:types`). */
type Schemas = components["schemas"];

export type Visibility = Schemas["VisibilityIn"]["visibility"];
export type User = Schemas["UserOut"];
export type Usage = Schemas["UsageOut"];
/** Returned once when an admin creates a user or resets a password. */
export type UserWithPassword = Schemas["UserWithPassword"];
export type SignIn = Schemas["app__auth__routes__SessionOut"];
export type OffboardingPreview = Schemas["OffboardingPreview"];
export type Session = Schemas["app__chat__routes__SessionOut"];
export type Hit = Schemas["HitOut"];
export type DocumentItem = Schemas["DocumentOut"];
export type PageSummary = Schemas["PageSummary"];
export type Page = Schemas["PageOut"];
export type Commit = Schemas["CommitOut"];
export type Project = Schemas["ProjectOut"];
export type Integration = Schemas["IntegrationOut"];
export type Credential = Schemas["CredentialOut"];
export type AdminIntegration = Schemas["AdminIntegrationOut"];
export type IntegrationKind = AdminIntegration["kind"];
export type CredentialTest = Schemas["TestOut"];
export type ImportResult = Schemas["ImportResult"];
export type LibrarianRun = Schemas["LibrarianRunOut"];
export type LibrarianChange = Schemas["LibrarianChangeOut"];
export type ProjectDetail = Schemas["ProjectDetail"];
export type Home = Schemas["HomeOut"];
export type NeedsYou = Schemas["NeedsYou"];
export type ActivityItem = Schemas["ActivityItem"];
export type Activity = Schemas["ActivityOut"];
export type Checklist = Schemas["Checklist"];
export type TodayBrief = Schemas["TodayBrief"];

// Phase 2: schedules, runs, inbox (02 §7).
export type Agent = Schemas["AgentOut"];
export type Schedule = Schemas["ScheduleOut"];
export type ScheduleIn = Schemas["ScheduleIn"];
export type SchedulePage = Schemas["SchedulePage"];
export type CronPreview = Schemas["PreviewOut"];
export type RunSummary = Schemas["RunOut"];
export type RunDetail = Schemas["RunDetail"];
export type RunPage = Schemas["RunPage"];
export type Proposal = Schemas["ProposalOut"];
export type InboxItem = Schemas["InboxItemOut"];
export type InboxPage = Schemas["InboxPage"];
export type NotifyMode = Schedule["notify_mode"];

// Phase 3: work items and plans (03 §8).
export type WorkItem = Schemas["WorkItemOut"];
export type WorkItemDetail = Schemas["WorkItemDetail"];
export type WorkItemPage = Schemas["WorkItemPage"];
export type WorkItemState = WorkItem["state"];
export type Plan = Schemas["PlanOut"];
export type PlanStatus = Plan["status"];
export type Revision = Schemas["RevisionOut"];
export type RevisionSummary = Schemas["RevisionSummary"];
export type RevisionPage = Schemas["RevisionPage"];
export type PlanDoc = Schemas["PlanDoc"];
export type FieldDiff = Schemas["FieldDiff"];
export type PlanDiff = Schemas["DiffOut"];

// Phase 4: jobs (04 §8).
export type Job = Schemas["JobOut"];
export type JobPage = Schemas["JobPage"];
export type JobStatus = Job["status"];

export type PushPayload = Schemas["PushPayload"];

/** `JobOut.push` (04 §6): the approved payload and what each push step left behind. */
export interface JobPush {
  state?: "approved" | "branch_pushed" | "review_requested" | "done";
  pushed_sha?: string;
  mr_url?: string;
  change_url?: string;
  jira_comment_url?: string;
  /** Set when a push failed and the job went back to review (08 §F5). */
  error?: { code: string; message: string; at: string };
}

/** `JobOut.result` (04 §5.4 JobResult), stored as JSON. */
export interface JobResult {
  schema_version: 1;
  patch_artifact_id: string;
  patch_sha256: string;
  files: { path: string; status: "added" | "modified" | "deleted" | "renamed"; added: number; removed: number; binary: boolean }[];
  checks: { name: string; status: "pass" | "warn" | "fail"; detail: string }[];
  verify: { command: string; exit_code: number | null; duration_s: number; timed_out: boolean; log_artifact_id: string }[];
  agent_notes: string;
  attempts: number;
  cost_usd: number;
  failed_checks: boolean;
}

/** One `job_events` row as the SSE stream sends it. */
export interface JobEvent {
  seq: number;
  at: string;
  type: "status" | "agent_text" | "agent_tool" | "verify_cmd" | "check" | "push" | "error";
  [key: string]: unknown;
}

export type PlanValidation = Schemas["ValidationResult"];
export type PlanDecision = Schemas["DecisionOut"];

/** A finding in a run report as stored (02 §3): the model's fields plus the server's `verified`. */
export interface Finding {
  fingerprint: string;
  severity: "info" | "warning" | "alert";
  title: string;
  detail: string;
  links: string[];
  verified?: boolean;
}

export interface Report {
  status: "ok" | "warning" | "alert";
  headline: string;
  findings: Finding[];
  summary_md: string;
}

/** A proposal card as the chat stream and message history carry it (02 §6). */
export interface ProposalCard {
  proposal_id: string;
  name: string;
  agent: string;
  agent_name?: string;
  cron: string;
  timezone: string;
  prompt?: string;
  inputs: Record<string, unknown>;
  notify_mode: NotifyMode;
  description?: string;
  next_runs?: string[];
  status?: "pending" | "accepted" | "dismissed" | "expired";
  schedule_id?: string | null;
  expires_at: string;
}

/** Citations and tool calls are free-form JSON in the schema; this is what the chat stream and history carry. */
export interface Citation {
  n: number;
  document_id: number;
  title: string;
  source_type: Hit["source_type"];
  source_ref: string;
  visibility: Visibility;
  meta: Record<string, unknown>;
}

export type ToolActivity = { name: string; args: Record<string, unknown>; status?: string };

export type ChatMessage = Omit<Schemas["MessageOut"], "citations" | "tools" | "proposals"> & {
  citations: Citation[];
  tools: ToolActivity[];
  proposals?: ProposalCard[];
};
