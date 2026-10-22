import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import WorkItemPage from "./features/work/WorkItemPage";
import JobsPage from "./features/work/JobsPage";
import { PatchView, parsePatch, splitRows } from "./components/Patch";
import { nextAction } from "./features/work/states";
import { MEMBER, mockApi, renderAt, signedInAs, sse } from "./test-utils";
import type { Job, JobResult, Plan, WorkItemDetail } from "./api";

afterEach(() => vi.unstubAllGlobals());

const NOW = new Date().toISOString();
const USER = { ...MEMBER, timezone: "Europe/Stockholm", timezone_confirmed: true };
const WORK_ID = "11111111-0000-4000-8000-000000000001";
const PLAN_ID = "22222222-0000-4000-8000-000000000002";
const REV_ID = "33333333-0000-4000-8000-000000000003";
const JOB_ID = "44444444-0000-4000-8000-000000000004";
const LOG_ID = "55555555-0000-4000-8000-000000000005";

const PATCH = [
  "diff --git a/app/webhooks.py b/app/webhooks.py",
  "index 1111111..2222222 100644",
  "--- a/app/webhooks.py",
  "+++ b/app/webhooks.py",
  "@@ -1,3 +1,3 @@",
  " import httpx",
  "-RETRIES = 0",
  "+RETRIES = 3",
  " def send(): ...",
  "diff --git a/tests/test_webhooks.py b/tests/test_webhooks.py",
  "new file mode 100644",
  "--- /dev/null",
  "+++ b/tests/test_webhooks.py",
  "@@ -0,0 +1 @@",
  "+def test_retries(): ...",
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
  push: null, question: null, questions: 0, created_at: NOW, started_at: NOW, finished_at: null, updated_at: NOW, ...over,
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

describe("PatchView", () => {
  it("parses files and pairs removed and added lines side by side", () => {
    const files = parsePatch(PATCH);
    expect(files.map((f) => f.path)).toEqual(["app/webhooks.py", "tests/test_webhooks.py"]);
    const rows = splitRows(files[0].lines);
    expect(rows.map((r) => r.kind)).toEqual(["hunk", "same", "change", "same"]);
    expect(rows[2]).toMatchObject({ left: "RETRIES = 0", right: "RETRIES = 3" });
  });

  it("switches between side by side and unified", () => {
    render(<PatchView patch={PATCH} />);
    // jsdom has no matchMedia: narrow screen, unified by default.
    expect(screen.getByRole("button", { name: "Unified" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Changes in app/webhooks.py")).toHaveTextContent("+RETRIES = 3");
    fireEvent.click(screen.getByRole("button", { name: "Side by side" }));
    const table = screen.getByRole("table", { name: "Changes in app/webhooks.py, side by side" });
    expect(within(table).getByRole("columnheader", { name: "Before" })).toBeInTheDocument();
    expect(within(table).getByText("- RETRIES = 0")).toBeInTheDocument();
    expect(within(table).getByText("+ RETRIES = 3")).toBeInTheDocument();
  });
});

describe("Job tab", () => {
  it("shows the review: timeline, files and diff, checks, logs, agent notes and cost", async () => {
    const calls = hub(job());
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    expect(await screen.findByRole("heading", { name: /Files changed/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Job" })).toHaveAttribute("aria-current", "page");
    // timeline from the event stream
    const timeline = await screen.findByRole("list", { name: "Job timeline" });
    expect(within(timeline).getByText("cat .mwb/TASK.md", { exact: false })).toBeInTheDocument();
    expect(within(timeline).getByText("I'll change app/webhooks.py.")).toBeInTheDocument();
    // diff
    expect(await screen.findByLabelText("Changes in tests/test_webhooks.py")).toHaveTextContent("+def test_retries");
    // checks
    const checks = screen.getByRole("list", { name: "Checks" });
    expect(within(checks).getByText("Stays within the plan")).toBeInTheDocument();
    expect(within(checks).getByText("Warning")).toBeInTheDocument();
    // agent notes: labelled, safe Markdown
    expect(screen.getByRole("heading", { name: /Agent notes\s*Written by the agent/ })).toBeInTheDocument();
    expect(screen.getByText("three").tagName).toBe("STRONG");
    expect(screen.getAllByText("$0.42").length).toBeGreaterThan(0);
    // logs: collapsed, fetched when opened, tail shown
    expect(calls.some((c) => c.path === `/api/artifacts/${LOG_ID}`)).toBe(false);
    const summary = screen.getByText("pytest -q", { selector: ".mono" });
    const details = summary.closest("details")!;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    expect(await screen.findByText("Last 200 lines of 250.")).toBeInTheDocument();
    expect(screen.getByText(/line 250/)).toBeInTheDocument();
  });

  it("sends review feedback with Request changes", async () => {
    const calls = hub(job(), detail(), { [`POST /api/jobs/${JOB_ID}/request-changes`]: job({ status: "running", review_round: 1, result: null }) });
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    const box = await screen.findByLabelText("What should change?");
    expect(screen.getByRole("button", { name: "Request changes" })).toBeDisabled();
    fireEvent.change(box, { target: { value: "Also log each retry." } });
    fireEvent.click(screen.getByRole("button", { name: "Request changes" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ feedback: "Also log each retry." }));
    expect(calls.find((c) => c.method === "POST")?.path).toBe(`/api/jobs/${JOB_ID}/request-changes`);
  });

  it("discards after a confirmation", async () => {
    const calls = hub(job(), detail(), { [`POST /api/jobs/${JOB_ID}/discard`]: job({ status: "discarded" }) });
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    fireEvent.click(await screen.findByRole("button", { name: "Discard" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("back to Approved");
    fireEvent.click(within(dialog).getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === `/api/jobs/${JOB_ID}/discard`)).toBe(true));
  });

  it("shows a working job's step and attempts, and cancels it", async () => {
    const running = job({ status: "running", attempt: 2, result: null });
    const calls = hub(running, detail({ state: "in_progress" }), { [`POST /api/jobs/${JOB_ID}/cancel`]: job({ status: "cancelled", result: null }) });
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    expect(await screen.findByText("Attempt 2 of 3")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Files changed/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel job" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === `/api/jobs/${JOB_ID}/cancel`)).toBe(true));
  });

  it("explains a job that stopped", async () => {
    hub(job({ status: "timed_out", failed_stage: "running", error: "The agent ran past 45 min.", result: null }), detail({ state: "approved" }));
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    expect(await screen.findByText("Job stopped at running the agent: The agent ran past 45 min.")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Start job" }).length).toBeGreaterThan(0);
  });

  it("opens from the /jobs/:id deep link", async () => {
    hub(job());
    renderAt(`/jobs/${JOB_ID}`, <WorkItemPage tab="job" />, "/jobs/:jobId");
    expect(await screen.findByRole("heading", { name: /Files changed/ })).toBeInTheDocument();
  });

  it("starts a job from an approved plan after stating the limits", async () => {
    expect(nextAction(detail({ state: "approved" }))).toEqual({ kind: "start_job", label: "Start job" });
    const plan = {
      id: PLAN_ID, work_item_id: WORK_ID, status: "approved", current_revision: 2, base_sha: "abc", base_branch: "main",
      error: null, auto: false, approvals: [], created_at: NOW, updated_at: NOW,
      revision: { id: REV_ID, revision: 2, content: { sandbox: { optional_hosts: ["registry.example.com"], cpus: 2, memory_gb: 4, timeout_min: 45, max_attempts: 3 } } },
    } as unknown as Plan; // prettier-ignore
    const calls = hub(null, detail({ state: "approved" }), {
      [`GET /api/plans/${PLAN_ID}`]: plan,
      [`POST /api/work-items/${WORK_ID}/jobs`]: job({ status: "created", result: null }),
    });
    const { router } = renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    expect(await screen.findByText(/No job yet/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start job" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/Up to 45 min, 3 attempts/)).toHaveTextContent("network: package mirrors and registry.example.com");
    fireEvent.click(within(dialog).getByRole("button", { name: "Start job" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ plan_revision_id: REV_ID }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/work/${WORK_ID}/job`));
  });
});

describe("Jobs page", () => {
  it("lists jobs with status, attempt and cost, filtered by status", async () => {
    const calls = mockApi({
      ...signedInAs(USER),
      "GET /api/jobs": (init: { url: string }) => ({
        items: init.url.includes("status=failed") ? [] : [job(), job({ id: "j2", key: "PAY-13", status: "running", attempt: 2, cost_usd: 1.5 })],
        next_cursor: null,
      }),
    });
    renderAt("/jobs", <JobsPage />);
    const table = await screen.findByRole("table", { name: "Jobs" });
    expect(within(table).getByRole("link", { name: "PAY-12" })).toHaveAttribute("href", `/work/${WORK_ID}/job`);
    expect(within(table).getByText("2 of 3")).toBeInTheDocument();
    expect(within(table).getByText("$1.50")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "stopped" } });
    expect(await screen.findByText("No jobs match this filter.")).toBeInTheDocument();
    expect(calls.at(-1)?.path).toBe("/api/jobs");
  });
});
