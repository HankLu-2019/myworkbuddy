import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import InboxPage, { groupByDay } from "./features/inbox/InboxPage";
import SchedulesPage from "./features/schedules/SchedulesPage";
import ScheduleEditPage from "./features/schedules/ScheduleEditPage";
import ScheduleDetailPage from "./features/schedules/ScheduleDetailPage";
import RunPage from "./features/schedules/RunPage";
import { linkText } from "./features/schedules/RunResult";
import { toInputs } from "./features/schedules/InputsForm";
import { presetOf } from "./features/schedules/presets";
import ChatPage, { contextFromParam } from "./features/chat/ChatPage";
import SettingsPage from "./features/settings/SettingsPage";
import HomePage from "./features/home/HomePage";
import { MEMBER, mockApi, renderAt, signedInAs, sse } from "./test-utils";
import type { InboxItem } from "./api";

afterEach(() => vi.unstubAllGlobals());

const NOW = new Date().toISOString();
const YESTERDAY = new Date(Date.now() - 86_400_000).toISOString();
const RUN_ID = "6f1c0a52-8b1e-4f53-9d2a-1f4e2c3b4a5d";
const SCHEDULE_ID = "a1b2c3d4-0000-4000-8000-000000000001";
const USER = { ...MEMBER, timezone: "Europe/Stockholm", timezone_confirmed: true, email_notifications: true };

const item = (over: Partial<InboxItem> = {}): InboxItem => ({
  id: 1, kind: "alert", severity: "alert", title: "K8s health: 1 crash loop", body_md: "", link: `/runs/${RUN_ID}`,
  run_id: RUN_ID, read_at: null, created_at: NOW, ...over,
});

const AGENTS = [
  {
    id: 1, slug: "k8s-health", name: "Kubernetes health", description: "Crash loops and warnings.",
    model_alias: "chat", tools: ["k8s_get"], system: true, integrations: ["kubernetes"], missing: [],
    inputs_schema: {
      type: "object", required: ["namespaces"],
      properties: { namespaces: { title: "Namespaces", type: "array", items: { type: "string" } }, integration: { title: "Cluster", type: "string" } },
    },
  },
  {
    id: 2, slug: "jira-digest", name: "Jira digest", description: "A digest.", model_alias: "fast", tools: ["jira_search"],
    system: true, integrations: ["jira"], missing: ["jira"],
    inputs_schema: { type: "object", required: ["jql"], properties: { jql: { title: "JQL", type: "string", maxLength: 1000 } } },
  },
];

const schedule = (over: Record<string, unknown> = {}) => ({
  id: SCHEDULE_ID, name: "Prod pods", agent: { slug: "k8s-health", name: "Kubernetes health" }, prompt: "",
  inputs: { namespaces: ["prod"] }, cron: "0 8 * * 1-5", description: "At 08:00, Monday through Friday",
  timezone: "Europe/Stockholm", enabled: true, notify_mode: "on_change", notify_email: true, max_runtime_s: 300,
  next_run_at: NOW, last_run_at: null, last_status: null, consecutive_failures: 0, paused_reason: null,
  created_at: NOW, updated_at: NOW, ...over,
});

const report = {
  status: "alert", headline: "payments is crash-looping", summary_md: "OOM since 2.3.",
  findings: [
    { fingerprint: "k8s:prod/payments:CrashLoopBackOff", severity: "alert", title: "Crash loop", detail: "Exit **137**",
      links: ["https://grafana.example.com/d/abc?var=prod"], verified: true },
    { fingerprint: "k8s:prod/ghost:OOMKilled", severity: "warning", title: "Invented", detail: "", links: [], verified: false },
  ],
};

const run = (over: Record<string, unknown> = {}) => ({
  id: RUN_ID, schedule_id: SCHEDULE_ID, schedule_name: "Prod pods", agent: "k8s-health", trigger: "schedule",
  status: "succeeded", scheduled_for: NOW, started_at: NOW, finished_at: NOW, changed: true, notified: true,
  headline: report.headline, report_status: "alert", error_code: null, error: null, cost_usd: "0.0123", created_at: NOW,
  report, fingerprints: ["k8s:prod/payments:CrashLoopBackOff"], tokens_in: 10, tokens_out: 5, session_id: "s-1",
  prompt: "", inputs: { namespaces: ["prod"] }, new_fingerprints: ["k8s:prod/payments:CrashLoopBackOff"],
  resolved: [{ fingerprint: "k8s:prod/db:OOMKilled", title: "DB OOM", severity: "alert" }], previous_run_id: "prev",
  ...over,
});

