import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useLocation } from "react-router-dom";
import App from "./App";
import { isTyping } from "./app/shortcuts";
import { safeNext } from "./features/auth/LoginPage";
import { ADMIN, MEMBER, mockApi, renderAt, signedInAs } from "./test-utils";

afterEach(() => vi.unstubAllGlobals());

const NOW = "2026-10-20T10:00:00Z";
const CHAT = { "GET /api/chat/sessions": [], "GET /api/chat/shared": [], "GET /api/projects": [] };

/** Renders the app plus a probe that prints the current URL, so tests can assert navigation. */
function Where() {
  const l = useLocation();
  return <output data-testid="where">{l.pathname + l.search}</output>;
}
function renderApp(path: string) {
  return renderAt(
    path,
    <>
      <App />
      <Where />
    </>,
  );
}
const where = () => screen.getByTestId("where").textContent;

describe("navigation (07 §3)", () => {
  it("groups the sidebar and hides admin pages from members", async () => {
    mockApi({ ...signedInAs(MEMBER), ...CHAT });
    renderApp("/chat");
    const primary = await screen.findByRole("navigation", { name: "Primary" });
    expect(within(primary).getByText("Work")).toBeInTheDocument();
    expect(within(primary).getByText("Knowledge")).toBeInTheDocument();
    for (const name of ["Chat", "Search", "Wiki", "Sources"]) {
      expect(within(primary).getByRole("link", { name })).toBeInTheDocument();
    }
    const account = screen.getByRole("navigation", { name: "Account" });
    expect(within(account).getByRole("link", { name: "Settings" })).toBeInTheDocument();
    expect(within(account).queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
    expect(within(primary).getByRole("link", { name: "Chat" })).toHaveAttribute("aria-current", "page");
  });

  it("shows admins Users and System, with a dot when a check fails", async () => {
    mockApi({
      ...signedInAs(ADMIN),
      ...CHAT,
      "GET /api/ready": new Response(JSON.stringify({ ready: false }), { status: 503 }),
    });
    renderApp("/chat");
    const account = await screen.findByRole("navigation", { name: "Account" });
    expect(within(account).getByRole("link", { name: "Users" })).toBeInTheDocument();
    expect(await within(account).findByLabelText("A system check is failing")).toBeInTheDocument();
  });

  it("marks Sources when one of my repositories failed", async () => {
    mockApi({
      ...signedInAs(MEMBER),
      ...CHAT,
      "GET /api/projects": [{ id: 1, name: "etl", status: "failed", mine: true }],
    });
    renderApp("/chat");
    expect(await screen.findByLabelText("A repository failed to index")).toBeInTheDocument();
  });

  it.each([
    ["/projects", "/sources/repos"],
    ["/documents", "/sources/documents"],
    ["/imports", "/sources/imports"],
    ["/sources", "/sources/repos"],
    ["/", "/chat"],
    ["/nowhere", "/chat"],
  ])("redirects %s to %s", async (from, to) => {
    mockApi({
      ...signedInAs(MEMBER),
      ...CHAT,
      "GET /api/knowledge/documents": [],
      "GET /api/imports/claude-code": [],
    });
    renderApp(from);
    await waitFor(() => expect(where()).toBe(to));
  });

  it("shows Sources as tabs with their own URLs", async () => {
    mockApi({ ...signedInAs(MEMBER), ...CHAT, "GET /api/knowledge/documents": [] });
    renderApp("/sources/documents");
    const tabs = await screen.findByRole("navigation", { name: "Sources" });
    expect(within(tabs).getByRole("link", { name: "Documents" })).toHaveAttribute("aria-current", "page");
    fireEvent.click(within(tabs).getByRole("link", { name: "Repositories" }));
    await waitFor(() => expect(where()).toBe("/sources/repos"));
  });

  it("sends signed-out users to the login page and back where they were", async () => {
    mockApi({
      "GET /api/auth/me": new Response(JSON.stringify({ detail: "Not signed in" }), { status: 401 }),
      "POST /api/auth/login": { user: MEMBER, csrf_token: "c" },
      ...CHAT,
      "GET /api/wiki/pages": [],
      "GET /api/wiki/librarian/changes": [],
      "GET /api/wiki/librarian/runs": [],
    });
    renderApp("/wiki?x=1");
    await waitFor(() => expect(where()).toBe("/login?next=%2Fwiki%3Fx%3D1"));
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(where()).toBe("/wiki?x=1"));
  });

  it("only follows same-app next links", () => {
    expect(safeNext("/wiki/3")).toBe("/wiki/3");
    expect(safeNext(null)).toBe("/chat");
    expect(safeNext("https://evil.example")).toBe("/chat");
    expect(safeNext("//evil.example")).toBe("/chat");
    expect(safeNext("/\\evil.example")).toBe("/chat");
    expect(safeNext("/login?next=%2Fwiki")).toBe("/chat");
    expect(safeNext("/loginhelp")).toBe("/loginhelp");
  });
});

