import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import WorkItemPage from "./features/work/WorkItemPage";
import { renderAt } from "./test-utils";
import { JOB_ID, NOW, RESULT, WORK_ID, detail, hub, job } from "./job-fixtures";
import type { Job, PushPayload } from "./api";

afterEach(() => vi.unstubAllGlobals());

const PREVIEW: PushPayload = {
  target: { host: "gitlab", repo_url: "https://git.example.com/team/payments.git", branch: "mwb/PAY-12-retry-webhooks", base_branch: "main" },
  commit: { author_name: "Alice", author_email: "a@x", message: `PAY-12 Retry webhooks\n\nMWB-Job: ${JOB_ID}` },
  merge_request: { title: "Draft: PAY-12: Retry webhooks", description: "Retry on 5xx." },
  gerrit: null,
  jira_comment: { enabled: true, body: "Merge request opened: <link placeholder>" },
  patch_sha256: "f".repeat(64),
  acknowledgements: [],
  review_replies: [],
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
