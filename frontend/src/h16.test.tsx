import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider, focusManager } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import App from "./App";
import { ApiError } from "./api";
import { describeError } from "./app/errors";
import { draftKey, readDraft, writeDraft } from "./app/drafts";
import { ActivityIndicator, POLL_ACTIVE_MS, POLL_IDLE_MS } from "./app/ActivityIndicator";
import { ErrorNote, ToastProvider, formatElapsed, formatRelative, setDisplayTimezone, formatDate } from "./components/ui";
import HomePage from "./features/home/HomePage";
import RepoDetailPage from "./features/sources/RepoDetailPage";
import WikiPage from "./features/wiki/WikiPage";
import ChatPage from "./features/chat/ChatPage";
import SettingsPage from "./features/settings/SettingsPage";
import { ADMIN, MEMBER, mockApi, renderAt, signedInAs } from "./test-utils";

const NOW = "2026-10-20T10:00:00Z";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  localStorage.clear();
  setDisplayTimezone(null);
});

const HOME_EMPTY = { needs_you: [], running: [], recent_chats: [], recent_pages: [], librarian: null, checklists: [] };
const STEPS = [
  { key: "timezone", label: "Confirm your timezone", href: "/settings/profile", done: true, optional: false },
  { key: "tokens", label: "Add your GitLab, Gerrit or Jira token", href: "/settings/integrations", done: false, optional: false },
  { key: "import", label: "Import Claude Code sessions", href: "/sources/imports", done: false, optional: true },
];

describe("error messages (07 §5.10, §6.8)", () => {
  const err = (code: string, message = "server text", extra: Partial<ApiError> = {}) =>
    Object.assign(new ApiError(400, message, code), extra);

  it.each([
    ["not_found", "This item doesn't exist or isn't shared with you.", "Go Home"],
    ["forbidden", "Only the owner can do this.", undefined],
    ["budget_exceeded", "server text", "View usage"],
    ["llm_unavailable", "The AI service isn't responding. Your message is saved; try again in a minute.", undefined],
    ["credential_invalid", "server text", "Update token"],
    ["not_signed_in", "You're signed out. Sign in again to continue.", "Sign in"],
    ["validation_error", "server text", undefined],
    ["invalid_credentials", "server text", undefined],
    ["wiki_conflict", "server text", undefined],
  ])("%s", (code, message, action) => {
    const view = describeError(err(code));
    expect(view.message).toBe(message);
    expect(view.action?.label).toBe(action);
  });

  it("adds a refresh hint to conflicts and a wait time to rate limits", () => {
    expect(describeError(err("conflict", "Already added.")).message).toBe("Already added. Reload to see the latest version.");
    expect(describeError(err("rate_limited", "Too many", { retryAfter: 600 })).message).toBe(
      "Too many attempts. Try again in 10 minutes.",
    );
  });

  it("never shows raw server text for internal errors, but gives a ref to copy", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const e = new ApiError(500, "Traceback (most recent call last)...", "internal", null, "req-42");
    render(
      <MemoryRouter>
        <ErrorNote error={e} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Something went wrong. It's been logged. (ref req-42)");
    expect(screen.queryByText(/Traceback/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy ref" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("req-42"));
  });

  it("links the fix button where it is resolved", () => {
    render(
      <MemoryRouter>
        <ErrorNote error={err("budget_exceeded", "You've reached your monthly AI budget.")} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "View usage" })).toHaveAttribute("href", "/settings/usage");
  });

  it("reads the request id and Retry-After from responses", async () => {
    mockApi({
      "GET /x": new Response(JSON.stringify({ detail: "slow down", code: "rate_limited" }), {
        status: 429,
        headers: { "X-Request-ID": "abc", "Retry-After": "90" },
      }),
    });
    const { api } = await import("./api");
    const e = (await api("/x").catch((x) => x)) as ApiError;
    expect([e.requestId, e.retryAfter, e.code]).toEqual(["abc", 90, "rate_limited"]);
  });
});

