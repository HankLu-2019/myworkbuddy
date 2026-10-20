import { render } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { vi } from "vitest";
import { AuthProvider } from "./auth";

export type Handler = (init: RequestInit & { url: string }) => unknown;

export const ADMIN = { id: 1, username: "root", email: null, display_name: null, role: "admin", is_active: true };
export const MEMBER = { id: 2, username: "alice", email: "a@x", display_name: "Alice", role: "member", is_active: true };

/** Route fetch calls by "METHOD path" (query string ignored). Unmatched calls fail the test loudly. */
export function mockApi(routes: Record<string, Handler | unknown>) {
  const calls: { method: string; path: string; body: unknown; headers: Record<string, string> }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    const method = init.method ?? "GET";
    const path = url.split("?")[0];
    const body = typeof init.body === "string" ? JSON.parse(init.body) : init.body;
    calls.push({ method, path, body, headers: (init.headers ?? {}) as Record<string, string> });
    const key = `${method} ${path}`;
    if (!(key in routes)) return new Response(JSON.stringify({ detail: `unmocked ${key}` }), { status: 599 });
    const route = routes[key];
    const result = typeof route === "function" ? (route as Handler)({ ...init, url }) : route;
    if (result instanceof Response) return result;
    return new Response(result === undefined ? null : JSON.stringify(result), {
      status: result === undefined ? 204 : 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

export function sse(events: [string, unknown][]): Response {
  const body = events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join("");
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

export function renderAt(path: string, element: ReactElement, routePath = "*") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <AuthProvider>
          <Routes>
            <Route path={routePath} element={element} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

export function signedInAs(user: typeof MEMBER | typeof ADMIN) {
  return { "GET /api/auth/me": { user, csrf_token: "csrf-1" } };
}