describe("top bar", () => {
  it("shows a breadcrumb and sets the document title", async () => {
    mockApi({
      ...signedInAs(MEMBER),
      ...CHAT,
      "GET /api/knowledge/search": [],
    });
    renderApp("/search?q=oom");
    const crumbs = await screen.findByRole("navigation", { name: "Breadcrumb" });
    await waitFor(() => expect(within(crumbs).getAllByRole("listitem")).toHaveLength(2));
    expect(within(crumbs).getByRole("link", { name: "Search" })).toHaveAttribute("href", "/search");
    expect(within(crumbs).getByText("“oom”")).toHaveAttribute("aria-current", "page");
    expect(document.title).toBe("“oom” · Search · MyWorkBuddy");
  });

  it("searches everything from any page and keeps the query in the URL", async () => {
    const calls = mockApi({ ...signedInAs(MEMBER), ...CHAT, "GET /api/knowledge/search": [] });
    renderApp("/chat");
    const box = await screen.findByRole("searchbox", { name: "Search everything" });
    fireEvent.change(box, { target: { value: "spark oom" } });
    fireEvent.submit(box);
    await waitFor(() => expect(where()).toBe("/search?q=spark%20oom"));
    expect(await screen.findByLabelText<HTMLInputElement>("Search query")).toHaveValue("spark oom");
    await waitFor(() => expect(calls.some((c) => c.path === "/api/knowledge/search")).toBe(true));
  });
});

describe("keyboard shortcuts (07 §6.11)", () => {
  it("ignores keys while typing", () => {
    expect(isTyping(document.createElement("input"))).toBe(true);
    expect(isTyping(document.createElement("textarea"))).toBe(true);
    expect(isTyping(document.createElement("select"))).toBe(true);
    expect(isTyping(document.createElement("button"))).toBe(false);
    expect(isTyping(null)).toBe(false);
  });

  it("/ focuses search, g w opens the wiki, ? shows help", async () => {
    mockApi({
      ...signedInAs(MEMBER),
      ...CHAT,
      "GET /api/wiki/pages": [],
      "GET /api/wiki/librarian/changes": [],
      "GET /api/wiki/librarian/runs": [],
    });
    renderApp("/chat");
    const box = await screen.findByRole("searchbox", { name: "Search everything" });
    fireEvent.keyDown(document.body, { key: "/" });
    expect(box).toHaveFocus();
    fireEvent.keyDown(box, { key: "g" }); // typing: ignored
    fireEvent.keyDown(box, { key: "w" });
    expect(where()).toBe("/chat");
    box.blur();
    fireEvent.keyDown(document.body, { key: "g" });
    fireEvent.keyDown(document.body, { key: "w" });
    await waitFor(() => expect(where()).toBe("/wiki"));
    fireEvent.keyDown(document.body, { key: "?" });
    expect(await screen.findByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
  });

  it("c starts a new chat exactly once", async () => {
    const session = { id: "s7", title: "New chat", source: "chat", visibility: "private", project_id: null, owner_id: 2,
      created_at: NOW, updated_at: NOW };
    const calls = mockApi({
      ...signedInAs(MEMBER),
      ...CHAT,
      "POST /api/chat/sessions": session,
      "GET /api/chat/sessions/s7": session,
      "GET /api/chat/sessions/s7/messages": [],
    });
    renderApp("/search");
    await screen.findByRole("searchbox", { name: "Search everything" });
    fireEvent.keyDown(document.body, { key: "c" });
    await waitFor(() => expect(where()).toBe("/chat/s7"));
    expect(calls.filter((c) => c.method === "POST" && c.path === "/api/chat/sessions")).toHaveLength(1);
  });
});