describe("time (07 §6.10, rule R8)", () => {
  it("formats relative times and elapsed work", () => {
    const now = Date.parse(NOW);
    expect(formatRelative(NOW, now + 10_000)).toBe("just now");
    expect(formatRelative(NOW, now + 5 * 60_000)).toBe("5 minutes ago");
    expect(formatRelative(NOW, now + 3 * 3600_000)).toBe("3 hours ago");
    expect(formatElapsed(NOW, now + 200_000)).toBe("3 min 20 s");
  });

  it("shows dates in the user's timezone", () => {
    setDisplayTimezone("Asia/Shanghai");
    expect(formatDate("2026-10-20T20:00:00Z")).toMatch(/21|Oct 21/); // 04:00 next day in Shanghai
    setDisplayTimezone("America/Los_Angeles");
    expect(formatDate("2026-10-20T20:00:00Z")).toMatch(/20/);
    setDisplayTimezone("Not/AZone"); // invalid: browser zone, no crash
    expect(formatDate(NOW)).toBeTruthy();
  });
});

describe("Home (07 §4.1)", () => {
  it("shows the checklist, needs-you cards with one fix button each, running work and recent items", async () => {
    mockApi({
      ...signedInAs(MEMBER),
      "GET /api/home": {
        ...HOME_EMPTY,
        needs_you: [
          { kind: "ingest_failed", key: "ingest:4", message: "Indexing payments-api failed: auth", action: "Open repository", href: "/sources/repos/4", at: NOW },
        ],
        running: [{ kind: "ingest", id: 5, title: "etl", status: "running", step: "Indexing code 4 of 10", since: NOW, href: "/sources/repos/5" }],
        recent_chats: [{ id: "s1", title: "Spark OOM", updated_at: NOW }],
        recent_pages: [{ id: 3, title: "Runbook", path: "runbooks/oom.md", visibility: "public", mine: false, updated_at: NOW }],
        librarian: { pages_changed: 3, rejected: 1, at: NOW },
        checklists: [{ kind: "getting_started", steps: STEPS }],
      },
    });
    renderAt("/", <HomePage />);
    const checklist = await screen.findByRole("region", { name: "Getting started" });
    expect(within(checklist).getByText("1 of 3 done")).toBeInTheDocument();
    expect(within(checklist).getByRole("link", { name: "Add your GitLab, Gerrit or Jira token" })).toHaveAttribute(
      "href",
      "/settings/integrations?from=getting-started",
    );
    const needs = screen.getByRole("region", { name: "Needs you" });
    expect(within(needs).getByText("Indexing payments-api failed: auth")).toBeInTheDocument();
    expect(within(needs).getByRole("link", { name: "Open repository" })).toHaveAttribute("href", "/sources/repos/4");
    expect(screen.getByRole("region", { name: "Running now" })).toHaveTextContent("Indexing code 4 of 10");
    expect(screen.getByRole("link", { name: "Spark OOM" })).toHaveAttribute("href", "/chat/s1");
    expect(screen.getByText(/The librarian changed 3 pages and held back 1/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review" })).toHaveAttribute("href", "/wiki/changes");
    for (const name of ["New chat", "Search", "Add repository"]) expect(screen.getByRole("link", { name })).toBeInTheDocument();
  });

  it("says when nothing needs you, hides an empty Running now and lets the checklist be dismissed", async () => {
    mockApi({ ...signedInAs(MEMBER), "GET /api/home": { ...HOME_EMPTY, checklists: [{ kind: "getting_started", steps: STEPS }] } });
    renderAt("/", <HomePage />);
    expect(await screen.findByText("Nothing needs you right now.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Running now" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("region", { name: "Getting started" })).not.toBeInTheDocument();
    expect(localStorage.getItem(`mwb:checklist-dismissed:${MEMBER.id}:getting_started`)).toBe("1");
  });

  it("hides a checklist whose required steps are done", async () => {
    const done = STEPS.map((s) => ({ ...s, done: !s.optional }));
    mockApi({ ...signedInAs(ADMIN), "GET /api/home": { ...HOME_EMPTY, checklists: [{ kind: "admin_setup", steps: done }] } });
    renderAt("/", <HomePage />);
    await screen.findByText("Nothing needs you right now.");
    expect(screen.queryByRole("region", { name: "Admin setup" })).not.toBeInTheDocument();
  });
});

describe("activity indicator (07 §6.3)", () => {
  function renderIndicator() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ToastProvider>
            <ActivityIndicator />
          </ToastProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("shows running work, then a toast when it finishes", async () => {
    let items: unknown[] = [{ kind: "ingest", id: 5, title: "etl", status: "running", step: "Cloning", since: NOW, href: "/sources/repos/5" }];
    mockApi({
      "GET /api/activity": () => ({ items }),
      "GET /api/projects/5": { id: 5, name: "etl", status: "ready" },
    });
    renderIndicator();
    const trigger = await screen.findByRole("button", { name: "1 background task running" });
    fireEvent.click(trigger);
    const pop = await screen.findByRole("dialog", { name: "Background work" });
    expect(within(pop).getByRole("link", { name: "etl" })).toHaveAttribute("href", "/sources/repos/5");
    expect(pop).toHaveTextContent("Cloning");
    items = [];
    await waitFor(() => expect(screen.getByText("etl is ready")).toBeInTheDocument(), { timeout: POLL_ACTIVE_MS + 2000 });
  }, 15_000);

  it("polls every 5 s while work runs, 30 s when idle, and not at all while the tab is hidden", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let running = true;
    const calls = mockApi({
      "GET /api/activity": () => ({
        items: running ? [{ kind: "ingest", id: 1, title: "a", status: "queued", step: null, since: NOW, href: "/x" }] : [],
      }),
      "GET /api/projects/1": { id: 1, name: "a", status: "ready" },
    });
    const count = () => calls.filter((c) => c.path === "/api/activity").length;
    renderIndicator();
    await waitFor(() => expect(count()).toBe(1));
    await act(() => vi.advanceTimersByTimeAsync(POLL_ACTIVE_MS + 100));
    expect(count()).toBe(2);
    running = false;
    await act(() => vi.advanceTimersByTimeAsync(POLL_ACTIVE_MS + 100));
    expect(count()).toBe(3);
    await act(() => vi.advanceTimersByTimeAsync(POLL_ACTIVE_MS * 2));
    expect(count()).toBe(3); // idle now: next poll at 30 s
    await act(() => vi.advanceTimersByTimeAsync(POLL_IDLE_MS));
    expect(count()).toBe(4);
    act(() => focusManager.setFocused(false)); // what TanStack does on visibilitychange to hidden
    await act(() => vi.advanceTimersByTimeAsync(POLL_IDLE_MS * 3));
    expect(count()).toBe(4);
    act(() => focusManager.setFocused(true)); // refetch on focus
    await waitFor(() => expect(count()).toBe(5));
    focusManager.setFocused(undefined);
  });
});

describe("drafts and session expiry (07 §5.11, §6.6)", () => {
  beforeEach(() => localStorage.clear());

  it("keeps the chat composer per session and restores it with a note", async () => {
    const session = { id: "s1", title: "Spark OOM", source: "chat", visibility: "private", project_id: null, owner_id: MEMBER.id,
      created_at: NOW, updated_at: NOW };
    writeDraft(draftKey(MEMBER.id, "chat", "s1"), "half a question");
    mockApi({
      ...signedInAs(MEMBER),
      "GET /api/chat/sessions": [session],
      "GET /api/chat/shared": [],
      "GET /api/projects": [],
      "GET /api/chat/sessions/s1": session,
      "GET /api/chat/sessions/s1/messages": [],
    });
    renderAt("/chat/s1", <ChatPage />, "/chat/:sessionId");
    const box = await screen.findByLabelText<HTMLTextAreaElement>("Message");
    await waitFor(() => expect(box.value).toBe("half a question"));
    expect(await screen.findByText("Restored your unsaved text")).toBeInTheDocument();
    fireEvent.change(box, { target: { value: "a full question" } });
    expect(readDraft(draftKey(MEMBER.id, "chat", "s1"))).toBe("a full question");
  });

  it("reopens the wiki editor with the saved edit and its original version", async () => {
    const page = { id: 3, path: "notes.md", title: "Notes", visibility: "private", owner_id: 2, updated_at: NOW, mine: true,
      content: "# Notes\n\nold", head_sha: "def", can_edit: true };
    const key = draftKey(MEMBER.id, "wiki", 3);
    writeDraft(key, "# Notes\n\nmy edit");
    writeDraft(`${key}:base`, "abc");
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/wiki/pages": [page],
      "GET /api/wiki/pages/3": page,
      "PUT /api/wiki/pages/3": page,
    });
    renderAt("/wiki/3", <WikiPage />, "/wiki/:pageId");
    const editor = await screen.findByLabelText<HTMLTextAreaElement>("Page content");
    expect(editor.value).toBe("# Notes\n\nmy edit");
    expect(await screen.findByText("Restored your unsaved text")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toMatchObject({ base_sha: "abc" }));
    await waitFor(() => expect(readDraft(key)).toBeNull());
  });

  it("asks before leaving the wiki editor with unsaved changes", async () => {
    const page = { id: 3, path: "notes.md", title: "Notes", visibility: "private", owner_id: 2, updated_at: NOW, mine: true,
      content: "# Notes", head_sha: "abc", can_edit: true };
    mockApi({ ...signedInAs(MEMBER), "GET /api/wiki/pages": [page], "GET /api/wiki/pages/3": page });
    const { router } = renderAt("/wiki/3", <WikiPage />, "/wiki/:pageId");
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Page content"), { target: { value: "# Notes\n\nchanged" } });
    act(() => void router.navigate("/wiki/changes"));
    const ask = await screen.findByRole("dialog", { name: "Leave without saving?" });
    fireEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    expect(router.state.location.pathname).toBe("/wiki/3");
    act(() => void router.navigate("/wiki/changes"));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Leave without saving?" })).getByRole("button", { name: "Leave" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/wiki/changes"));
  });

  it("explains an expired session on the sign-in page", async () => {
    mockApi({ "GET /api/auth/me": new Response("{}", { status: 401 }) });
    renderAt("/login?next=%2Fwiki%2F3&reason=expired", <App />);
    expect(await screen.findByText(/Your session expired/)).toBeInTheDocument();
  });

  it("survives storage that throws", () => {
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(() => writeDraft("k", "v")).not.toThrow();
    spy.mockRestore();
  });
});

