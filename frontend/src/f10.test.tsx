import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import RepoDetailPage from "./features/sources/RepoDetailPage";
import { ackLabel } from "./features/work/states";
import { ADMIN, MEMBER, mockApi, renderAt, signedInAs } from "./test-utils";

afterEach(() => vi.unstubAllGlobals());

const NOW = new Date().toISOString();
const repo = (over: Record<string, unknown> = {}) => ({
  id: 7, name: "payments", repo_url: "https://git.example.com/team/payments.git", host: "gitlab", branch: null,
  visibility: "private", status: "ready", error: null, last_ingested_sha: "abc", progress: null, owner_id: 2,
  updated_at: NOW, mine: false, integration_id: 1, files: 10, code_chunks: 40, docs: 2, card_page_id: null, hint: null,
  conventions_md: null, verify_services: false, ...over,
}); // prettier-ignore

describe("Verification with containers (08 §F10)", () => {
  it("lets an admin allow it for a repository", async () => {
    const calls = mockApi({
      ...signedInAs(ADMIN),
      "GET /api/projects/7": repo(),
      "PUT /api/projects/7/verify-services": repo({ verify_services: true }),
    });
    renderAt("/sources/repos/7", <RepoDetailPage />, "/sources/repos/:repoId");
    fireEvent.click(await screen.findByRole("switch", { name: "Let plans for this repository run containers when verifying" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ enabled: true }));
    expect(await screen.findByText("Containers allowed in verification")).toBeInTheDocument();
  });

  it("members see only whether it's allowed", async () => {
    mockApi({ ...signedInAs(MEMBER), "GET /api/projects/7": repo({ verify_services: true }) });
    renderAt("/sources/repos/7", <RepoDetailPage />, "/sources/repos/:repoId");
    expect(await screen.findByText("Plans for this repository may run containers when verifying.")).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /run containers/ })).toBeNull();
  });

  it("names the approval it needs", () => {
    expect(ackLabel("services")).toBe("Verification runs its own Docker, with twice the CPU, memory and disk");
  });
});
