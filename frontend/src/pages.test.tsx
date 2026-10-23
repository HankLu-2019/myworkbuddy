import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import App from "./App";
import ChatPage, { chatErrorText, citationHref } from "./features/chat/ChatPage";
import SearchPage from "./features/search/SearchPage";
import DocumentsPage from "./features/sources/DocumentsTab";
import WikiPage, { wrapInKeep } from "./features/wiki/WikiPage";
import ProjectsPage from "./features/sources/ReposTab";
import ImportsPage from "./features/sources/ImportsTab";
import SettingsPage from "./features/settings/SettingsPage";
import AdminPage from "./features/admin/UsersPage";
import SystemPage, { bytes } from "./features/admin/SystemPage";
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
      "POST /api/auth/login": new Response(JSON.stringify({ detail: "Wrong username or password", code: "invalid_credentials" }), { status: 401 }),
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
    expect(citationHref({ ...base, source_type: "upload", source_ref: "upload:x" })).toBe("/sources/documents");
    expect(citationHref({ ...base, source_type: "code", source_ref: "1:a.py" })).toBe("/sources/repos");
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

  it("shows partial and failed replies from the saved history", async () => {
    let saved: unknown[] = [];
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/chat/sessions": [session],
      "GET /api/chat/shared": [],
      "GET /api/projects": [],
      "GET /api/chat/sessions/s1": session,
      "GET /api/chat/sessions/s1/messages": () => saved,
      "POST /api/chat/sessions/s1/messages": () => {
        saved = [
          { id: 1, role: "user", content: "a", created_at: NOW, citations: [], tools: [] },
          { id: 2, role: "assistant", content: "The executor ran out of", created_at: NOW, citations: [], tools: [],
            status: "partial", error_code: "llm_unavailable", cost_usd: 0.031 },
          { id: 3, role: "user", content: "b", created_at: NOW, citations: [], tools: [] },
          { id: 4, role: "assistant", content: "You've reached your monthly AI budget (USD 50.00). An admin can raise it.",
            created_at: NOW, citations: [], tools: [], status: "failed", error_code: "budget_exceeded" },
        ];
        return sse([
          ["error", { code: "budget_exceeded", message: "You've reached your monthly AI budget (USD 50.00). An admin can raise it." }],
          ["done", { message_id: 4, citations: [] }],
        ]);
      },
    });
    renderAt("/chat/s1", <ChatPage />, "/chat/:sessionId");
    const box = await screen.findByLabelText("Message");
    fireEvent.change(box, { target: { value: "b" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText(/Incomplete — the model call failed/)).toBeInTheDocument();
    expect(screen.getByText("The executor ran out of")).toBeInTheDocument();
    expect(screen.getByLabelText("Cost of this reply")).toHaveTextContent("$0.03");
    // The failed reply is saved, so the message appears once (no duplicate error note).
    await waitFor(() => expect(screen.getAllByText(/monthly AI budget/)).toHaveLength(1));
    // Retry resends the user message the failed reply answered.
    fireEvent.click(screen.getAllByText("Retry")[1]);
    await waitFor(() =>
      expect(calls.filter((c) => c.method === "POST").map((c) => c.body)).toEqual([{ content: "b" }, { content: "b" }]),
    );
  });

  it("explains a busy chat", async () => {
    mockApi({
      ...signedInAs(MEMBER),
      "GET /api/chat/sessions": [session],
      "GET /api/chat/shared": [],
      "GET /api/projects": [],
      "GET /api/chat/sessions/s1": session,
      "GET /api/chat/sessions/s1/messages": [],
      "POST /api/chat/sessions/s1/messages": new Response(
        JSON.stringify({ detail: "This chat is still answering.", code: "session_busy" }),
        { status: 409 },
      ),
    });
    renderAt("/chat/s1", <ChatPage />, "/chat/:sessionId");
    const box = await screen.findByLabelText("Message");
    fireEvent.change(box, { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("A reply is still being written in another tab.")).toBeInTheDocument();
  });

  it("maps error codes to messages", () => {
    expect(chatErrorText("budget_exceeded", undefined)).toMatch(/monthly AI budget/);
    expect(chatErrorText("llm_unavailable", undefined)).toMatch(/isn't answering/);
    expect(chatErrorText("internal", "")).toBe("Something went wrong. Try again.");
    expect(chatErrorText(undefined, "custom")).toBe("custom");
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
    renderAt("/sources/documents", <DocumentsPage />);
    expect(await screen.findByText("pm.md")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Share with team" }));
    const share = await screen.findByRole("dialog", { name: "Share pm.md with the team?" });
    expect(share).toHaveTextContent("becomes searchable by everyone");
    fireEvent.click(within(share).getByRole("button", { name: "Share with team" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/knowledge/documents/5/visibility")).toBe(true));
    expect(await screen.findByRole("button", { name: "Undo" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete pm.md?" });
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    fireEvent.click(within(confirm).getByRole("button", { name: "Delete document" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));
    fireEvent.change(screen.getByLabelText("Upload files"), { target: { files: [new File(["# x"], "new.md")] } });
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/knowledge/documents")).toBe(true));
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
    fireEvent.click(screen.getByRole("button", { name: "Share with team" }));
    const share = await screen.findByRole("dialog", { name: "Share runbooks/oom.md with the team?" });
    fireEvent.click(within(share).getByRole("button", { name: "Share with team" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/wiki/pages/3/visibility")?.body).toEqual({ visibility: "public" }));
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
    expect(await screen.findByRole("alert", { name: "Edit conflict" })).toBeInTheDocument();
    expect(screen.getByText(/someone else's text/)).toBeInTheDocument();
    expect(screen.getAllByText("my draft").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText("Discard my edits"));
    await waitFor(() => expect(screen.queryByRole("alert", { name: "Edit conflict" })).not.toBeInTheDocument());
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
      "GET /api/integrations": [
        { id: 1, kind: "gitlab", name: "GitLab", base_url: "https://g", help: "PAT",
          credentials: [{ kind: "token", label: "Personal access token", status: "ok", verified_at: NOW, last_error: null, updated_at: NOW }] },
        { id: 2, kind: "gerrit", name: "Gerrit", base_url: "https://review", help: "HTTP password",
          credentials: [
            { kind: "username", label: "Username", status: "not_set", verified_at: null, last_error: null, updated_at: null },
            { kind: "password", label: "HTTP password", status: "not_set", verified_at: null, last_error: null, updated_at: null },
          ] },
      ],
    });
    renderAt("/sources/repos", <ProjectsPage />);
    expect(await screen.findByText("auth required")).toBeInTheDocument();
    expect(screen.getByText("Indexing code 40 of 120")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add repository" }));
    const dialog = await screen.findByRole("dialog", { name: "Add repository" });
    fireEvent.change(await within(dialog).findByLabelText("Where it lives"), { target: { value: "2" } });
    expect(await within(dialog).findByText(/You need your Gerrit username and http password first/)).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Add token" })).toHaveAttribute(
      "href",
      "/settings/integrations?focus=2:username&back=%2Fsources%2Frepos%3Fadd%3D1",
    );
    expect(within(dialog).getByRole("button", { name: "Add repository" })).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText("Where it lives"), { target: { value: "1" } });
    fireEvent.change(within(dialog).getByLabelText("Repository URL"), { target: { value: "https://g/new.git" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add repository" }));
    expect(await screen.findByText("Indexing started")).toBeInTheDocument();
    expect(screen.getByText(/Gerrit needs the \/a\/ prefix/)).toBeInTheDocument();
    fireEvent.click(screen.getAllByText("Re-index")[0]);
    fireEvent.click(screen.getAllByText("Share code index")[0]);
    const share = await screen.findByRole("dialog", { name: "Share etl with the team?" });
    expect(share).toHaveTextContent("Your project card stays private");
    fireEvent.click(within(share).getByRole("button", { name: "Share with team" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/projects/1/visibility")).toBe(true));
    fireEvent.click(screen.getAllByRole("button", { name: "Remove" })[0]);
    const confirm = await screen.findByRole("dialog", { name: "Remove etl?" });
    fireEvent.click(within(confirm).getByRole("button", { name: "Remove repository" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));
    expect(calls.find((c) => c.method === "POST" && c.path === "/api/projects")?.body).toMatchObject({
      integration_id: 1,
      repo_url: "https://g/new.git",
    });
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
      "GET /api/integrations": [
        { id: 1, kind: "gitlab", name: "GitLab", base_url: "https://g", help: "Scopes: read_api and read_repository.",
          credentials: [{ kind: "token", label: "Personal access token", status: "ok", verified_at: NOW, last_error: null, updated_at: NOW }] },
        { id: 2, kind: "gerrit", name: "Gerrit", base_url: "https://review", help: "HTTP password",
          credentials: [
            { kind: "username", label: "Username", status: "not_set", verified_at: null, last_error: null, updated_at: null },
            { kind: "password", label: "HTTP password", status: "not_set", verified_at: null, last_error: null, updated_at: null },
          ] },
        { id: 3, kind: "jira", name: "Jira", base_url: "https://jira", help: "PAT",
          credentials: [{ kind: "token", label: "Personal access token", status: "unreadable", verified_at: null, last_error: null, updated_at: NOW }] },
      ],
      "GET /api/auth/sessions": [
        { id: 1, created_at: NOW, last_seen_at: NOW, expires_at: NOW, ip: "10.0.0.1", current: true,
          user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/130.0 Safari/537.36" },
        { id: 2, created_at: NOW, last_seen_at: NOW, expires_at: NOW, ip: "10.0.0.2", current: false,
          user_agent: "Mozilla/5.0 (Windows NT 10.0) Gecko/20100101 Firefox/131.0" },
      ],
      "POST /api/auth/sessions/sign-out-others": { signed_out: 1 },
      "GET /api/usage": { user_id: 2, spend_usd: 12.5, budget_usd: 50, budget_reset_at: NOW, fetched_at: NOW, chat_usd_month: 0.42 },
      "PUT /api/integrations/2/credentials/username": undefined,
      "DELETE /api/integrations/1/credentials/token": undefined,
      "POST /api/integrations/1/test": { ok: false, status: "invalid", message: "GitLab rejected the credential (401)" },
      "POST /api/auth/password": undefined,
    });
    const { router } = renderAt("/settings/integrations?focus=2", <SettingsPage />, "/settings/:tab");
    const gerrit = await screen.findByLabelText("Gerrit username");
    await waitFor(() => expect(gerrit).toHaveFocus());
    expect(screen.getByText(/Scopes: read_api and read_repository/)).toBeInTheDocument();
    const gerritCard = screen.getByRole("region", { name: "Gerrit" });
    expect(within(gerritCard).getByRole("button", { name: "Test" })).toBeDisabled();
    expect(within(gerritCard).getByText("Save username and http password first.")).toBeInTheDocument();
    fireEvent.change(gerrit, { target: { value: "alice" } });
    fireEvent.click(within(gerritCard).getAllByRole("button", { name: "Save" })[0]);
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ value: "alice" }));
    const gitlabCard = screen.getByRole("region", { name: "GitLab" });
    expect(within(gitlabCard).getByText("Verified")).toBeInTheDocument();
    fireEvent.click(within(gitlabCard).getByRole("button", { name: "Test" }));
    expect(await within(gitlabCard).findByText("GitLab rejected the credential (401)")).toBeInTheDocument();
    fireEvent.click(within(gitlabCard).getByRole("button", { name: "Clear" }));
    const clear = await screen.findByRole("dialog", { name: "Clear your GitLab personal access token?" });
    fireEvent.click(within(clear).getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));
    expect(screen.getByText("Can't be read any more. Enter it again.")).toBeInTheDocument();
    await router.navigate("/settings/usage");
    expect(await screen.findByText(/USD 12.50 of USD 50.00 used/)).toBeInTheDocument();
    expect(screen.getByText("$0.42")).toBeInTheDocument();
    expect(screen.getByLabelText("Share of monthly AI budget used")).toHaveAttribute("value", "25");
    await router.navigate("/settings/sessions");
    expect(await screen.findByText("Chrome on macOS")).toBeInTheDocument();
    expect(screen.getByText("Firefox on Windows")).toBeInTheDocument();
    expect(screen.getByText("This device")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Sign out other sessions"));
    expect(await screen.findByText("Signed out 1 other session.")).toBeInTheDocument();
    await router.navigate("/settings/password");
    await screen.findByLabelText("Current password");
    expect(screen.getByText(/signs you out everywhere/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "old-password" } });
    fireEvent.change(screen.getByLabelText("New password (10+ characters)"), { target: { value: "new-password-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/auth/password")).toBe(true));
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
    fireEvent.click(screen.getAllByRole("button", { name: "Reset password" })[1]);
    const confirmReset = await screen.findByRole("dialog", { name: "Reset the password for alice?" });
    fireEvent.click(within(confirmReset).getByRole("button", { name: "Reset password" }));
    expect(await screen.findByLabelText("Password")).toHaveTextContent("Zz23defGhk45mnPq");
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
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
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    expect(await screen.findByText("Moves to the new owner: 3 documents, 1 wiki pages.")).toBeInTheDocument();
    expect(screen.getByText(/Deleted: 12 documents, 2 wiki pages, 1 repositories, 4 chats, 1 saved tokens, 2 sign-ins/)).toBeInTheDocument();
    expect(screen.getByText(/already added by the new owner/)).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Delete user" });
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
    expect((await screen.findAllByRole("link", { name: "Search" })).length).toBeGreaterThan(0);
    expect(calls.find((c) => c.path === "/api/auth/password")?.body).toEqual({
      current_password: "Abc23defGhk45mnP", new_password: "my-own-password",
    });
  });
});

describe("system page", () => {
  const SYSTEM = {
    checks: { database: "ok", llm_gateway: "ok", worker: "worker: last heartbeat 300 s ago" },
    alerts: ["worker: last heartbeat 300 s ago", "Last successful backup was 30 h ago"],
    worker: { heartbeat: { at: NOW, host: "worker-1" }, age_s: 300 },
    jobs: {
      queues: [{ queue: "default", status: "todo", count: 3 }],
      failed_24h: [{ id: 41, queue: "default", task: "ingest_project", attempts: 1, failed_at: NOW }],
      periodic: [{ task: "librarian_tick", last_at: NOW }],
    },
    backup: { status: "ok", last_success_at: NOW, snapshot_id: "abcdef1234", sizes: { "mwb.dump": 2048 } },
    backup_drill: { ok: true, at: NOW, note: "monthly" },
    disk: { path: "/data", total_bytes: 100 * 1024 ** 3, free_bytes: 40 * 1024 ** 3, free_ratio: 0.4 },
    disk_guard: { ok: true },
    wiki_reconcile: { missing: [] },
    embeddings: { failed: 1, sample: [{ chunk_id: 9, document_id: 3, error: "400 too long" }] },
    llm_spend: { month_to_date_usd: 12.345, users_counted: 4, users_unknown: 1 },
  };

  it("shows each item and retries a failed job", async () => {
    const calls = mockApi({
      ...signedInAs(ADMIN),
      "GET /api/admin/system": SYSTEM,
      "POST /api/admin/system/jobs/41/retry": { retried: 41 },
    });
    renderAt("/admin/system", <SystemPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Last successful backup was 30 h ago");
    expect(screen.getByText("worker: last heartbeat 300 s ago", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText(/default: 3 waiting/)).toBeInTheDocument();
    expect(screen.getByText("librarian_tick")).toBeInTheDocument();
    expect(screen.getByText(/mwb.dump 2.0 KB/)).toBeInTheDocument();
    expect(screen.getByText("Passed")).toBeInTheDocument();
    expect(screen.getByText(/40.0 GB free of 100.0 GB \(40 %\)/)).toBeInTheDocument();
    expect(screen.getByText(/chunk 9 \(document 3\): 400 too long/)).toBeInTheDocument();
    expect(screen.getByText(/USD 12.35 across 4 users/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Retry job 41"));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/admin/system/jobs/41/retry")).toBe(true));
  });

  it("says when backups aren't configured", async () => {
    mockApi({ ...signedInAs(ADMIN), "GET /api/admin/system": { ...SYSTEM, alerts: [], backup: { status: "not_configured" }, backup_drill: null } });
    renderAt("/admin/system", <SystemPage />);
    expect(await screen.findByText("Not configured")).toBeInTheDocument();
    expect(screen.getByText(/Set MWB_BACKUP_REPO and RESTIC_PASSWORD/)).toBeInTheDocument();
    expect(screen.getByText("Everything looks fine.")).toBeInTheDocument();
    expect(screen.getByText("never recorded")).toBeInTheDocument();
  });

  it("shows where stored secrets are protected, and plain-http hosts", async () => {
    const vault = { backend: "vault", ok: true, error: null, address: "http://vault:8200", data_keys: 1, legacy_keys: 1 };
    mockApi({ ...signedInAs(ADMIN), "GET /api/admin/system": { ...SYSTEM, secrets: vault, http_internal_hosts: ["gitlab.local"] } });
    const { unmount } = renderAt("/admin/system", <SystemPage />);
    const block = await screen.findByRole("region", { name: "Stored secrets" });
    expect(block).toHaveTextContent("Protected by HashiCorp Vault at http://vault:8200: 1 data key loaded.");
    expect(block).toHaveTextContent("After mwb rotate-secrets you can remove it.");
    expect(block).toHaveTextContent("Plain http is allowed to gitlab.local");
    unmount();
    mockApi({ ...signedInAs(ADMIN), "GET /api/admin/system": { ...SYSTEM, secrets: { ...vault, ok: false, error: "sealed" } } });
    renderAt("/admin/system", <SystemPage />);
    expect(await screen.findByText(/Vault at http:\/\/vault:8200 is sealed/)).toBeInTheDocument();
  });

  it("formats sizes", () => {
    expect(bytes(512)).toBe("512 B");
    expect(bytes(1536)).toBe("1.5 KB");
    expect(bytes(undefined)).toBe("—");
  });
});