describe("Settings (07 §4.12)", () => {
  it("confirms the timezone and saves the profile", async () => {
    const calls = mockApi({
      ...signedInAs({ ...MEMBER, timezone: "Europe/Stockholm", timezone_confirmed: false } as typeof MEMBER),
      "PATCH /api/auth/me": ({ body }: { body?: BodyInit | null }) => ({ ...MEMBER, ...JSON.parse(String(body)), timezone_confirmed: true }),
    });
    renderAt("/settings/profile", <SettingsPage />, "/settings/:tab");
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    fireEvent.click(await screen.findByRole("button", { name: `Use ${zone}` }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ timezone: zone }));
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Alice L" } });
    fireEvent.change(screen.getByLabelText("Timezone"), { target: { value: "Nowhere/Land" } });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    expect(screen.getByText(/Pick a timezone from the list/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Timezone"), { target: { value: "Asia/Shanghai" } });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    await waitFor(() =>
      expect(calls.filter((c) => c.method === "PATCH").at(-1)?.body).toMatchObject({ display_name: "Alice L", timezone: "Asia/Shanghai" }),
    );
  });

  it("stores the theme in the browser and applies it", async () => {
    mockApi({ ...signedInAs(MEMBER) });
    renderAt("/settings/profile", <SettingsPage />, "/settings/:tab");
    fireEvent.click(await screen.findByLabelText("Dark"));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("mwb:theme")).toBe("dark");
    fireEvent.click(screen.getByLabelText("Same as my system"));
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("offers the way back to Add repository", async () => {
    mockApi({ ...signedInAs(MEMBER), "GET /api/integrations": [] });
    renderAt("/settings/integrations?back=%2Fsources%2Frepos%3Fadd%3D1", <SettingsPage />, "/settings/:tab");
    expect(await screen.findByRole("link", { name: "Back to Add repository" })).toHaveAttribute("href", "/sources/repos?add=1");
  });
});

describe("repository detail (07 §4.6)", () => {
  it("shows the reason, the fix and the counts, and retries", async () => {
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/projects/4": {
        id: 4, name: "payments-api", repo_url: "https://g/p.git", host: "gitlab", branch: null, visibility: "private",
        status: "failed", error: "fatal: Authentication failed", last_ingested_sha: null, progress: null, owner_id: 2,
        updated_at: NOW, mine: true, files: 12, code_chunks: 340, docs: 3, card_page_id: 9,
        hint: { message: "Your GitLab token was rejected. Update it, then re-index.", action: "Update token", href: "/settings/integrations?focus=gitlab_token" },
      },
      "POST /api/projects/4/ingest": { queued: true },
    });
    renderAt("/sources/repos/4", <RepoDetailPage />, "/sources/repos/:repoId");
    expect(await screen.findByText("Indexing payments-api failed: fatal: Authentication failed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Update token" })).toHaveAttribute("href", "/settings/integrations?focus=gitlab_token");
    expect(screen.getByText("340")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Project card/ })).toHaveAttribute("href", "/wiki/9");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/projects/4/ingest")).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove payments-api?" });
    expect(dialog).toHaveTextContent("Its project card in your wiki and your session summaries that mention it stay.");
  });
});

