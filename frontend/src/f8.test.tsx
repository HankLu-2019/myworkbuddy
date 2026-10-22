import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import SettingsPage from "./features/settings/SettingsPage";
import { MEMBER, mockApi, renderAt, signedInAs } from "./test-utils";
import type { NotifyChannel } from "./api";

afterEach(() => vi.unstubAllGlobals());

const NOW = new Date().toISOString();
const SLACK: NotifyChannel = {
  id: 4, kind: "slack", name: "#payments", url_hint: "hooks.slack.com/…abcd", kinds: ["job"], min_severity: "warning",
  include_details: false, enabled: true, disabled_reason: null, created_at: NOW,
}; // prettier-ignore
const OFF: NotifyChannel = { ...SLACK, id: 5, name: "#ops", kinds: [], enabled: false, disabled_reason: "The webhook answered 404." };

describe("Slack and Teams channels (08 §F8)", () => {
  it("adds a channel with its filter and never shows the URL again", async () => {
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/notify-channels": [],
      "POST /api/notify-channels": { ...SLACK, kind: "teams", name: "Team chat" },
    });
    renderAt("/settings/notifications", <SettingsPage />, "/settings/:tab");
    const card = await screen.findByRole("generic", { name: "Chat channels" });
    expect(await within(card).findByText("No chat channels yet.")).toBeInTheDocument();
    fireEvent.click(within(card).getByRole("button", { name: "Add channel" }));
    const form = within(card).getByRole("form", { name: "Add a chat channel" });
    fireEvent.change(within(form).getByLabelText("App"), { target: { value: "teams" } });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Team chat" } });
    const url = within(form).getByLabelText("Webhook URL");
    expect(url).toHaveAttribute("type", "password");
    fireEvent.change(url, { target: { value: "https://x.logic.azure.com/workflows/a?sig=SECRET" } });
    fireEvent.change(within(form).getByLabelText("Send"), { target: { value: "alert" } });
    fireEvent.click(within(form).getByLabelText("Jobs"));
    fireEvent.click(within(form).getByLabelText("Alerts"));
    fireEvent.click(within(form).getByLabelText("Include the first lines of each item"));
    expect(within(form).getByText(/Everyone in the chat channel will see them/)).toBeInTheDocument();
    fireEvent.click(within(form).getByRole("button", { name: "Add channel" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")).toBeTruthy());
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({
      kind: "teams", name: "Team chat", url: "https://x.logic.azure.com/workflows/a?sig=SECRET", kinds: ["job", "alert"],
      min_severity: "alert", include_details: true,
    }); // prettier-ignore
    expect(await screen.findByText("Team chat added")).toBeInTheDocument();
    expect(within(card).queryByRole("form")).toBeNull();
  });

  it("lists channels, sends a test, and turns a dead one back on", async () => {
    let test = { ok: true, status: 200, error: null as string | null };
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/notify-channels": [SLACK, OFF],
      "POST /api/notify-channels/4/test": () => test,
      "PATCH /api/notify-channels/5": { ...OFF, enabled: true, disabled_reason: null },
      "DELETE /api/notify-channels/4": undefined,
    });
    renderAt("/settings/notifications", <SettingsPage />, "/settings/:tab");
    const slack = await screen.findByRole("row", { name: "#payments" });
    expect(slack).toHaveTextContent("hooks.slack.com/…abcd");
    expect(slack).toHaveTextContent("Jobs · warnings and alerts");
    const ops = screen.getByRole("row", { name: "#ops" });
    expect(ops).toHaveTextContent("Everything in your inbox");
    expect(ops).toHaveTextContent("Off: The webhook answered 404.");
    fireEvent.click(within(slack).getByRole("button", { name: "Send test" }));
    expect(await screen.findByText("Test message sent to #payments")).toBeInTheDocument();
    test = { ok: false, status: 403, error: "The webhook answered 403." };
    fireEvent.click(within(slack).getByRole("button", { name: "Send test" }));
    expect(await screen.findByText("#payments didn't get the test message")).toBeInTheDocument();
    fireEvent.click(within(ops).getByRole("button", { name: "Turn on" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ enabled: true }));
    fireEvent.click(within(slack).getByRole("button", { name: "Remove" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove #payments?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove channel" }));
    await waitFor(() => expect(calls.find((c) => c.method === "DELETE")?.path).toBe("/api/notify-channels/4"));
  });
});
