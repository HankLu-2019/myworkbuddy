// Shared fixtures for the Job tab tests (x6, f1, …): one work item, one job and a mocked API around them.
import { MEMBER, mockApi, signedInAs, sse } from "./test-utils";
import type { Job, JobResult, WorkItemDetail } from "./api";

export const NOW = new Date().toISOString();
export const USER = { ...MEMBER, timezone: "Europe/Stockholm", timezone_confirmed: true };
export const WORK_ID = "11111111-0000-4000-8000-000000000001";
export const PLAN_ID = "22222222-0000-4000-8000-000000000002";
export const JOB_ID = "44444444-0000-4000-8000-000000000004";
export const LOG_ID = "55555555-0000-4000-8000-000000000005";

export const PATCH = [
  "diff --git a/app/webhooks.py b/app/webhooks.py",
  "--- a/app/webhooks.py",
  "+++ b/app/webhooks.py",
  "@@ -1 +1 @@",
  "-RETRIES = 0",
  "+RETRIES = 3",
  "",
].join("\n");

export const RESULT: JobResult = {
  schema_version: 1,
  patch_artifact_id: "66666666-0000-4000-8000-000000000006",
  patch_sha256: "f".repeat(64),
  files: [
    { path: "app/webhooks.py", status: "modified", added: 1, removed: 1, binary: false },
    { path: "tests/test_webhooks.py", status: "added", added: 1, removed: 0, binary: false },
  ],
  checks: [
    { name: "no_changes", status: "pass", detail: "" },
    { name: "outside_plan", status: "warn", detail: "Files not in the plan: tests/test_webhooks.py." },
    { name: "secret_scan", status: "pass", detail: "" },
  ],
  verify: [{ command: "pytest -q", exit_code: 0, duration_s: 4.2, timed_out: false, log_artifact_id: LOG_ID }],
  agent_notes: "Added **three** retries.",
  attempts: 1,
  cost_usd: 0.42,
  failed_checks: false,
};

export const job = (over: Partial<Job> = {}): Job => ({
  id: JOB_ID, work_item_id: WORK_ID, key: "PAY-12", summary: "Retry webhook delivery on 5xx", status: "awaiting_review",
  failed_stage: null, attempt: 1, review_round: 0, max_attempts: 3, branch_name: "mwb/PAY-12-retry-webhooks",
  base_branch: "main", base_sha: "abc", cost_usd: 0.42, error_code: null, error: null, result: RESULT as unknown as Job["result"],
  push: null, created_at: NOW, started_at: NOW, finished_at: null, updated_at: NOW, ...over,
}); // prettier-ignore

export const detail = (over: Partial<WorkItemDetail> = {}): WorkItemDetail => ({
  id: WORK_ID, state: "in_review", key: "PAY-12", summary: "Retry webhook delivery on 5xx", jira_status: "In Progress",
  issue_type: "Story", priority: "High", labels: [], components: [], jira_url: "https://jira.example.com/browse/PAY-12",
  project: { id: 7, name: "payments" }, current_plan_id: PLAN_ID, plan_status: "approved", plan_error: null, cost_usd: 0.5,
  updated_remote: NOW, created_at: NOW, updated_at: NOW, description: "", links: [], assignee: null, ...over,
}); // prettier-ignore

export const EVENTS: [string, unknown][] = [
  ["status", { seq: 1, at: NOW, from: "created", to: "preparing" }],
  ["agent_tool", { seq: 2, at: NOW, tool: "Bash", summary: "cat .mwb/TASK.md", more: 2 }],
  ["agent_text", { seq: 3, at: NOW, text: "I'll change app/webhooks.py." }],
  ["verify_cmd", { seq: 4, at: NOW, command: "pytest -q", exit_code: 0, duration_s: 4.2, timed_out: false }],
  ["status", { seq: 5, at: NOW, from: "verifying", to: "awaiting_review" }],
  ["end", { status: "awaiting_review" }],
];

export function hub(j: Job | null, item = detail(), extra: Record<string, unknown> = {}) {
  return mockApi({
    ...signedInAs(USER),
    [`GET /api/work-items/${WORK_ID}`]: item,
    "GET /api/jobs": { items: j ? [j] : [], next_cursor: null },
    [`GET /api/jobs/${JOB_ID}`]: j,
    [`GET /api/jobs/${JOB_ID}/events`]: () => sse(EVENTS),
    [`GET /api/jobs/${JOB_ID}/patch`]: () => new Response(PATCH),
    [`GET /api/artifacts/${LOG_ID}`]: () => new Response(Array.from({ length: 250 }, (_, i) => `line ${i + 1}`).join("\n")),
    "GET /api/projects": [],
    ...extra,
  });
}