describe("Inbox (07 §4.2)", () => {
  it("groups by day; opening an item marks it read and goes to its link", async () => {
    const calls = mockApi({
      ...signedInAs(USER),
      "GET /api/inbox": { items: [item(), item({ id: 2, kind: "system", severity: "info", title: "Older", created_at: YESTERDAY, read_at: NOW })], next_cursor: null },
      "GET /api/schedules": { items: [schedule()], next_cursor: null },
      "POST /api/inbox/1/read": item({ read_at: NOW }),
    });
    const { router } = renderAt("/inbox", <InboxPage />);
    expect(await screen.findByRole("region", { name: "Today" })).toHaveTextContent("K8s health: 1 crash loop");
    expect(screen.getByRole("region", { name: "Yesterday" })).toHaveTextContent("Older");
    expect(screen.getByRole("region", { name: "Today" })).toHaveTextContent("Schedule");
    expect(screen.getByLabelText("Unread")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^K8s health: 1 crash loop Schedule/ }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/inbox/1/read")).toBe(true));
    expect(router.state.location.pathname).toBe(`/runs/${RUN_ID}`);
  });

  it("is empty with a pointer to schedules, and shows errors", async () => {
    mockApi({ ...signedInAs(USER), "GET /api/inbox": { items: [], next_cursor: null }, "GET /api/schedules": { items: [], next_cursor: null } });
    const first = renderAt("/inbox", <InboxPage />);
    expect(await screen.findByText("You're all caught up.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create a schedule" })).toHaveAttribute("href", "/schedules/new");
    first.unmount();
    mockApi({ ...signedInAs(USER), "GET /api/schedules": { items: [], next_cursor: null } });
    renderAt("/inbox", <InboxPage />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("marks all read with undo, and filters by the URL", async () => {
    const calls = mockApi({
      ...signedInAs(USER),
      "GET /api/inbox": { items: [item()], next_cursor: null },
      "GET /api/schedules": { items: [schedule()], next_cursor: null },
      "POST /api/inbox/read-all": { marked: 1, ids: [1] },
      "POST /api/inbox/unread": { marked: 1, ids: [1] },
    });
    renderAt("/inbox?filter=alerts", <InboxPage />);
    await screen.findByText("K8s health: 1 crash loop");
    expect(screen.getByRole("link", { name: "Alerts" })).toHaveAttribute("aria-current", "page");
    expect(calls.find((c) => c.path === "/api/inbox")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Mark all read" }));
    fireEvent.click(await screen.findByRole("button", { name: "Undo" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/inbox/unread")?.body).toEqual({ ids: [1] }));
  });

  it("groups items across days in order", () => {
    const groups = groupByDay([item(), item({ id: 2, created_at: YESTERDAY })]);
    expect(groups.map(([d, l]) => [d, l.length])).toEqual([["Today", 1], ["Yesterday", 1]]);
  });
});

describe("Schedules (07 §4.7)", () => {
  it("lists schedules with timing, last result and a paused banner; toggles and runs", async () => {
    const calls = mockApi({
      ...signedInAs(USER),
      "GET /api/schedules": {
        items: [
          schedule({ last_status: "alert", last_run_at: NOW }),
          schedule({ id: "b", name: "CI", enabled: false, paused_reason: "Paused after 3 failures: boom", next_run_at: null }),
        ],
        next_cursor: null,
      },
      "PATCH /api/schedules/a1b2c3d4-0000-4000-8000-000000000001": schedule({ enabled: false }),
      "POST /api/schedules/a1b2c3d4-0000-4000-8000-000000000001/run": { run_id: RUN_ID },
    });
    renderAt("/schedules", <SchedulesPage />);
    const row = await screen.findByRole("row", { name: /Prod pods/ });
    expect(row).toHaveTextContent("At 08:00, Monday through Friday");
    expect(row).toHaveTextContent("Alert");
    expect(screen.getByText(/CI: Paused after 3 failures: boom/)).toBeInTheDocument();
    fireEvent.click(within(row).getByRole("switch"));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ enabled: false }));
    fireEvent.click(within(row).getByRole("button", { name: "Run now" }));
    expect(await screen.findByText(/Prod pods is running/)).toBeInTheDocument();
  });

  it("has an empty state", async () => {
    mockApi({ ...signedInAs(USER), "GET /api/schedules": { items: [], next_cursor: null } });
    renderAt("/schedules", <SchedulesPage />);
    expect(await screen.findByText(/No schedules yet/)).toBeInTheDocument();
  });

  it("creates a schedule from the form with presets, a preview and inputs from the agent's schema", async () => {
    const calls = mockApi({
      ...signedInAs(USER),
      "GET /api/agents": AGENTS,
      "POST /api/schedules/preview": { valid: true, error: null, description: "At 08:00, Monday through Friday", next: [NOW, NOW, NOW] },
      "POST /api/schedules": schedule(),
      "GET /api/schedules/a1b2c3d4-0000-4000-8000-000000000001": schedule(),
    });
    const { router } = renderAt("/schedules/new", <ScheduleEditPage />);
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Prod pods" } });
    fireEvent.change(screen.getByLabelText("Namespaces"), { target: { value: "prod\nweb\n" } });
    await waitFor(() => expect(document.body.textContent).toMatch(/Next: .*\(Europe\/Stockholm\)/), { timeout: 3000 });
    fireEvent.click(screen.getByLabelText("Custom"));
    expect(screen.getByLabelText("Cron expression")).toHaveValue("0 8 * * 1-5");
    fireEvent.click(screen.getByLabelText("Every hour"));
    fireEvent.click(screen.getByLabelText("On alert only"));
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "POST" && c.path === "/api/schedules")?.body).toMatchObject({
        name: "Prod pods", agent: "k8s-health", cron: "0 * * * *", inputs: { namespaces: ["prod", "web"] },
        notify_mode: "on_alert", timezone: "Europe/Stockholm",
      }),
    );
    await waitFor(() => expect(router.state.location.pathname).toBe(`/schedules/${SCHEDULE_ID}`));
  });

  it("shows server field errors, missing credentials, and a test run in a drawer", async () => {
    mockApi({
      ...signedInAs(USER),
      "GET /api/agents": AGENTS,
      "POST /api/schedules/preview": { valid: false, error: "Runs would be 5 minutes apart; the minimum is 15 minutes.", description: null, next: [] },
      "POST /api/schedules": new Response(
        JSON.stringify({ detail: "inputs.jql: '' is too short", code: "validation_error", field: "inputs.jql" }),
        { status: 422 },
      ),
      "POST /api/schedules/test": { run_id: RUN_ID },
      [`GET /api/runs/${RUN_ID}`]: run(),
    });
    renderAt("/schedules/new?agent=jira-digest", <ScheduleEditPage />);
    expect(await screen.findByText(/Needs a working jira credential/)).toBeInTheDocument();
    expect(await screen.findByText(/the minimum is 15 minutes/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));
    expect(await screen.findByText("inputs.jql: '' is too short")).toBeInTheDocument();
    expect(screen.getByLabelText("JQL")).toHaveAttribute("aria-invalid", "true");
    fireEvent.click(screen.getByRole("button", { name: "Run once now to test" }));
    const drawer = await screen.findByRole("dialog", { name: "Test run" });
    expect(await within(drawer).findByText("payments is crash-looping")).toBeInTheDocument();
  });

  it("shows a schedule's configuration, runs, and Resume for a paused one", async () => {
    const calls = mockApi({
      ...signedInAs(USER),
      [`GET /api/schedules/${SCHEDULE_ID}`]: schedule({ enabled: false, paused_reason: "Paused after 3 failures: boom", next_run_at: null }),
      [`GET /api/schedules/${SCHEDULE_ID}/runs`]: { items: [run()], next_cursor: null },
      [`PATCH /api/schedules/${SCHEDULE_ID}`]: schedule(),
    });
    renderAt(`/schedules/${SCHEDULE_ID}`, <ScheduleDetailPage />, "/schedules/:scheduleId");
    expect(await screen.findByText("Paused after 3 failures: boom")).toBeInTheDocument();
    expect(screen.getByText("At 08:00, Monday through Friday (Europe/Stockholm)")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /Run of/ })).toHaveAttribute("href", `/runs/${RUN_ID}`);
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ enabled: true }));
  });

  it("knows its presets and builds inputs", () => {
    expect(presetOf("0  8 * * 1-5")).toBe("weekday_morning");
    expect(presetOf("15 7 * * *")).toBe("custom");
    const schema = { required: ["jql"], properties: { jql: { type: "string" }, extra: { type: "array" } } };
    expect(toInputs(schema, { jql: " x ", extra: "\n" })).toEqual({ jql: "x" });
  });
});

