import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, parseEvent, setCsrfToken, streamPost } from "./api";
import { mockApi, sse } from "./test-utils";

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrfToken("");
});

describe("api", () => {
  it("sends JSON with the CSRF header on unsafe methods only", async () => {
    const calls = mockApi({ "POST /api/x": { ok: true }, "GET /api/x": [1] });
    setCsrfToken("tok");
    expect(await api("/api/x", { method: "POST", json: { a: 1 } })).toEqual({ ok: true });
    expect(await api("/api/x")).toEqual([1]);
    expect(calls[0].headers["X-CSRF-Token"]).toBe("tok");
    expect(calls[0].body).toEqual({ a: 1 });
    expect(calls[1].headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("returns undefined for 204", async () => {
    mockApi({ "DELETE /api/x": undefined });
    expect(await api("/api/x", { method: "DELETE" })).toBeUndefined();
  });

  it("turns error bodies into readable messages", async () => {
    mockApi({
      "GET /api/a": new Response(JSON.stringify({ detail: "Not found" }), { status: 404 }),
      "GET /api/b": new Response(JSON.stringify({ detail: [{ msg: "too short" }, { msg: "bad" }] }), { status: 422 }),
      "GET /api/c": new Response("oops", { status: 500 }),
    });
    await expect(api("/api/a")).rejects.toMatchObject({ status: 404, message: "Not found" });
    await expect(api("/api/b")).rejects.toMatchObject({ message: "too short; bad" });
    const err: unknown = await api("/api/c").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toBe("Request failed (500)");
  });

  it("parses server-sent events across chunk boundaries", async () => {
    mockApi({ "POST /api/s": sse([["delta", { text: "Hel" }], ["delta", { text: "lo" }], ["done", { id: 1 }]]) });
    const seen: string[] = [];
    await streamPost("/api/s", {}, (e) => seen.push(`${e.event}:${JSON.stringify(e.data)}`));
    expect(seen).toEqual(['delta:{"text":"Hel"}', 'delta:{"text":"lo"}', 'done:{"id":1}']);
  });

  it("parseEvent tolerates non-JSON data", () => {
    expect(parseEvent("event: x\ndata: plain")).toEqual({ event: "x", data: "plain" });
    expect(parseEvent("data: 3")).toEqual({ event: "message", data: 3 });
  });
});
