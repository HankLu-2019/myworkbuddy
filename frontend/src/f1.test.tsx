import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import WorkItemPage from "./features/work/WorkItemPage";
import { renderAt } from "./test-utils";
import { JOB_ID, NOW, WORK_ID, hub, job } from "./job-fixtures";
import type { Job } from "./api";

afterEach(() => vi.unstubAllGlobals());

const SHA = "a".repeat(40);
const pushed = (pipeline: Record<string, unknown> | undefined) =>
  job({
    status: "pushed",
    push: { state: "done", pushed_sha: SHA, mr_url: "https://git.example.com/team/payments/-/merge_requests/7", pipeline } as unknown as Job["push"],
  });

describe("Pipeline of the pushed commit (08 §F1)", () => {
  it("offers Fix CI failure when it failed, with an optional note", async () => {
    const failed = {
      sha: SHA, status: "failed", url: "https://git.example.com/pipelines/9", checked_at: NOW,
      failed: [{ name: "test:unit", id: 91, stage: "test", url: null }],
    }; // prettier-ignore
    const calls = hub(pushed(failed), undefined, { [`POST /api/jobs/${JOB_ID}/fix-ci`]: job({ status: "preparing", review_round: 1, result: null }) });
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    expect(await screen.findByText("Pipeline failed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open pipeline" })).toHaveAttribute("href", "https://git.example.com/pipelines/9");
    expect(screen.getByText("Failed: test:unit")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Note for the agent (optional)"), { target: { value: "Keep three retries." } });
    fireEvent.click(screen.getByRole("button", { name: "Fix CI failure" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ note: "Keep three retries." }));
    expect(calls.find((c) => c.method === "POST")?.path).toBe(`/api/jobs/${JOB_ID}/fix-ci`);
  });

  it("shows a passing pipeline without the fix, and says when none ran yet", async () => {
    hub(pushed({ sha: SHA, status: "passed", url: null, failed: [], checked_at: NOW }));
    const { unmount } = renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    expect(await screen.findByText("Pipeline passed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Fix CI failure" })).not.toBeInTheDocument();
    unmount();
    vi.unstubAllGlobals();
    hub(pushed({ sha: "b".repeat(40), status: "failed", url: null, failed: [], checked_at: NOW })); // an older commit's
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    expect(await screen.findByText("No pipeline seen for the pushed commit yet.")).toBeInTheDocument();
  });
});