describe("Run result (07 §4.8)", () => {
  it("shows findings, what's new and resolved, and the way to investigate", async () => {
    mockApi({ ...signedInAs(USER), [`GET /api/runs/${RUN_ID}`]: run() });
    renderAt(`/runs/${RUN_ID}`, <RunPage />, "/runs/:runId");
    expect(await screen.findByRole("heading", { name: "payments is crash-looping" })).toBeInTheDocument();
    const findings = screen.getByRole("list", { name: "Findings" });
    expect(within(findings).getByText("Crash loop").closest(".finding")).toHaveTextContent("New since last run");
    expect(within(findings).getByText("Invented").closest(".finding")).toHaveTextContent("Unverified");
    expect(screen.getByText("grafana.example.com")).toBeInTheDocument();
    expect(within(findings).queryByRole("link")).toBeNull(); // links from findings are text, not anchors
    expect(screen.getByRole("region", { name: "Resolved since last run" })).toHaveTextContent("DB OOM");
    expect(screen.getByRole("link", { name: "Investigate in chat" })).toHaveAttribute("href", `/chat?context=run:${RUN_ID}`);
    expect(screen.getByRole("link", { name: "Open transcript" })).toHaveAttribute("href", "/chat/s-1");
    expect(screen.getByText("$0.01")).toBeInTheDocument();
  });

  it("explains a failed run and a missing one", async () => {
    mockApi({
      ...signedInAs(USER),
      [`GET /api/runs/${RUN_ID}`]: run({ status: "failed", report: null, error_code: "budget_exceeded", error: "Budget reached." }),
    });
    const first = renderAt(`/runs/${RUN_ID}`, <RunPage />, "/runs/:runId");
    expect(await screen.findByRole("link", { name: "View usage" })).toHaveAttribute("href", "/settings/usage");
    first.unmount();
    mockApi({ ...signedInAs(USER), [`GET /api/runs/${RUN_ID}`]: new Response(JSON.stringify({ detail: "Run not found", code: "not_found" }), { status: 404 }) });
    renderAt(`/runs/${RUN_ID}`, <RunPage />, "/runs/:runId");
    expect(await screen.findByText(/doesn't exist or isn't shared with you/)).toBeInTheDocument();
  });

  it("shows links as host and path", () => {
    expect(linkText("https://gitlab.example.com/team/etl/-/jobs/1/")).toEqual({ host: "gitlab.example.com", rest: "/team/etl/-/jobs/1" });
  });
});

