import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import InsightsPage, { formatMinutes } from "./features/work/InsightsPage";
import { ADMIN, MEMBER, mockApi, renderAt, signedInAs } from "./test-utils";
import type { Insights } from "./api";

afterEach(() => vi.unstubAllGlobals());

const dist = (pairs: [number, number][]) => ({
  mean: 1.17,
  distribution: pairs.map(([value, jobs]) => ({ value, label: value >= 5 ? "5+" : String(value), jobs })),
});
const DATA: Insights = {
  start: "2026-10-05", end: "2026-10-18", timezone: "America/New_York", scope: "me", jobs: 6, merged: 2,
  outcomes: [
    { outcome: "merged", error_code: null, jobs: 2, cost_usd: 4 },
    { outcome: "failed", error_code: "verify_failed", jobs: 1, cost_usd: 0.4 },
    { outcome: "in_progress", error_code: null, jobs: 1, cost_usd: 0.2 },
  ],
  median_minutes_to_review: 45, median_minutes_push_to_merge: 840,
  attempts: dist([[0, 1], [1, 3], [2, 2]]), review_rounds: dist([[1, 5], [2, 1]]), plan_revisions: dist([[1, 4], [3, 1]]),
  cost: { jobs_usd: 5.2, planning_usd: 0.95, total_usd: 6.15, per_merged_usd: 2.3 },
  weeks: [
    { week_start: "2026-10-05", jobs: 2, merged: 1, job_cost_usd: 1.9, planning_cost_usd: 0.35, cost_usd: 2.25 },
    { week_start: "2026-10-12", jobs: 4, merged: 1, job_cost_usd: 3.3, planning_cost_usd: 0.6, cost_usd: 3.9 },
  ],
}; // prettier-ignore
const OPTIONS = { projects: [{ id: 7, name: "payments" }], issue_types: ["Bug", "Story"] };

describe("Insights (08 §F9)", () => {
  it("shows the headline figures, a chart per question, and a table view for each", async () => {
    mockApi({ ...signedInAs(MEMBER), "GET /api/insights": DATA, "GET /api/insights/options": OPTIONS });
    renderAt("/insights", <InsightsPage />);
    expect(await screen.findByRole("group", { name: "Jobs" })).toHaveTextContent("62 merged (33 %)");
    expect(screen.getByRole("group", { name: "Start to review" })).toHaveTextContent("45 min");
    expect(screen.getByRole("group", { name: "Push to merge" })).toHaveTextContent("14 h");
    expect(screen.getByRole("group", { name: "AI cost per merged change" })).toHaveTextContent("$2.30");
    expect(screen.getByRole("group", { name: "AI cost" })).toHaveTextContent("$6.15$5.20 jobs · $0.95 planning");
    expect(screen.getByText("Jobs started from 2026-10-05 to 2026-10-18, in America/New_York time.")).toBeInTheDocument();
    const ended = screen.getByRole("region", { name: "How jobs ended" });
    expect(within(ended).getByText("Failed: verify failed")).toBeInTheDocument();
    expect(within(ended).getByText("In progress")).toBeInTheDocument();
    fireEvent.click(within(ended).getByRole("button", { name: "Show table" }));
    const table = within(ended).getByRole("table", { name: "How jobs ended" });
    expect(within(table).getByRole("row", { name: /Merged 2 jobs/ })).toBeInTheDocument();
    expect(within(ended).getByRole("button", { name: "Show chart" })).toHaveAttribute("aria-pressed", "true");
    const weekly = screen.getByRole("region", { name: "AI cost per week" });
    expect(within(weekly).getByRole("img")).toHaveAccessibleName(/AI cost per week: .*\$2\.25, .*\$3\.90/);
    for (const name of ["AI cost by outcome", "Attempts per job", "Review rounds per job", "Plan revisions before approval"]) {
      expect(within(screen.getByRole("region", { name })).getByRole("button", { name: "Show table" })).toBeInTheDocument();
    }
    expect(screen.queryByLabelText("Whose jobs")).toBeNull(); // members see their own work only
  });

  it("sends the filters, and admins can switch to everyone's jobs", async () => {
    const calls = mockApi({ ...signedInAs(ADMIN), "GET /api/insights": DATA, "GET /api/insights/options": OPTIONS });
    renderAt("/insights?from=2026-10-05", <InsightsPage />);
    await screen.findByRole("group", { name: "Jobs" });
    fireEvent.change(screen.getByLabelText("Repository"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("Issue type"), { target: { value: "Story" } });
    fireEvent.change(screen.getByLabelText("Whose jobs"), { target: { value: "all" } });
    await waitFor(() => {
      const url = calls.filter((c) => c.path === "/api/insights").length;
      expect(url).toBeGreaterThan(3);
    });
    const last = (globalThis.fetch as unknown as { mock: { calls: [string][] } }).mock.calls
      .map(([u]) => String(u))
      .filter((u) => u.startsWith("/api/insights?"))
      .pop();
    expect(last).toBe("/api/insights?from=2026-10-05&project_id=7&issue_type=Story&scope=all");
  });

  it("explains an empty period", async () => {
    mockApi({
      ...signedInAs(MEMBER),
      "GET /api/insights": { ...DATA, jobs: 0, merged: 0, outcomes: [], weeks: [] },
      "GET /api/insights/options": { projects: [], issue_types: [] },
    });
    renderAt("/insights?scope=all", <InsightsPage />); // a member asking for everyone gets their own
    expect(await screen.findByText(/No jobs started between these dates\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the Board" })).toHaveAttribute("href", "/work");
    expect(screen.queryByRole("group", { name: "Jobs" })).toBeNull();
  });

  it("formats durations", () => {
    expect([null, 0.4, 45, 60, 200, 2880, 3000].map(formatMinutes)).toEqual(["—", "0 min", "45 min", "1 h", "3 h 20 min", "2 d", "2 d 2 h"]);
  });
});
