import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import RepoDetailPage from "./features/sources/RepoDetailPage";
import { ackLabel } from "./features/work/states";
import { MEMBER, mockApi, renderAt, signedInAs } from "./test-utils";

afterEach(() => vi.unstubAllGlobals());

const NOW = new Date().toISOString();
const repo = (over: Record<string, unknown> = {}) => ({
  id: 7, name: "payments", repo_url: "https://git.example.com/team/payments.git", host: "gitlab", branch: null,
  visibility: "private", status: "ready", error: null, last_ingested_sha: "abc", progress: null, owner_id: 2,
  updated_at: NOW, mine: true, integration_id: 1, files: 10, code_chunks: 40, docs: 2, card_page_id: null, hint: null,
  conventions_md: null, ...over,
}); // prettier-ignore

describe("Repository conventions (08 §F4)", () => {
  it("lets the owner save the conventions text", async () => {
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/projects/7": repo(),
      "PUT /api/projects/7/conventions": repo({ conventions_md: "Use structlog." }),
    });
    renderAt("/sources/repos/7", <RepoDetailPage />, "/sources/repos/:repoId");
    const box = await screen.findByLabelText("Conventions");
    expect(screen.getByRole("button", { name: "Save conventions" })).toBeDisabled();
    fireEvent.change(box, { target: { value: "Use structlog." } });
    fireEvent.click(screen.getByRole("button", { name: "Save conventions" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ text: "Use structlog." }));
  });

  it("shows a teammate's conventions read-only, and names the protected-path acknowledgement", async () => {
    mockApi({ ...signedInAs(MEMBER), "GET /api/projects/7": repo({ mine: false, owner_id: 9, conventions_md: "Never touch vendor/." }) });
    renderAt("/sources/repos/7", <RepoDetailPage />, "/sources/repos/:repoId");
    expect(await screen.findByText("Never touch vendor/.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save conventions" })).not.toBeInTheDocument();
    expect(ackLabel("protected_path:migrations/0002.py")).toBe("Changes a path the repository's conventions protect (migrations/0002.py)");
  });
});
