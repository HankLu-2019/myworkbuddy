import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import WorkItemPage from "./features/work/WorkItemPage";
import { MEMBER, mockApi, renderAt, signedInAs, sse } from "./test-utils";
import type { Job, JobResult, PushPayload, WorkItemDetail } from "./api";

afterEach(() => vi.unstubAllGlobals());

const NOW = new Date().toISOString();
const USER = { ...MEMBER, timezone: "Europe/Stockholm", timezone_confirmed: true };
const WORK_ID = "11111111-0000-4000-8000-000000000001";
const PLAN_ID = "22222222-0000-4000-8000-000000000002";
const JOB_ID = "44444444-0000-4000-8000-000000000004";
const LOG_ID = "55555555-0000-4000-8000-000000000005";

const PATCH = [
  "diff --git a/app/webhooks.py b/app/webhooks.py",
  "--- a/app/webhooks.py",
  "+++ b/app/webhooks.py",
  "@@ -1 +1 @@",
  "-RETRIES = 0",
  "+RETRIES = 3",
  "",
].join("\n");

const RESULT: JobResult = {
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

const job = (over: Partial<Job> = {}): Job => ({
  id: JOB_ID, work_item_id: WORK_ID, key: "PAY-12", summary: "Retry webhook delivery on 5xx", status: "awaiting_review",
  failed_stage: null, attempt: 1, review_round: 0, max_attempts: 3, branch_name: "mwb/PAY-12-retry-webhooks",
  base_branch: "main", base_sha: "abc", cost_usd: 0.42, error_code: null, error: null, result: RESULT as unknown as Job["result"],
  push: null, created_at: NOW, started_at: NOW, finished_at: null, updated_at: NOW, ...over,
}); // prettier-ignore

const detail = (over: Partial<WorkItemDetail> = {}): WorkItemDetail => ({
  id: WORK_ID, state: "in_review", key: "PAY-12", summary: "Retry webhook delivery on 5xx", jira_status: "In Progress",
  issue_type: "Story", priority: "High", labels: [], components: [], jira_url: "https://jira.example.com/browse/PAY-12",
  project: { id: 7, name: "payments" }, current_plan_id: PLAN_ID, plan_status: "approved", plan_error: null, cost_usd: 0.5,
  updated_remote: NOW, created_at: NOW, updated_at: NOW, description: "", links: [], assignee: null, ...over,
}); // prettier-ignore

const EVENTS: [string, unknown][] = [
  ["status", { seq: 1, at: NOW, from: "created", to: "preparing" }],
  ["agent_tool", { seq: 2, at: NOW, tool: "Bash", summary: "cat .mwb/TASK.md", more: 2 }],
  ["agent_text", { seq: 3, at: NOW, text: "I'll change app/webhooks.py." }],
  ["verify_cmd", { seq: 4, at: NOW, command: "pytest -q", exit_code: 0, duration_s: 4.2, timed_out: false }],
  ["status", { seq: 5, at: NOW, from: "verifying", to: "awaiting_review" }],
  ["end", { status: "awaiting_review" }],
];

function hub(j: Job | null, item = detail(), extra: Record<string, unknown> = {}) {
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

const PREVIEW: PushPayload = {
  target: { host: "gitlab", repo_url: "https://git.example.com/team/payments.git", branch: "mwb/PAY-12-retry-webhooks", base_branch: "main" },
  commit: { author_name: "Alice", author_email: "a@x", message: `PAY-12 Retry webhooks\n\nMWB-Job: ${JOB_ID}` },
  merge_request: { title: "Draft: PAY-12: Retry webhooks", description: "Retry on 5xx." },
  gerrit: null,
  jira_comment: { enabled: true, body: "Merge request opened: <link placeholder>" },
  patch_sha256: "f".repeat(64),
  acknowledgements: [],
};

describe("Push review", () => {
  it("shows the exact payload, lets the texts be edited and approves it", async () => {
    const calls = hub(job(), detail(), {
      [`GET /api/jobs/${JOB_ID}/push-preview`]: PREVIEW,
      [`POST /api/jobs/${JOB_ID}/approve-push`]: job({ status: "approved_push" }),
    });
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    fireEvent.click(await screen.findByRole("button", { name: "Review push" }));
    const dialog = await screen.findByRole("dialog", { name: "Review push" });
    expect(await within(dialog).findByText("mwb/PAY-12-retry-webhooks → main")).toBeInTheDocument();
    fireEvent.change(within(dialog).getByLabelText("Merge request title"), { target: { value: "Draft: PAY-12: Retry (edited)" } });
    fireEvent.click(within(dialog).getByLabelText("Comment on the Jira ticket"));
    expect(within(dialog).queryByLabelText("Jira comment")).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Push branch and open merge request" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.path).toBe(`/api/jobs/${JOB_ID}/approve-push`));
    const sent = calls.find((c) => c.method === "POST")!.body as PushPayload;
    expect(sent.merge_request?.title).toBe("Draft: PAY-12: Retry (edited)");
    expect(sent.jira_comment).toEqual({ enabled: false, body: "Merge request opened: <link placeholder>" });
    expect(sent.target).toEqual(PREVIEW.target);
  });

  it("needs the acknowledgement when checks failed, and names the Gerrit action", async () => {
    const failed = { ...RESULT, failed_checks: true };
    const gerrit: PushPayload = {
      ...PREVIEW,
      target: { ...PREVIEW.target, host: "gerrit" },
      merge_request: null,
      gerrit: { topic: "mwb-PAY-12" },
      acknowledgements: [],
    };
    hub(job({ result: failed as unknown as Job["result"] }), detail(), { [`GET /api/jobs/${JOB_ID}/push-preview`]: gerrit });
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    expect(await screen.findByText(/Some checks failed/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Review push" }));
    const dialog = await screen.findByRole("dialog", { name: "Review push" });
    const upload = await within(dialog).findByRole("button", { name: "Upload change to Gerrit" });
    expect(upload).toBeDisabled();
    expect(within(dialog).getByText(/Keep the Change-Id line/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByLabelText("Push despite failing checks"));
    expect(upload).toBeEnabled();
  });

  it("after the push: links, watching for merge, and changes can still be requested", async () => {
    const push = { state: "done", mr_url: "https://git.example.com/team/payments/-/merge_requests/7", jira_comment_url: "https://jira.example.com/browse/PAY-12?focusedCommentId=1" };
    hub(job({ status: "pushed", push: push as unknown as Job["push"] }));
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    expect(await screen.findByRole("link", { name: "Merge request" })).toHaveAttribute("href", push.mr_url);
    expect(screen.getByRole("link", { name: "Jira comment" })).toHaveAttribute("href", push.jira_comment_url);
    expect(screen.getByText("Watching for merge.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Review push" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Discard" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("What should change?")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  it("a failed push offers Retry push", async () => {
    const push = { state: "branch_pushed", error: { code: "credential_invalid", message: "Gitlab rejected your credential.", at: NOW } };
    const calls = hub(job({ push: push as unknown as Job["push"] }), detail(), {
      [`POST /api/jobs/${JOB_ID}/retry-push`]: job({ status: "approved_push" }),
    });
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    expect(await screen.findByText("Push failed: Gitlab rejected your credential.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Update your token first" })).toHaveAttribute("href", "/settings/integrations");
    fireEvent.click(screen.getByRole("button", { name: "Retry push" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === `/api/jobs/${JOB_ID}/retry-push`)).toBe(true));
  });
});
