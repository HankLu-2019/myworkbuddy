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
