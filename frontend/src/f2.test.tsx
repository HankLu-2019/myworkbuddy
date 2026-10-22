import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import WorkItemPage from "./features/work/WorkItemPage";
import { renderAt } from "./test-utils";
import { JOB_ID, WORK_ID, hub, job } from "./job-fixtures";
import type { Job, PushPayload, ReviewComment } from "./api";

afterEach(() => vi.unstubAllGlobals());

const COMMENTS: ReviewComment[] = [
  { id: "d1", reply_to: "d1", author: "bob", path: "app/webhooks.py", line: 3, body: "Please log each retry. <img src=x onerror=alert(1)>", url: null },
  { id: "d2", reply_to: "d2", author: "carol", path: null, line: null, body: "Rename the setting.", url: null },
];

describe("Review comments (08 §F2)", () => {
  it("lets the user pick the open comments to address, showing their text as plain text", async () => {
    const pushed = job({ status: "pushed", push: { state: "done", mr_url: "https://git.example.com/mr/7" } as unknown as Job["push"] });
    const calls = hub(pushed, undefined, {
      [`GET /api/jobs/${JOB_ID}/review-comments`]: { items: COMMENTS },
      [`POST /api/jobs/${JOB_ID}/request-changes`]: job({ status: "preparing", review_round: 1, result: null }),
    });
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    fireEvent.click(await screen.findByRole("button", { name: "Address review comments" }));
    const dialog = await screen.findByRole("dialog", { name: "Address review comments" });
    expect(await within(dialog).findByText("Please log each retry. <img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(dialog.querySelector("img")).toBeNull(); // text, never HTML
    fireEvent.click(within(dialog).getByLabelText("General · carol"));
    fireEvent.change(within(dialog).getByLabelText("Your note (optional)"), { target: { value: "Use the logger." } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Address 1 comment" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ feedback: "Use the logger.", comment_ids: ["d1"] }));
  });

  it("lists the replies in the push preview; a reply can be edited or dropped", async () => {
    const preview: PushPayload = {
      target: { host: "gitlab", repo_url: "https://git.example.com/team/payments.git", branch: "mwb/PAY-12", base_branch: "main" },
      commit: { author_name: "Alice", author_email: "a@x", message: "PAY-12" },
      merge_request: { title: "Draft: PAY-12", description: "" },
      gerrit: null,
      jira_comment: null,
      patch_sha256: "f".repeat(64),
      acknowledgements: [],
      review_replies: [
        { comment_id: "d1", body: "Addressed in <commit placeholder>." },
        { comment_id: "d2", body: "Addressed in <commit placeholder>." },
      ],
    };
    const addressed = [
      { id: "d1", reply_to: "d1", path: "app/webhooks.py", line: 3, author: "bob" },
      { id: "d2", reply_to: "d2", path: null, line: null, author: "carol" },
    ];
    const calls = hub(job({ review_round: 1, push: { addressed, addressed_round: 1 } as unknown as Job["push"] }), undefined, {
      [`GET /api/jobs/${JOB_ID}/push-preview`]: preview,
      [`POST /api/jobs/${JOB_ID}/approve-push`]: job({ status: "approved_push" }),
    });
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    fireEvent.click(await screen.findByRole("button", { name: "Review push" }));
    const dialog = await screen.findByRole("dialog", { name: "Review push" });
    fireEvent.change(await within(dialog).findByLabelText("Reply text for app/webhooks.py:3 (bob)"), { target: { value: "Fixed, thanks." } });
    fireEvent.click(within(dialog).getByLabelText("Reply on the change (carol)"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Push branch and open merge request" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")).toBeTruthy());
    expect((calls.find((c) => c.method === "POST")!.body as PushPayload).review_replies).toEqual([{ comment_id: "d1", body: "Fixed, thanks." }]);
  });
});