const SESSION = { id: "s-9", title: "Check prod", source: "chat", visibility: "private", project_id: null, owner_id: USER.id, context: [], created_at: NOW, updated_at: NOW };
const CARD = {
  proposal_id: "p-1", name: "Prod pods", agent: "k8s-health", agent_name: "Kubernetes health", cron: "0 8 * * 1-5",
  timezone: "Europe/Stockholm", inputs: { namespaces: ["prod"] }, notify_mode: "on_alert", prompt: "",
  description: "At 08:00, Monday through Friday", next_runs: [NOW], expires_at: new Date(Date.now() + 3_600_000).toISOString(),
};

describe("Chat → schedule (07 §5.6, §5.7)", () => {
  it("shows a proposal card from the stream and creates the schedule on Create", async () => {
    let sent = false;
    const calls = mockApi({
      ...signedInAs(USER),
      "GET /api/chat/sessions": [SESSION],
      "GET /api/chat/shared": [],
      "GET /api/projects": [],
      "GET /api/chat/sessions/s-9": SESSION,
      "GET /api/chat/sessions/s-9/messages": () =>
        sent
          ? [
              { id: 1, role: "user", content: "Check prod every weekday", created_at: NOW, citations: [], tools: [], status: "complete" },
              { id: 2, role: "assistant", content: "I've proposed it.", created_at: NOW, citations: [], tools: [], status: "complete", proposals: [CARD] },
            ]
          : [],
      "POST /api/chat/sessions/s-9/messages": () => {
        sent = true;
        return sse([
        ["delta", { text: "I've proposed it." }],
          ["proposal", CARD],
          ["done", { message_id: 2, citations: [] }],
        ]);
      },
      "POST /api/schedules/proposals/p-1/accept": schedule(),
    });
    renderAt("/chat/s-9", <ChatPage />, "/chat/:sessionId");
    fireEvent.change(await screen.findByLabelText("Message"), { target: { value: "Check prod every weekday" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const card = await screen.findByRole("region", { name: "Proposed schedule: Prod pods" });
    expect(card).toHaveTextContent("At 08:00, Monday through Friday (Europe/Stockholm) · Notify on alert only");
    fireEvent.click(within(card).getByRole("button", { name: "Create" }));
    expect(await within(card).findByRole("link", { name: "Open" })).toHaveAttribute("href", `/schedules/${SCHEDULE_ID}`);
    expect(calls.find((c) => c.path === "/api/schedules/proposals/p-1/accept")?.body).toEqual({});
  });

  it("starts a chat with a run attached and suggests the first question", async () => {
    const withContext = { ...SESSION, id: "s-10", title: "Investigate a run", context: [{ type: "run", id: RUN_ID }] };
    const calls = mockApi({
      ...signedInAs(USER),
      "GET /api/chat/sessions": [withContext],
      "GET /api/chat/shared": [],
      "GET /api/projects": [],
      "POST /api/chat/sessions": withContext,
      "GET /api/chat/sessions/s-10": withContext,
      "GET /api/chat/sessions/s-10/messages": [],
    });
    const { router } = renderAt(`/chat?context=run:${RUN_ID}`, <ChatPage />, "/chat/:sessionId?");
    await waitFor(() => expect(router.state.location.pathname).toBe("/chat/s-10"));
    expect(calls.filter((c) => c.method === "POST" && c.path === "/api/chat/sessions")).toHaveLength(1);
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ context: [{ type: "run", id: RUN_ID }], title: "Investigate a run" });
    expect(await screen.findByRole("link", { name: "Run result" })).toHaveAttribute("href", `/runs/${RUN_ID}`);
    await waitFor(() => expect(screen.getByLabelText("Message")).toHaveValue("What's the likely cause and what should I check?"));
  });

  it("parses the context parameter", () => {
    expect(contextFromParam(`run:${RUN_ID}`)).toEqual([{ type: "run", id: RUN_ID }]);
    expect(contextFromParam("run:../etc")).toEqual([]);
  });

  it("opens run transcripts read-only", async () => {
    const runSession = { ...SESSION, id: "s-run", source: "run", title: "Prod pods · 2026-10-21 08:00" };
    mockApi({
      ...signedInAs(USER),
      "GET /api/chat/sessions": [],
      "GET /api/chat/shared": [],
      "GET /api/projects": [],
      "GET /api/chat/sessions/s-run": runSession,
      "GET /api/chat/sessions/s-run/messages": [],
    });
    renderAt("/chat/s-run", <ChatPage />, "/chat/:sessionId");
    expect(await screen.findByText(/transcript of a scheduled run/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Message")).toBeNull();
  });
});

describe("Settings → Notifications and Home (07 §4.12, §4.1)", () => {
  it("turns email notifications off", async () => {
    const calls = mockApi({ ...signedInAs(USER), "PATCH /api/auth/me": { ...USER, email_notifications: false } });
    renderAt("/settings/notifications", <SettingsPage />, "/settings/:tab");
    fireEvent.click(await screen.findByRole("switch", { name: /Email me about alerts/ }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ email_notifications: false }));
    expect(await screen.findByText("Email notifications off")).toBeInTheDocument();
  });

  it("shows paused schedules and alerts under Needs you, and today's brief", async () => {
    mockApi({
      ...signedInAs(USER),
      "GET /api/home": {
        needs_you: [
          { kind: "schedule_paused", key: "schedule:x", message: "Paused after 3 failures: boom", action: "Review", href: `/schedules/${SCHEDULE_ID}`, at: NOW },
          { kind: "alert", key: "inbox:1", message: "K8s health: 1 crash loop", action: "Open alert", href: `/runs/${RUN_ID}`, at: NOW },
        ],
        today: { run_id: RUN_ID, schedule_name: "Morning brief", headline: "2 failed builds", status: "warning", at: NOW },
        running: [], recent_chats: [], recent_pages: [], librarian: null, checklists: [],
      },
    });
    renderAt("/", <HomePage />);
    expect(await screen.findByRole("link", { name: "Review" })).toHaveAttribute("href", `/schedules/${SCHEDULE_ID}`);
    expect(screen.getByRole("link", { name: "Open alert" })).toHaveAttribute("href", `/runs/${RUN_ID}`);
    const today = screen.getByRole("region", { name: "Today" });
    expect(today).toHaveTextContent("Morning brief: 2 failed builds");
    expect(within(today).getByRole("link", { name: "Open" })).toHaveAttribute("href", `/runs/${RUN_ID}`);
  });
});