describe("librarian changes (07 §5.5)", () => {
  it("reverts a change after confirming", async () => {
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/wiki/pages": [],
      "GET /api/wiki/librarian/changes": [{ sha: "abc1234", date: NOW, message: "Librarian: update decisions.md", diff: "+x" }],
      "GET /api/wiki/librarian/runs": [],
      "POST /api/wiki/librarian/changes/abc1234/revert": { reverted: ["decisions.md"] },
    });
    renderAt("/wiki/changes", <WikiPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Revert this change" }));
    const dialog = await screen.findByRole("dialog", { name: "Revert this change?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Revert change" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/wiki/librarian/changes/abc1234/revert")).toBe(true));
    expect(await screen.findByText("Change reverted")).toBeInTheDocument();
    expect(localStorage.getItem(`mwb:librarian-seen:${MEMBER.id}`)).toBeTruthy();
  });

  it("shows why a revert was refused", async () => {
    mockApi({
      ...signedInAs(MEMBER),
      "GET /api/wiki/pages": [],
      "GET /api/wiki/librarian/changes": [{ sha: "abc1234", date: NOW, message: "Librarian: x", diff: "+x" }],
      "GET /api/wiki/librarian/runs": [],
      "POST /api/wiki/librarian/changes/abc1234/revert": new Response(
        JSON.stringify({ detail: "x.md changed after the librarian's edit; open the page and change it by hand", code: "conflict" }),
        { status: 409 },
      ),
    });
    renderAt("/wiki/changes", <WikiPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Revert this change" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revert change" }));
    expect(await screen.findByText(/changed after the librarian's edit/)).toBeInTheDocument();
  });
});

describe("citations (07 §5.2)", () => {
  it("open in a drawer beside the chat on desktop", async () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("min-width"), addEventListener() {}, removeEventListener() {} }));
    const session = { id: "s1", title: "OOM", source: "chat", visibility: "private", project_id: null, owner_id: MEMBER.id, created_at: NOW, updated_at: NOW };
    mockApi({
      ...signedInAs(MEMBER),
      "GET /api/chat/sessions": [session],
      "GET /api/chat/shared": [],
      "GET /api/projects": [],
      "GET /api/chat/sessions/s1": session,
      "GET /api/chat/sessions/s1/messages": [
        { id: 1, role: "assistant", content: "See [1].", created_at: NOW, status: "complete", tools: [],
          citations: [{ n: 1, document_id: 1, title: "OOM runbook", source_type: "wiki", source_ref: "wiki:7", visibility: "public", meta: {} }] },
      ],
      "GET /api/wiki/pages/7": { id: 7, path: "runbooks/oom.md", title: "OOM runbook", visibility: "public", owner_id: 3, updated_at: NOW,
        mine: false, content: "# OOM runbook\n\nRaise overhead.", head_sha: "a", can_edit: false },
    });
    renderAt("/chat/s1", <ChatPage />, "/chat/:sessionId");
    fireEvent.click(await screen.findByRole("link", { name: /OOM runbook/ }));
    const drawer = await screen.findByRole("dialog", { name: "[1] OOM runbook" });
    expect(await within(drawer).findByText("Raise overhead.")).toBeInTheDocument();
    expect(within(drawer).getByRole("link", { name: "Open full page" })).toHaveAttribute("href", "/wiki/7");
  });
});
