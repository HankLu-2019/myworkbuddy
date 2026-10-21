import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import IntegrationsAdminPage from "./features/admin/IntegrationsAdminPage";
import SettingsPage from "./features/settings/SettingsPage";
import { ADMIN, MEMBER, mockApi, renderAt, signedInAs } from "./test-utils";

afterEach(() => vi.unstubAllGlobals());
const NOW = "2026-10-21T10:00:00Z";

const admin = (over: Record<string, unknown> = {}) => ({
  id: 1, kind: "gitlab", name: "GitLab", base_url: "https://gitlab.example.com", settings: {}, has_ca_bundle: false,
  enabled: true, credentials: 3, projects: 2, updated_at: NOW, ...over,
});

describe("Admin → Integrations (02 §7)", () => {
  it("lists integrations and adds a Kubernetes one with its settings", async () => {
    const calls = mockApi({
      ...signedInAs(ADMIN),
      "GET /api/admin/integrations": [admin()],
      "POST /api/admin/integrations": admin({ id: 2, kind: "kubernetes", name: "Prod" }),
    });
    renderAt("/admin/integrations", <IntegrationsAdminPage />);
    const row = await screen.findByRole("row", { name: "GitLab" });
    expect(row).toHaveTextContent("https://gitlab.example.com");
    expect(row).toHaveTextContent("Active");
    fireEvent.click(screen.getAllByRole("button", { name: "Add integration" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "Add integration" });
    fireEvent.change(within(dialog).getByLabelText("Kind"), { target: { value: "kubernetes" } });
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "Prod" } });
    fireEvent.change(within(dialog).getByLabelText("Base URL"), { target: { value: "https://k8s.example.com:6443" } });
    fireEvent.change(within(dialog).getByLabelText("Namespaces"), { target: { value: "prod, web" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add integration" }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "POST")?.body).toEqual({
        kind: "kubernetes",
        name: "Prod",
        base_url: "https://k8s.example.com:6443",
        settings: { allowed_namespaces: ["prod", "web"] },
      }),
    );
    expect(await screen.findByText("Prod added")).toBeInTheDocument();
  });

  it("shows why an integration can't be deleted, and disables it", async () => {
    const calls = mockApi({
      ...signedInAs(ADMIN),
      "GET /api/admin/integrations": [admin()],
      "DELETE /api/admin/integrations/1": new Response(
        JSON.stringify({ detail: "2 repositories are indexed through GitLab; disable it instead, or remove them first.", code: "conflict" }),
        { status: 409 },
      ),
      "PATCH /api/admin/integrations/1": admin({ enabled: false }),
    });
    renderAt("/admin/integrations", <IntegrationsAdminPage />);
    const row = await screen.findByRole("row", { name: "GitLab" });
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete GitLab?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete integration" }));
    expect(await within(dialog).findByText(/disable it instead/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    fireEvent.click(within(row).getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ enabled: false }));
  });

  it("keeps a stored CA bundle unless replaced or removed", async () => {
    const calls = mockApi({
      ...signedInAs(ADMIN),
      "GET /api/admin/integrations": [admin({ has_ca_bundle: true })],
      "PATCH /api/admin/integrations/1": admin(),
    });
    renderAt("/admin/integrations", <IntegrationsAdminPage />);
    fireEvent.click(within(await screen.findByRole("row", { name: "GitLab" })).getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit GitLab" });
    expect(within(dialog).getByText(/A CA bundle is stored/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).not.toHaveProperty("ca_bundle"));
  });

  it("is for admins only", async () => {
    mockApi({ ...signedInAs(MEMBER) });
    renderAt("/admin/integrations", <IntegrationsAdminPage />);
    await waitFor(() => expect(screen.queryByRole("table", { name: "Integrations" })).not.toBeInTheDocument());
  });
});

describe("Settings → Integrations (02 §1)", () => {
  it("takes a kubeconfig in a text area, focuses it and explains refusals", async () => {
    const calls = mockApi({
      ...signedInAs(MEMBER),
      "GET /api/integrations": [
        { id: 4, kind: "kubernetes", name: "Prod", base_url: "https://k8s:6443", help: "exec plugins are refused",
          credentials: [{ kind: "kubeconfig", label: "Kubeconfig", status: "invalid", verified_at: null,
            last_error: "Prod rejected the credential (401)", updated_at: NOW }] },
      ],
      "PUT /api/integrations/4/credentials/kubeconfig": new Response(
        JSON.stringify({ detail: "Kubeconfig refused: user 'u' uses `exec`: exec plugins run commands on our server", code: "validation_error" }),
        { status: 422 },
      ),
    });
    renderAt("/settings/integrations?focus=4:kubeconfig", <SettingsPage />, "/settings/:tab");
    const box = await screen.findByLabelText("Prod kubeconfig");
    expect(box.tagName).toBe("TEXTAREA");
    await waitFor(() => expect(box).toHaveFocus());
    const card = screen.getByRole("region", { name: "Prod" });
    expect(within(card).getByText("Rejected")).toBeInTheDocument();
    expect(within(card).getByText("Prod rejected the credential (401)")).toBeInTheDocument();
    fireEvent.change(box, { target: { value: "users: [{name: u, user: {exec: {}}}]" } });
    fireEvent.click(within(card).getByRole("button", { name: "Save" }));
    expect(await within(card).findByText(/uses `exec`: exec plugins run commands/)).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ value: "users: [{name: u, user: {exec: {}}}]" });
  });

  it("says what to do when there are no integrations", async () => {
    mockApi({ ...signedInAs(MEMBER), "GET /api/integrations": [] });
    renderAt("/settings/integrations", <SettingsPage />, "/settings/:tab");
    expect(await screen.findByText(/An admin adds GitLab, Gerrit, Jira/)).toBeInTheDocument();
  });
});
