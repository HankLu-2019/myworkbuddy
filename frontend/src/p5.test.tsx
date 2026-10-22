import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import BoardPage from "./features/work/BoardPage";
import WorkItemPage from "./features/work/WorkItemPage";
import { approveBlockers } from "./features/work/PlanTab";
import { ackLabel, nextAction, stepOf } from "./features/work/states";
import { sectionOf } from "./features/work/PlanView";
import { contextFromParam } from "./features/chat/ChatPage";
import { MEMBER, mockApi, renderAt, signedInAs } from "./test-utils";
import type { Plan, PlanDoc, Revision, WorkItem, WorkItemDetail } from "./api";

afterEach(() => vi.unstubAllGlobals());

const NOW = new Date().toISOString();
const USER = { ...MEMBER, timezone: "Europe/Stockholm", timezone_confirmed: true };
const WORK_ID = "11111111-0000-4000-8000-000000000001";
const PLAN_ID = "22222222-0000-4000-8000-000000000002";
const REV_ID = "33333333-0000-4000-8000-000000000003";
const HASH = "ab12cd34".padEnd(64, "0");
const CI = "ci_config:.gitlab-ci.yml";

const work = (over: Partial<WorkItem> = {}): WorkItem => ({
  id: WORK_ID, state: "new", key: "PAY-12", summary: "Retry webhook delivery on 5xx", jira_status: "To Do",
  issue_type: "Story", priority: "High", labels: [], components: [], jira_url: "https://jira.example.com/browse/PAY-12",
  project: { id: 7, name: "payments" }, current_plan_id: null, plan_status: null, plan_error: null, cost_usd: 0,
  updated_remote: NOW, created_at: NOW, updated_at: NOW, ...over,
});

const detail = (over: Partial<WorkItemDetail> = {}): WorkItemDetail => ({
  ...work(), description: "Webhooks are dropped on 503.", links: [], assignee: "Alice", ...over,
});

const DOC: PlanDoc = {
  schema_version: 1, title: "Retry webhooks", summary: "Retry on 5xx.",
  ambiguities: [{ question: "Which codes?", assumption: "502, 503, 504." }], knowledge_refs: [],
  approach: "Wrap delivery in a retry.", alternatives: [],
  changes: [
    { path: "app/webhooks.py", action: "modify", description: "Add retries." },
    { path: "app/missing.py", action: "modify", description: "Oops." },
  ],
  tests: [{ name: "test_retries", kind: "unit", description: "Retries 3 times." }],
  verification: ["pytest -q"], risks: [], rollback: "Revert.", estimate: "S", commit_message: "PAY-12 Retry webhooks",
  sandbox: { optional_hosts: [], cpus: 2, memory_gb: 4, timeout_min: 45, max_attempts: 3 },
};

const revision = (over: Partial<Revision> = {}): Revision => ({
  id: REV_ID, revision: 2, content: DOC, content_hash: HASH, author: "planner", change_note: null,
  validation: { ok: true, issues: [{ level: "warning", field: "changes[1].path", message: ".gitlab-ci.yml is CI/CD configuration." }], needs_ack: [CI] },
  session_id: null, cost_usd: 0.012, created_at: NOW, ...over,
});

const plan = (over: Partial<Plan> = {}): Plan => ({
  id: PLAN_ID, work_item_id: WORK_ID, status: "needs_decision", current_revision: 2, base_sha: "abc", base_branch: "main",
  error: null, auto: false, revision: revision(), approvals: [], created_at: NOW, updated_at: NOW, ...over,
});

const hubRoutes = (p: Plan, item = detail({ state: "needs_decision", current_plan_id: PLAN_ID })) => ({
  ...signedInAs(USER),
  [`GET /api/work-items/${WORK_ID}`]: item,
  [`GET /api/plans/${PLAN_ID}`]: p,
  [`GET /api/plans/${PLAN_ID}/revisions`]: {
    items: [
      { revision: 2, content_hash: HASH, author: "planner", change_note: null, ok: true, created_at: NOW },
      { revision: 1, content_hash: "0".repeat(64), author: "planner", change_note: null, ok: false, created_at: NOW },
    ],
    next_cursor: null,
  },
  [`GET /api/plans/${PLAN_ID}/diff`]: {
    from_revision: 1, to_revision: 2,
    fields: [{ field: "approach", key: null, kind: "changed", before: "Old.", after: "Wrap delivery in a retry." }],
  },
  "GET /api/projects": [],
});

