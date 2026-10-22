import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { RunResult } from "./features/schedules/RunResult";
import { MEMBER, mockApi, renderAt, signedInAs } from "./test-utils";
import type { RunDetail, TicketDraft } from "./api";

afterEach(() => vi.unstubAllGlobals());

const NOW = new Date().toISOString();
const RUN = "88888888-0000-4000-8000-000000000008";
const FP = "gitlab:team/payments:pipeline:55:test:unit";
const run = (agent: string): RunDetail =>
  ({
    id: RUN, schedule_id: null, schedule_name: "CI watch", agent, trigger: "manual", status: "succeeded",
    scheduled_for: NOW, started_at: NOW, finished_at: NOW, changed: true, notified: false, headline: "1 failure",
    report_status: "alert", error_code: null, error: null, cost_usd: 0.01, created_at: NOW, fingerprints: [FP],
    tokens_in: 1, tokens_out: 1, session_id: null, prompt: "", inputs: {}, new_fingerprints: [FP], resolved: [],
    previous_run_id: null,
    report: { schema_version: 1, status: "alert", headline: "1 failure", summary_md: "",
      findings: [{ fingerprint: FP, severity: "alert", title: "payments main is broken", detail: "test_retries fails", links: [], verified: true }] },
  }) as unknown as RunDetail; // prettier-ignore

const DRAFT: TicketDraft = {
  fingerprint: FP, existing: null, integrations: [{ id: 3, name: "Jira" }], integration_id: 3,
  projects: [{ key: "PAY", name: "Payments" }], project_key: "PAY", issue_type: "Bug", summary: "payments main is broken",
  description: "test_retries fails", labels: ["mwb-ci"], autoplan: false, repository_id: 7,
}; // prettier-ignore

describe("CI failure → ticket (08 §F6)", () => {
  it("previews the issue, sends exactly the edited fields and links the work item", async () => {
    const calls = mockApi({
      ...signedInAs(MEMBER),
      [`GET /api/runs/${RUN}/ticket-draft`]: DRAFT,
      "GET /api/projects": [{ id: 7, name: "payments", status: "ready" }],
      [`POST /api/runs/${RUN}/tickets`]: { jira_key: "PAY-1001", jira_url: "https://jira/browse/PAY-1001", work_item_id: "w1", plan_started: true, created: true },
    });
    renderAt(`/runs/${RUN}`, <RunResult run={run("ci-failures")} />);
    fireEvent.click(screen.getByRole("button", { name: "Create ticket" }));
    const dialog = await screen.findByRole("dialog", { name: "Create a Jira ticket" });
    const summary = await within(dialog).findByLabelText("Summary");
    fireEvent.change(summary, { target: { value: "Fix test_retries on main" } });
    fireEvent.click(within(dialog).getByLabelText("Draft a plan for it right away"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Create Jira issue" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")).toBeTruthy());
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({
      fingerprint: FP, integration_id: 3, project_key: "PAY", issue_type: "Bug", summary: "Fix test_retries on main",
      description: "test_retries fails", labels: ["mwb-ci"], autoplan: true, repository_id: 7,
    }); // prettier-ignore
    expect(await within(dialog).findByText("PAY-1001")).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Open it on the Board" })).toHaveAttribute("href", "/work/w1");
  });

  it("only offers tickets on CI failure runs, and opens an existing one", async () => {
    const { unmount } = renderAt(`/runs/${RUN}`, <RunResult run={run("k8s-health")} />);
    expect(screen.queryByRole("button", { name: "Create ticket" })).not.toBeInTheDocument();
    unmount();
    mockApi({ ...signedInAs(MEMBER), [`GET /api/runs/${RUN}/ticket-draft`]: { ...DRAFT, existing: { jira_key: "PAY-9", work_item_id: "w9" } }, "GET /api/projects": [] });
    renderAt(`/runs/${RUN}`, <RunResult run={run("ci-failures")} />);
    fireEvent.click(screen.getByRole("button", { name: "Create ticket" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("PAY-9")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Create Jira issue" })).not.toBeInTheDocument();
  });
});
