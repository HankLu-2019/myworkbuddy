import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import McpServers from "./features/admin/McpServers";
import { ADMIN, mockApi, renderAt, signedInAs } from "./test-utils";
import type { McpServer, McpTool } from "./api";

afterEach(() => vi.unstubAllGlobals());

const NOW = new Date().toISOString();
const t = (id: number, name: string, over: Partial<McpTool> = {}): McpTool => ({
  id, name, full_name: `mcp.docs.${name}`, description: `${name} does things`, input_schema: {}, read_only_hint: null,
  write_hint: null, status: "disabled", reason: null, enabled_at: null, ...over,
}); // prettier-ignore
const SERVER: McpServer = {
  id: 1, name: "docs", url: "https://mcp.example.com/mcp", auth_header: "Authorization", has_auth: true,
  roles: ["admin", "member"], enabled: true, last_refreshed_at: NOW, last_error: null,
  tools: [
    t(10, "search_docs"), t(11, "create_ticket", { write_hint: "The server marks it as not read-only." }),
    t(12, "get_metric", { status: "changed" }), t(13, "big_dump", { status: "enabled", reason: "ok" }),
  ],
}; // prettier-ignore

describe("MCP servers (08 §F12)", () => {
  it("turns on a read tool with a reason, and never offers write tools", async () => {
    const calls = mockApi({
      ...signedInAs(ADMIN),
      "GET /api/admin/mcp-servers": [SERVER],
      "PUT /api/admin/mcp-tools/10": { ...SERVER.tools[0], status: "enabled" },
      "PUT /api/admin/mcp-tools/13": { ...SERVER.tools[3], status: "disabled" },
    });
    renderAt("/admin/integrations", <McpServers />);
    const write = await screen.findByRole("row", { name: "mcp.docs.create_ticket" });
    expect(write).toHaveTextContent("Write tool: can't be turned on.");
    expect(within(write).queryByRole("button")).toBeNull();
    expect(screen.getByRole("row", { name: "mcp.docs.get_metric" })).toHaveTextContent("Changed: check it again");
    fireEvent.click(within(screen.getByRole("row", { name: "mcp.docs.search_docs" })).getByRole("button", { name: "Turn on" }));
    const dialog = await screen.findByRole("dialog", { name: "Turn on mcp.docs.search_docs?" });
    const confirm = within(dialog).getByRole("button", { name: "Turn on as read-only" });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText("Why it's needed, and why it only reads"), { target: { value: "On-call runbook search." } });
    fireEvent.click(confirm);
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ enabled: true, reason: "On-call runbook search." }));
    fireEvent.click(within(screen.getByRole("row", { name: "mcp.docs.big_dump" })).getByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "PUT").at(-1)?.body).toEqual({ enabled: false }));
  });

  it("adds a server with its credential and role", async () => {
    const calls = mockApi({
      ...signedInAs(ADMIN),
      "GET /api/admin/mcp-servers": [],
      "POST /api/admin/mcp-servers": { ...SERVER, tools: [] },
    });
    renderAt("/admin/integrations", <McpServers />);
    fireEvent.click(await screen.findByRole("button", { name: "Add MCP server" }));
    const form = screen.getByRole("form", { name: "Add an MCP server" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "docs" } });
    fireEvent.change(within(form).getByLabelText("Server URL (streamable HTTP)"), { target: { value: "https://mcp.example.com/mcp" } });
    const secret = within(form).getByLabelText("Auth header value");
    expect(secret).toHaveAttribute("type", "password");
    fireEvent.change(secret, { target: { value: "Bearer x" } });
    fireEvent.click(within(form).getByLabelText("Members may use its tools (otherwise admins only)"));
    fireEvent.click(within(form).getByRole("button", { name: "Add server" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "docs", url: "https://mcp.example.com/mcp", auth_header: "Authorization", auth_value: "Bearer x", roles: ["admin"],
    })); // prettier-ignore
  });
});
