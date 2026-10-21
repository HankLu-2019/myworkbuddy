import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import App from "./App";
import ChatPage, { citationHref } from "./pages/ChatPage";
import SearchPage from "./pages/SearchPage";
import DocumentsPage from "./pages/DocumentsPage";
import WikiPage, { wrapInKeep } from "./pages/WikiPage";
import ProjectsPage from "./pages/ProjectsPage";
import ImportsPage from "./pages/ImportsPage";
import SettingsPage from "./pages/SettingsPage";
import AdminPage from "./pages/AdminPage";
import { ADMIN, MEMBER, mockApi, renderAt, signedInAs, sse } from "./test-utils";

afterEach(() => vi.unstubAllGlobals());

const NOW = "2026-10-20T10:00:00Z";
const session = { id: "s1", title: "Spark OOM", source: "chat", visibility: "private", project_id: null, owner_id: 2,
  created_at: NOW, updated_at: NOW };

describe("app shell", () => {
  it("sends signed-out users to the login page and signs in", async () => {
    const calls = mockApi({
      "GET /api/auth/me": new Response(JSON.stringify({ detail: "Not signed in" }), { status: 401 }),
      "POST /api/auth/login": { user: MEMBER, csrf_token: "c" },
      "GET /api/chat/sessions": [],
      "GET /api/chat/shared": [],
      "GET /api/projects": [],
    });
    renderAt("/chat", <App />);
    fireEvent.change(await screen.findByLabelText("Username"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Ask about your work")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/auth/login")?.body).toEqual({ username: "alice", password: "pw" });
    expect(screen.queryByText("Users")).not.toBeInTheDocument();
  });

  it("shows login errors", async () => {
    mockApi({
      "GET /api/auth/me": new Response("{}", { status: 401 }),
      "POST /api/auth/login": new Response(JSON.stringify({ detail: "Wrong username or password" }), { status: 401 }),
    });
    renderAt("/login", <App />);
    fireEvent.change(await screen.findByLabelText("Username"), { target: { value: "a" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "b" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Wrong username or password")).toBeInTheDocument();
  });

  it("shows the admin link to admins and signs out", async () => {
    const calls = mockApi({ ...signedInAs(ADMIN), "GET /api/knowledge/search": [], "POST /api/auth/logout": undefined });
    renderAt("/search", <App />);
    expect(await screen.findByText("Users")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Sign out"));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/auth/logout")).toBe(true));
    expect(await screen.findByLabelText("Username")).toBeInTheDocument();
  });
});

describe("chat", () => {
  it("maps citations to the right pages", () => {
    const base = { n: 1, document_id: 1, title: "t", visibility: "private" as const, meta: {} };
    expect(citationHref({ ...base, source_type: "wiki", source_ref: "wiki:7" })).toBe("/wiki/7");
    expect(citationHref({ ...base, source_type: "episode", source_ref: "session:abc" })).toBe("/chat/abc");
    expect(citationHref({ ...base, source_type: "upload", source_ref: "upload:x" })).toBe("/documents");
    expect(citationHref({ ...base, source_type: "code", source_ref: "1:a.py" })).toBe("/projects");
  });

  it("streams a reply and renders saved messages with citations", async () => {
    let saved: unknown[] = [];
    mockApi({
      ...signedInAs(MEMBER),
      "GET /api/chat/sessions": [session],
      "GET /api/chat/shared": [{ ...session, id: "s9", title: "Team chat", owner_id: 3 }],
      "GET /api/projects": [{ id: 4, name: "etl" }],
      "GET /api/chat/sessions/s1": session,
      "GET /api/chat/sessions/s1/messages": () => saved,
      "POST /api/chat/sessions/s1/messages": () => {
        saved = [
          { id: 1, role: "user", content: "why OOM?", created_at: NOW, citations: [], tools: [] },
          { id: 2, role: "assistant", content: "Raise memory [1].", created_at: NOW, tools: [{ name: "search_knowledge", args: { query: "oom" }, status: "done" }],
            citations: [{ n: 1, document_id: 1, title: "OOM runbook", source_type: "wiki", source_ref: "wiki:3", visibility: "private", meta: {} }] },
        ];
        return sse([["tool_call", { name: "search_knowledge", args: { query: "oom" } }], ["delta", { text: "Raise memory [1]." }], ["done", {}]]);
      },
      "PATCH /api/chat/sessions/s1": { ...session, visibility: "public" },
      "POST /api/chat/sessions/s1/close": { status: "summarizing" },
    });
    renderAt("/chat/s1", <ChatPage />, "/chat/:sessionId");
    expect(await screen.findByText("Team chat")).toBeInTheDocument();
    const box = await screen.findByLabelText("Message");
    fireEvent.change(box, { target: { value: "why OOM?" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(await screen.findByText("OOM runbook")).toBeInTheDocument();
    expect(screen.getByText("Raise memory [1].")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Share with team"));
    fireEvent.click(screen.getByText("Save to memory"));
    expect(await screen.findByText("Summary queued")).toBeInTheDocument();
  });

  it("shows a model error and lets the user send again", async () => {
    mockApi({
      ...signedInAs(MEMBER),
      "GET /api/chat/sessions": [session],
      "GET /api/chat/shared": [],
      "GET /api/projects": [],
      "GET /api/chat/sessions/s1": session,
      "GET /api/chat/sessions/s1/messages": [],
      "POST /api/chat/sessions/s1/messages": () => sse([["error", { message: "The model call failed: 403" }], ["done", {}]]),
    });
    renderAt("/chat/s1", <ChatPage />, "/chat/:sessionId");
    const box = await screen.findByLabelText("Message");
    fireEvent.change(box, { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("The model call failed: 403")).toBeInTheDocument();
    fireEvent.change(box, { target: { value: "again" } });
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });
});

describe("knowledge pages", () => {
  it("searches with a scope", async () => {
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/knowledge/search": [{ document_id: 1, title: "Spark OOM", source_type: "upload", source_ref: "upload:x",
        visibility: "public", owner_id: 3, project_id: null, excerpt: "OOMKilled", meta: {}, score: 0.03 }],
    });
    renderAt("/search", <SearchPage />);
    fireEvent.change(await screen.findByLabelText("Search query"), { target: { value: "OOMKilled" } });
    fireEvent.change(screen.getByLabelText("Scope"), { target: { value: "documents" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Spark OOM")).toBeInTheDocument();
    expect(screen.getByText("Team")).toBeInTheDocument();
    expect(calls.at(-1)?.path).toBe("/api/knowledge/search");
  });

  it("lists, shares and deletes documents", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/knowledge/documents": [{ id: 5, title: "pm.md", source_type: "upload", source_ref: "upload:pm.md",
        visibility: "private", owner_id: 2, project_id: null, chunks: 3, updated_at: NOW, mine: true }],
      "POST /api/knowledge/documents/5/visibility": {},
      "DELETE /api/knowledge/documents/5": undefined,
      "POST /api/knowledge/documents": {},
      "PUT /api/knowledge/documents/5/file": {},
    });
    const { container } = renderAt("/documents", <DocumentsPage />);
    expect(await screen.findByText("pm.md")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Share with team"));
    fireEvent.click(screen.getByText("Delete"));
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(["# x"], "new.md")] } });
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/knowledge/documents")).toBe(true));
    expect(calls.some((c) => c.method === "DELETE")).toBe(true);
    // Replace uploads into the same document.
    fireEvent.click(screen.getByText("Replace file"));
    fireEvent.change(screen.getByLabelText("Replacement file"), { target: { files: [new File(["# v2"], "pm.md")] } });
    await waitFor(() =>
      expect(calls.some((c) => c.method === "PUT" && c.path === "/api/knowledge/documents/5/file")).toBe(true),
    );
  });

  it("views, edits, publishes and shows history of a wiki page", async () => {
    const page = { id: 3, path: "runbooks/oom.md", title: "OOM runbook", visibility: "private", owner_id: 2,
      updated_at: NOW, mine: true, content: "# OOM runbook\n\nRaise memory.", head_sha: "abc", can_edit: true };
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/wiki/pages": [page, { ...page, id: 4, path: "team.md", visibility: "public", mine: false }],
      "GET /api/wiki/pages/3": page,
      "PUT /api/wiki/pages/3": page,
      "POST /api/wiki/pages/3/visibility": { ...page, visibility: "public" },
      "GET /api/wiki/pages/3/history": [{ sha: "abc123", author: "alice", date: NOW, message: "Create runbooks/oom.md" }],
      "GET /api/wiki/pages/3/commits/abc123": { diff: "+Raise memory.\n-old" },
    });
    renderAt("/wiki/3", <WikiPage />, "/wiki/:pageId");
    expect(await screen.findByText("Raise memory.")).toBeInTheDocument();
    expect(screen.getByText("team.md")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Edit"));
    const editor = screen.getByLabelText<HTMLTextAreaElement>("Page content");
    fireEvent.change(editor, { target: { value: "# New\nNever on Fridays" } });
    editor.setSelectionRange(8, 8); // cursor on the second line
    fireEvent.click(screen.getByText("Keep block"));
    expect(editor.value).toBe("# New\n<!-- mwb:keep -->\nNever on Fridays\n<!-- /mwb:keep -->");
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
        content: "# New\n<!-- mwb:keep -->\nNever on Fridays\n<!-- /mwb:keep -->",
        base_sha: "abc",
      }),
    );
    fireEvent.click(screen.getByText("Publish to team"));
    fireEvent.click(await screen.findByText("History"));
    fireEvent.click(await screen.findByText("Create runbooks/oom.md"));
    expect(await screen.findByText("+Raise memory.")).toBeInTheDocument();
  });

  it("shows an edit conflict with both versions and lets the user discard", async () => {
    const page = { id: 3, path: "notes.md", title: "Notes", visibility: "private", owner_id: 2,
      updated_at: NOW, mine: true, content: "# Notes\n\nmine", head_sha: "abc", can_edit: true };
    const conflict = new Response(
      JSON.stringify({ detail: "This page changed since you opened it", code: "wiki_conflict", current_sha: "def",
        current_content: "# Notes\n\nsomeone else's text" }),
      { status: 409 },
    );
    mockApi({
      ...signedInAs(MEMBER),
      "GET /api/wiki/pages": [page],
      "GET /api/wiki/pages/3": page,
      "PUT /api/wiki/pages/3": () => conflict,
    });
    renderAt("/wiki/3", <WikiPage />, "/wiki/:pageId");
    fireEvent.click(await screen.findByText("Edit"));
    fireEvent.change(screen.getByLabelText("Page content"), { target: { value: "my draft" } });
    fireEvent.click(screen.getByText("Save"));
    expect(await screen.findByRole("alertdialog", { name: "Edit conflict" })).toBeInTheDocument();
    expect(screen.getByText(/someone else's text/)).toBeInTheDocument();
    expect(screen.getAllByText("my draft").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText("Discard my edits"));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });

  it("wraps a selection in keep markers", () => {
    expect(wrapInKeep("a\nbcd\ne", 2, 5)).toEqual({ text: "a\n<!-- mwb:keep -->\nbcd\n<!-- /mwb:keep -->\ne", cursor: 42 });
    expect(wrapInKeep("only", 2, 2).text).toBe("<!-- mwb:keep -->\nonly\n<!-- /mwb:keep -->");
  });

  it("shows librarian changes and creates pages", async () => {
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/wiki/pages": [],
      "GET /api/wiki/librarian/changes": [{ sha: "s", date: NOW, message: "Librarian: update decisions.md", diff: "+x" }],
      "GET /api/wiki/librarian/runs": [
        { id: 1, started_at: NOW, status: "partial", error: null,
          pages_rejected: [{ path: "me/how-i-work.md", reason: "rewrite drops 3 of 5 headings", diff: "-## Tools" }] },
        { id: 2, started_at: NOW, status: "failed", error: "gateway down", pages_rejected: [] },
      ],
      "POST /api/wiki/librarian/run": { queued: true },
      "POST /api/wiki/pages": { id: 9 },
      "GET /api/wiki/pages/9": { id: 9, path: "a.md", title: "A", visibility: "private", owner_id: 2, updated_at: NOW,
        mine: true, content: "# A", head_sha: null, can_edit: true },
    });
    renderAt("/wiki", <WikiPage />);
    expect(await screen.findByText("Librarian: update decisions.md")).toBeInTheDocument();
    expect(await screen.findByText("Not applied: rewrite drops 3 of 5 headings")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("gateway down");
    fireEvent.click(screen.getByText("Update from recent sessions now"));
    expect(await screen.findByText("Queued")).toBeInTheDocument();
    fireEvent.click(screen.getByText("New page"));
    fireEvent.change(screen.getByLabelText("Path"), { target: { value: "a.md" } });
    fireEvent.click(screen.getByText("Create page"));
    await waitFor(() => expect(calls.find((c) => c.method === "POST" && c.path === "/api/wiki/pages")?.body)
      .toMatchObject({ path: "a.md" }));
  });

  it("adds and manages projects", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/projects": [{ id: 1, name: "etl", repo_url: "https://g/etl.git", host: "gitlab", branch: null,
        visibility: "private", status: "failed", error: "auth required", last_ingested_sha: null, owner_id: 2,
        updated_at: NOW, mine: true, progress: null },
        { id: 2, name: "api", repo_url: "https://g/api.git", host: "gitlab", branch: null, visibility: "private",
          status: "running", error: null, last_ingested_sha: null, owner_id: 2, updated_at: NOW, mine: true,
          progress: "Indexing code 40 of 120" }],
      "POST /api/projects": { id: 3, notice: "Changed the URL to https://g/a/new: Gerrit needs the /a/ prefix" },
      "POST /api/projects/1/ingest": { queued: true },
      "POST /api/projects/1/visibility": {},
      "DELETE /api/projects/1": undefined,
    });
    renderAt("/projects", <ProjectsPage />);
    expect(await screen.findByText("auth required")).toBeInTheDocument();
    expect(screen.getByText("Indexing code 40 of 120")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Repository URL"), { target: { value: "https://g/new.git" } });
    fireEvent.change(screen.getByLabelText("Host"), { target: { value: "gerrit" } });
    fireEvent.click(screen.getByText("Add repository"));
    expect(await screen.findByRole("status")).toHaveTextContent("Gerrit needs the /a/ prefix");
    fireEvent.click(screen.getAllByText("Re-index")[0]);
    fireEvent.click(screen.getAllByText("Share code index")[0]);
    fireEvent.click(screen.getAllByText("Remove")[0]);
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));
    expect(calls.find((c) => c.method === "POST" && c.path === "/api/projects")?.body).toMatchObject({ host: "gerrit" });
  });

  it("imports Claude Code transcripts", async () => {
    mockApi({
      ...signedInAs(MEMBER),
      "POST /api/imports/claude-code": [
        { filename: "a.jsonl", session_id: "s1", title: "Debug gerrit", status: "imported", error: null },
        { filename: "b.jsonl", session_id: null, title: null, status: "failed", error: "Not a transcript" },
      ],
    });
    const { container } = renderAt("/imports", <ImportsPage />);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(["{}"], "a.jsonl")] } });
    expect(await screen.findByText("Debug gerrit")).toBeInTheDocument();
    expect(screen.getByText("Not a transcript")).toBeInTheDocument();
  });
});