describe("states", () => {
  it("maps each state to a progress step and one next action", () => {
    expect(stepOf("new")).toBe(0);
    expect(stepOf("needs_decision")).toBe(1);
    expect(stepOf("approved")).toBe(2);
    expect(nextAction(work())).toEqual({ kind: "draft", label: "Draft plan" });
    expect(nextAction(work({ project: null }))).toEqual({ kind: "choose_repo", label: "Choose repository" });
    expect(nextAction(work({ state: "needs_decision" }))).toMatchObject({ label: "Review plan", to: `/work/${WORK_ID}/plan` });
    expect(nextAction(work({ state: "planning" })).kind).toBe("wait");
    expect(ackLabel(CI)).toBe("Changes CI or deployment configuration (.gitlab-ci.yml)");
    expect(ackLabel("optional_host:pypi.org")).toBe("Lets the sandbox reach pypi.org");
    expect(sectionOf("changes[1].path")).toBe("changes");
    expect(sectionOf("sandbox.optional_hosts[0]")).toBe("sandbox");
    expect(sectionOf("commit_message")).toBe("commit_message");
    expect(contextFromParam(`plan_revision:${REV_ID}`)).toEqual([{ type: "plan_revision", id: REV_ID }]);
  });

  it("explains why Approve is disabled", () => {
    const v = { ok: false, issues: [{ level: "error" as const, field: "x", message: "m" }], needs_ack: [CI, "large_change"] };
    expect(approveBlockers(v, new Set())).toEqual([
      "Fix 1 validation error (edit or revise the plan)",
      "Acknowledge 2 warnings",
    ]);
    expect(approveBlockers({ ...v, ok: true, issues: [] }, new Set([CI, "large_change"]))).toEqual([]);
  });
});

