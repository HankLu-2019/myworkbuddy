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
    throw new ApiError(res.status, errorMessage(body, res.status), code, body);
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

export type Visibility = "private" | "public";

export interface User {
  id: number;
  username: string;
  email: string | null;
  display_name: string | null;
  role: "admin" | "member";
  is_active: boolean;
}

export interface Session {
  id: string;
  title: string;
  source: string;
  visibility: Visibility;
  project_id: number | null;
  owner_id: number;
  created_at: string;
  updated_at: string;
}

export interface Citation {
  n: number;
  document_id: number;
  title: string;
  source_type: string;
  source_ref: string;
  visibility: Visibility;
  meta: Record<string, unknown>;
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  citations: Citation[];
  tools: { name: string; args: Record<string, unknown>; status: string }[];
}

export interface Hit {
  document_id: number;
  title: string;
  source_type: string;
  source_ref: string;
  visibility: Visibility;
  owner_id: number;
  project_id: number | null;
  excerpt: string;
  meta: Record<string, unknown>;
  score: number;
}

export interface DocumentItem {
  id: number;
  title: string;
  source_type: string;
  source_ref: string;
  visibility: Visibility;
  owner_id: number;
  project_id: number | null;
  chunks: number;
  updated_at: string;
  mine: boolean;
}

export interface PageSummary {
  id: number;
  path: string;
  title: string;
  visibility: Visibility;
  owner_id: number;
  updated_at: string;
  mine: boolean;
}

export interface Page extends PageSummary {
  content: string;
  head_sha: string | null;
  can_edit: boolean;
}

export interface Commit {
  sha: string;
  author: string;
  date: string;
  message: string;
}

export interface Project {
  id: number;
  name: string;
  repo_url: string;
  host: "gitlab" | "gerrit";
  branch: string | null;
  visibility: Visibility;
  status: string;
  error: string | null;
  last_ingested_sha: string | null;
  owner_id: number;
  updated_at: string;
  mine: boolean;
}

export interface SecretStatus {
  kind: string;
  label: string;
  is_set: boolean;
  updated_at: string | null;
}

export interface ImportResult {
  filename: string;
  session_id: string | null;
  title: string | null;
  status: "imported" | "unchanged" | "failed";
  error: string | null;
}