describe("settings and admin", () => {
  it("saves tokens, lists sign-ins and changes the password", async () => {
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/secrets": [
        { kind: "gitlab_token", label: "GitLab personal access token", is_set: true, status: "set", updated_at: NOW },
        { kind: "gerrit_username", label: "Gerrit username", is_set: false, status: "not_set", updated_at: null },
        { kind: "jira_token", label: "Jira personal access token", is_set: true, status: "unreadable", updated_at: NOW },
      ],
      "GET /api/auth/sessions": [
        { id: 1, created_at: NOW, last_seen_at: NOW, expires_at: NOW, ip: "10.0.0.1", current: true,
          user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/130.0 Safari/537.36" },
        { id: 2, created_at: NOW, last_seen_at: NOW, expires_at: NOW, ip: "10.0.0.2", current: false,
          user_agent: "Mozilla/5.0 (Windows NT 10.0) Gecko/20100101 Firefox/131.0" },
      ],
      "POST /api/auth/sessions/sign-out-others": { signed_out: 1 },
      "GET /api/usage": { user_id: 2, spend_usd: 12.5, budget_usd: 50, budget_reset_at: NOW, fetched_at: NOW },
      "PUT /api/secrets/gerrit_username": undefined,
      "DELETE /api/secrets/gitlab_token": undefined,
      "POST /api/auth/password": undefined,
    });
    renderAt("/settings", <SettingsPage />);
    fireEvent.change(await screen.findByLabelText("Gerrit username"), { target: { value: "alice" } });
    fireEvent.click(screen.getAllByText("Save")[1]);
    fireEvent.click(screen.getAllByText("Clear")[0]);
    expect(screen.getByText("Can't be read any more. Enter it again.")).toBeInTheDocument();
    expect(await screen.findByText(/USD 12.50 of USD 50.00 used/)).toBeInTheDocument();
    expect(screen.getByLabelText("Share of monthly AI budget used")).toHaveAttribute("value", "25");
    expect(await screen.findByText("Chrome on macOS")).toBeInTheDocument();
    expect(screen.getByText("Firefox on Windows")).toBeInTheDocument();
    expect(screen.getByText("This device")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Sign out other sessions"));
    expect(await screen.findByText("Signed out 1 other session.")).toBeInTheDocument();
    expect(screen.getByText(/signs you out everywhere/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "old-password" } });
    fireEvent.change(screen.getByLabelText("New password (10+ characters)"), { target: { value: "new-password-1" } });
    fireEvent.click(screen.getByText("Change password"));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/auth/password")).toBe(true));
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ value: "alice" });
  });

  it("adds users with a one-time password, resets and disables", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const calls = mockApi({
      ...signedInAs(ADMIN),
      "GET /api/admin/users": [ADMIN, { ...MEMBER, must_change_password: true, last_login_at: null }],
      "POST /api/admin/users": { ...MEMBER, username: "bob", initial_password: "Abc23defGhk45mnP" },
      "POST /api/admin/users/2/reset-password": { ...MEMBER, initial_password: "Zz23defGhk45mnPq" },
      "PATCH /api/admin/users/2": MEMBER,
      "GET /api/admin/usage": [
        { user_id: 1, spend_usd: null, budget_usd: 50, budget_reset_at: null, fetched_at: null },
        { user_id: 2, spend_usd: 3.2, budget_usd: 20, budget_reset_at: NOW, fetched_at: NOW },
      ],
    });
    renderAt("/admin", <AdminPage />);
    expect(await screen.findByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("Must change password")).toBeInTheDocument();
    expect(await screen.findByText("USD 3.20")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Change budget for alice"));
    fireEvent.change(screen.getByLabelText("Monthly budget for alice (USD)"), { target: { value: "75" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ llm_budget_usd: "75" }),
    );
    expect(screen.queryByLabelText("Initial password")).not.toBeInTheDocument(); // generated, not chosen
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "bob" } });
    fireEvent.click(screen.getByText("Add user"));
    expect(await screen.findByLabelText("Password")).toHaveTextContent("Abc23defGhk45mnP");
    fireEvent.click(screen.getByText("Copy"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("Abc23defGhk45mnP"));
    fireEvent.click(screen.getByText("Done"));
    fireEvent.click(screen.getAllByText("Reset password")[1]);
    expect(await screen.findByLabelText("Password")).toHaveTextContent("Zz23defGhk45mnPq");
    fireEvent.click(screen.getByText("Disable"));
    fireEvent.change(screen.getByLabelText("Role of alice"), { target: { value: "admin" } });
    await waitFor(() => expect(calls.filter((c) => c.method === "PATCH")).toHaveLength(3));
    expect(calls.find((c) => c.method === "POST" && c.path === "/api/admin/users")?.body).toEqual({
      username: "bob", email: null, display_name: null, role: "member",
    });
  });

  it("offboards a user after showing what moves and what is deleted", async () => {
    const calls = mockApi({
      ...signedInAs(ADMIN),
      "GET /api/admin/users": [ADMIN, MEMBER],
      "GET /api/admin/usage": [],
      "GET /api/admin/users/2/offboarding-preview": {
        transfer_to: 1, duplicate_projects: [7],
        transfer: { documents: 3, wiki_pages: 1, projects: 0, chat_sessions: 0, episodes: 0 },
        delete: { documents: 12, wiki_pages: 2, projects: 1, chat_sessions: 4, episodes: 0, artifacts: 0, secrets: 1, sign_ins: 2 },
      },
      "DELETE /api/admin/users/2": { transfer_to: 1, transfer: {}, delete: {}, duplicate_projects: [] },
    });
    renderAt("/admin", <AdminPage />);
    fireEvent.click(await screen.findByText("Delete"));
    expect(await screen.findByText("Moves to the new owner: 3 documents, 1 wiki pages.")).toBeInTheDocument();
    expect(screen.getByText(/Deleted: 12 documents, 2 wiki pages, 1 repositories, 4 chats, 1 saved tokens, 2 sign-ins/)).toBeInTheDocument();
    expect(screen.getByText(/already added by the new owner/)).toBeInTheDocument();
    const confirm = screen.getByText("Delete user");
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Type alice to confirm"), { target: { value: "alice" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));
    expect(calls.find((c) => c.path === "/api/admin/users/2/offboarding-preview")).toBeTruthy();
  });

  it("makes a user with a one-time password choose a new one first", async () => {
    const calls = mockApi({
      "GET /api/auth/me": { user: { ...MEMBER, must_change_password: true }, csrf_token: "csrf-1" },
      "POST /api/auth/password": undefined,
      "GET /api/chat/sessions": [],
      "GET /api/chat/shared": [],
      "GET /api/projects": [],
    });
    renderAt("/chat", <App />);
    expect(await screen.findByText("Choose your password")).toBeInTheDocument();
    expect(screen.queryByText("Search")).not.toBeInTheDocument(); // no navigation until it's changed
    fireEvent.change(screen.getByLabelText("One-time password"), { target: { value: "Abc23defGhk45mnP" } });
    fireEvent.change(screen.getByLabelText("New password (10+ characters)"), { target: { value: "my-own-password" } });
    fireEvent.change(screen.getByLabelText("Repeat new password"), { target: { value: "my-own-passwor" } });
    expect(screen.getByText("The passwords don't match.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Repeat new password"), { target: { value: "my-own-password" } });
    fireEvent.click(screen.getByText("Save and continue"));
    expect(await screen.findByText("Search")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/auth/password")?.body).toEqual({
      current_password: "Abc23defGhk45mnP", new_password: "my-own-password",
    });
  });
});