describe("Board", () => {
  it("puts tickets in their state's column with one action each", async () => {
    mockApi({
      ...signedInAs(USER),
      "GET /api/work-items": {
        items: [
          work(),
          work({ id: "w2", key: "PAY-13", state: "planning", current_plan_id: PLAN_ID, plan_status: "drafting" }),
          work({ id: "w3", key: "PAY-14", state: "needs_decision", cost_usd: 0.0042 }),
          work({ id: "w4", key: "OPS-1", project: null }),
          work({ id: "w5", key: "PAY-15", state: "new", plan_status: "failed", plan_error: "The planner took too long." }),
        ],
        next_cursor: null,
      },
      "GET /api/projects": [],
      "GET /api/integrations": [],
    });
    renderAt("/work", <BoardPage />);
    const column = async (name: string) => screen.findByRole("region", { name: new RegExp(`^${name}`) });
    const fresh = await column("New");
    expect(within(fresh).getAllByRole("listitem")).toHaveLength(3);
    expect(within(fresh).getByText("Choose repository")).toBeInTheDocument();
    expect(within(fresh).getByText("Planning failed: The planner took too long.")).toBeInTheDocument();
    expect(within(fresh).getAllByRole("button", { name: "Draft plan" })).toHaveLength(3);
    expect(within(await column("Planning")).getByText("Planner is working… usually 1–3 min")).toBeInTheDocument();
    const decide = await column("Needs decision");
    expect(within(decide).getByRole("link", { name: "Review plan" })).toHaveAttribute("href", "/work/w3/plan");
    expect(within(decide).getByText("$0.0042")).toBeInTheDocument();
    expect(within(await column("Done")).getByText("Nothing here.")).toBeInTheDocument();
  });

  it("asks for a repository before drafting when the ticket has none", async () => {
    const calls = mockApi({
      ...signedInAs(USER),
      "GET /api/work-items": { items: [work({ project: null })], next_cursor: null },
      "GET /api/projects": [{ id: 7, name: "payments", status: "ready" }],
      "GET /api/integrations": [],
      [`PATCH /api/work-items/${WORK_ID}`]: detail(),
      [`POST /api/work-items/${WORK_ID}/plans`]: plan({ status: "drafting", current_revision: 0, revision: null }),
    });
    renderAt("/work", <BoardPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Draft plan" }));
    const select = await screen.findByLabelText("Repository for PAY-12");
    await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(2));
    fireEvent.change(select, { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "Draft plan" }));
    expect(await screen.findByText("Planning PAY-12; you can leave this page", { exact: true })).toBeInTheDocument();
    expect(calls.filter((c) => c.method !== "GET").map((c) => [c.method, c.path, c.body])).toEqual([
      ["PATCH", `/api/work-items/${WORK_ID}`, { project_id: 7 }],
      ["POST", `/api/work-items/${WORK_ID}/plans`, undefined],
    ]);
  });

  it("says where to turn on intake when Refresh from Jira can't run", async () => {
    mockApi({
      ...signedInAs(USER),
      "GET /api/work-items": { items: [], next_cursor: null },
      "POST /api/work-items/refresh": new Response(
        JSON.stringify({ detail: "Turn on Jira intake in Settings first.", code: "conflict", field: "jira_intake_enabled" }),
        { status: 409 },
      ),
    });
    renderAt("/work", <BoardPage />);
    expect(await screen.findByText(/No tickets yet/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh from Jira" }));
    expect(await screen.findByText(/Turn on Jira intake in Settings first/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Settings" })).toHaveAttribute("href", "/settings/notifications");
  });
});

describe("Plan tab", () => {
  it("shows validation, needs acknowledgements before Approve, and approves exactly what was shown", async () => {
    const calls = mockApi({
      ...hubRoutes(plan()),
      [`POST /api/plans/${PLAN_ID}/approve`]: plan({ status: "approved" }),
    });
    renderAt(`/work/${WORK_ID}/plan`, <WorkItemPage tab="plan" />, "/work/:workId/plan");
    const approve = await screen.findByRole("button", { name: "Approve revision 2" });
    expect(approve).toBeDisabled();
    expect(screen.getByText("To approve: Acknowledge 1 warning.")).toBeInTheDocument();
    expect(approve).toHaveAttribute("aria-describedby", "approve-why");
    expect(screen.getByRole("list", { name: "Warnings" })).toHaveTextContent(".gitlab-ci.yml is CI/CD configuration.");
    // Revision 2 changed the approach since revision 1.
    expect(await screen.findByText("Changed from revision 1: Approach and alternatives.")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: /^Approach and alternatives/ })).getByText("Changed")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Progress" })).toHaveTextContent("Plan");

    fireEvent.click(screen.getByRole("checkbox", { name: "Changes CI or deployment configuration (.gitlab-ci.yml)" }));
    await waitFor(() => expect(approve).toBeEnabled());
    expect(screen.queryByText(/To approve:/)).not.toBeInTheDocument();
    fireEvent.click(approve);
    expect(await screen.findByText("Revision 2 approved", { exact: true })).toBeInTheDocument();
    const sent = calls.find((c) => c.method === "POST");
    expect(sent?.body).toEqual({ revision: 2, content_hash: HASH, acknowledged: [CI] });
  });

  it("keeps Approve disabled while there are errors, and marks the missing path", async () => {
    const bad = revision({
      validation: {
        ok: false,
        issues: [{ level: "error", field: "changes[1].path", message: "app/missing.py doesn't exist at the plan's base commit." }],
        needs_ack: [],
      },
    });
    mockApi(hubRoutes(plan({ revision: bad })));
    renderAt(`/work/${WORK_ID}/plan`, <WorkItemPage tab="plan" />, "/work/:workId/plan");
    const approve = await screen.findByRole("button", { name: "Approve revision 2" });
    expect(approve).toBeDisabled();
    expect(screen.getByText("To approve: Fix 1 validation error (edit or revise the plan).")).toBeInTheDocument();
    const errors = screen.getByRole("list", { name: "Errors" });
    expect(within(errors).getByRole("link", { name: "File changes" })).toHaveAttribute("href", "#plan-changes");
    const table = screen.getByRole("table", { name: "File changes" });
    expect(within(table).getByText("app/missing.py doesn't exist at the plan's base commit.", { exact: false })).toBeInTheDocument();
  });

  it("revises with feedback and keeps the unsent text as a draft", async () => {
    let current = plan();
    const calls = mockApi({
      ...hubRoutes(plan()),
      [`GET /api/plans/${PLAN_ID}`]: () => current,
      [`POST /api/plans/${PLAN_ID}/revise`]: () => (current = plan({ status: "drafting" })),
    });
    renderAt(`/work/${WORK_ID}/plan`, <WorkItemPage tab="plan" />, "/work/:workId/plan");
    const box = await screen.findByLabelText("Revise with feedback");
    fireEvent.change(box, { target: { value: "Give up after three attempts." } });
    expect(localStorage.getItem(`draft:${USER.id}:plan-feedback:${PLAN_ID}`)).toBe("Give up after three attempts.");
    fireEvent.click(screen.getByRole("button", { name: "Revise with feedback" }));
    expect(await screen.findByText("Planner is writing revision 3; you can leave this page", { exact: true })).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ revision: 2, feedback: "Give up after three attempts." });
    expect(localStorage.getItem(`draft:${USER.id}:plan-feedback:${PLAN_ID}`)).toBeNull();
    expect(await screen.findByText(/Planner is working on revision 3/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve revision/ })).not.toBeInTheDocument();
  });

  it("rejects after confirming, with an optional reason", async () => {
    const calls = mockApi({
      ...hubRoutes(plan()),
      [`POST /api/plans/${PLAN_ID}/reject`]: plan({ status: "rejected" }),
    });
    renderAt(`/work/${WORK_ID}/plan`, <WorkItemPage tab="plan" />, "/work/:workId/plan");
    fireEvent.click(await screen.findByRole("button", { name: "Reject" }));
    const dialog = await screen.findByRole("dialog", { name: "Reject revision 2?" });
    fireEvent.change(within(dialog).getByLabelText("Reason (optional)"), { target: { value: "Wrong approach" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Reject plan" }));
    expect(await screen.findByText("Plan rejected; the ticket is back in New", { exact: true })).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("/reject"))?.body).toEqual({ revision: 2, comment: "Wrong approach" });
  });

  it("edits a copy and saves it as a new revision", async () => {
    const calls = mockApi({
      ...hubRoutes(plan()),
      [`POST /api/plans/${PLAN_ID}/revisions`]: plan({ current_revision: 3, revision: revision({ revision: 3, author: "user" }) }),
    });
    renderAt(`/work/${WORK_ID}/plan`, <WorkItemPage tab="plan" />, "/work/:workId/plan");
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const form = await screen.findByRole("form", { name: "Edit revision 2" });
    fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "Retry webhooks, narrower" } });
    fireEvent.click(within(form).getByRole("button", { name: "Remove file change 2" }));
    fireEvent.change(within(form).getByLabelText("What you changed (optional)"), { target: { value: "Narrower" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save as revision 3" }));
    expect(await screen.findByText("Saved as revision 3", { exact: true })).toBeInTheDocument();
    const body = calls.find((c) => c.path.endsWith("/revisions") && c.method === "POST")?.body as {
      base_revision: number;
      content: PlanDoc;
      change_note: string;
    };
    expect(body.base_revision).toBe(2);
    expect(body.change_note).toBe("Narrower");
    expect(body.content.title).toBe("Retry webhooks, narrower");
    expect(body.content.changes.map((c) => c.path)).toEqual(["app/webhooks.py"]);
  });

  it("opens a chat about the revision", async () => {
    const calls = mockApi({
      ...hubRoutes(plan()),
      "POST /api/chat/sessions": { id: "s1", title: "Discuss", context: [], source: "chat", visibility: "private", project_id: 7, owner_id: 2, created_at: NOW, updated_at: NOW },
    });
    const { router } = renderAt(`/work/${WORK_ID}/plan`, <WorkItemPage tab="plan" />, "/work/:workId/plan");
    fireEvent.click(await screen.findByRole("button", { name: "Discuss in chat" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/chat/s1"));
    expect(calls.find((c) => c.path === "/api/chat/sessions")?.body).toMatchObject({ context: [{ type: "plan_revision", id: REV_ID }] });
  });

  it("renders the hub from a /plans/:id deep link", async () => {
    mockApi(hubRoutes(plan({ status: "approved" }), detail({ state: "approved", current_plan_id: PLAN_ID })));
    renderAt(`/plans/${PLAN_ID}`, <WorkItemPage tab="plan" />, "/plans/:planId");
    expect(await screen.findByText(/Revision 2 is approved/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve revision/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Plan" })).toHaveAttribute("aria-current", "page");
  });
});
