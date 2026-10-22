import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import WorkItemPage from "./features/work/WorkItemPage";
import { renderAt } from "./test-utils";
import { JOB_ID, NOW, PATCH, WORK_ID, hub, job } from "./job-fixtures";

afterEach(() => vi.unstubAllGlobals());

const ART = "77777777-0000-4000-8000-000000000007";

describe("The agent's question (08 §F3)", () => {
  it("shows the question as the agent's text with the changes so far, and sends the answer", async () => {
    const asking = job({
      status: "needs_input", result: null, questions: 1,
      question: { text: "Retry **429** too? <img src=https://evil.example/x.png>", number: 1, patch_artifact_id: ART, asked_at: NOW },
    }); // prettier-ignore
    const calls = hub(asking, undefined, {
      [`GET /api/artifacts/${ART}`]: () => new Response(PATCH),
      [`POST /api/jobs/${JOB_ID}/answer`]: job({ status: "running", result: null, question: null }),
    });
    renderAt(`/work/${WORK_ID}/job`, <WorkItemPage tab="job" />, "/work/:workId/job");
    expect(await screen.findByRole("heading", { name: /The agent has a question\s*Written by the agent/ })).toBeInTheDocument();
    expect(screen.getByText("429").tagName).toBe("STRONG");
    expect(document.querySelector('img[src^="https://evil"]')).toBeNull(); // safe Markdown: no remote images
    expect(screen.getAllByText("Question from the agent").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(await screen.findByLabelText("Changes so far")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send answer" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Your answer"), { target: { value: "Only 5xx." } });
    fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ answer: "Only 5xx." }));
  });
});
