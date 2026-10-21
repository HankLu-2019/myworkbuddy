import { useState } from "react";
import { Navigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { api, type AdminIntegration, type IntegrationKind } from "../../api";
import { useAuth } from "../../auth";
import { usePageTitle } from "../../app/pageTitle";
import {
  Button,
  ConfirmDialog,
  DataTable,
  Dialog,
  EmptyState,
  ErrorNote,
  PageHeader,
  Select,
  StatusPill,
  TextArea,
  TextField,
  Toggle,
  useToast,
} from "../../components/ui";
import { AdminTabs } from "./AdminTabs";

const KINDS: [IntegrationKind, string][] = [
  ["gitlab", "GitLab"],
  ["gerrit", "Gerrit"],
  ["jira", "Jira"],
  ["jenkins", "Jenkins"],
  ["kubernetes", "Kubernetes"],
  ["feed", "RSS/Atom feeds"],
];
const KIND_LABEL = Object.fromEntries(KINDS) as Record<IntegrationKind, string>;

type Form = {
  kind: IntegrationKind;
  name: string;
  base_url: string;
  ca_bundle: string;
  api_version: string;
  allowed_kinds: string;
  allowed_namespaces: string;
  urls: string;
};

const BLANK: Form = { kind: "gitlab", name: "", base_url: "", ca_bundle: "", api_version: "2", allowed_kinds: "", allowed_namespaces: "", urls: "" };

const list = (text: string) =>
  text
    .split(/[\s,]+/)
    .map((x) => x.trim())
    .filter(Boolean);

function settingsOf(f: Form): Record<string, unknown> {
  switch (f.kind) {
    case "jira":
      return { api_version: f.api_version };
    case "kubernetes":
      return {
        ...(f.allowed_kinds.trim() ? { allowed_kinds: list(f.allowed_kinds) } : {}),
        allowed_namespaces: f.allowed_namespaces.trim() ? list(f.allowed_namespaces) : null,
      };
    case "feed":
      return { urls: list(f.urls) };
    default:
      return {};
  }
}

function formOf(i: AdminIntegration): Form {
  const s = i.settings as { api_version?: string; allowed_kinds?: string[]; allowed_namespaces?: string[] | null; urls?: string[] };
  return {
    kind: i.kind,
    name: i.name,
    base_url: i.base_url,
    ca_bundle: "",
    api_version: s.api_version ?? "2",
    allowed_kinds: (s.allowed_kinds ?? []).join(", "),
    allowed_namespaces: (s.allowed_namespaces ?? []).join(", "),
    urls: (s.urls ?? []).join("\n"),
  };
}

/** Add or edit one integration (02 §1). A new CA bundle replaces the stored one; leaving it empty keeps it. */
function IntegrationDialog({ editing, open, onOpenChange }: { editing: AdminIntegration | null; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState<Form>(() => (editing ? formOf(editing) : BLANK));
  const [removeCa, setRemoveCa] = useState(false);
  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = { name: form.name, base_url: form.base_url, settings: settingsOf(form) };
      if (form.ca_bundle.trim()) body.ca_bundle = form.ca_bundle;
      else if (removeCa) body.ca_bundle = "";
      return editing
        ? api<AdminIntegration>(`/api/admin/integrations/${editing.id}`, { method: "PATCH", json: body })
        : api<AdminIntegration>("/api/admin/integrations", { method: "POST", json: { ...body, kind: form.kind } });
    },
    onSuccess: (i) => {
      qc.invalidateQueries({ queryKey: ["admin-integrations"] });
      qc.invalidateQueries({ queryKey: ["integrations"] });
      onOpenChange(false);
      toast({ title: editing ? `${i.name} saved` : `${i.name} added`, tone: "success", description: editing ? undefined : "Users can now add their credentials in Settings." });
    },
  });
  const set = (k: keyof Form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) save.reset();
        onOpenChange(o);
      }}
      title={editing ? `Edit ${editing.name}` : "Add integration"}
      description="Users add their own credentials for it in Settings → Integrations."
    >
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        {!editing && <Select label="Kind" value={form.kind} options={KINDS} onChange={(e) => setForm({ ...form, kind: e.target.value as IntegrationKind })} />}
        <TextField label="Name" value={form.name} onChange={set("name")} placeholder={KIND_LABEL[form.kind]} required />
        <TextField
          label="Base URL"
          value={form.base_url}
          onChange={set("base_url")}
          placeholder={form.kind === "kubernetes" ? "https://k8s.example.com:6443" : "https://gitlab.example.com"}
          hint={form.kind === "kubernetes" ? "The API server, exactly as it appears in kubeconfigs." : "https:// only."}
          required
        />
        {form.kind === "jira" && (
          <Select
            label="API version"
            value={form.api_version}
            options={[
              ["2", "2 (Jira Server and Data Center)"],
              ["3", "3 (Jira Cloud)"],
            ]}
            onChange={set("api_version")}
          />
        )}
        {form.kind === "kubernetes" && (
          <>
            <TextField
              label="Resource kinds users may read"
              value={form.allowed_kinds}
              onChange={set("allowed_kinds")}
              placeholder="pods, deployments, events, … (empty: the default set)"
              hint="Secrets can never be read."
            />
            <TextField
              label="Namespaces"
              value={form.allowed_namespaces}
              onChange={set("allowed_namespaces")}
              placeholder="empty: every namespace the credential can see"
            />
          </>
        )}
        {form.kind === "feed" && (
          <TextArea label="Feed URLs, one per line" value={form.urls} onChange={set("urls")} rows={4} required />
        )}
        <TextArea
          label="CA bundle (PEM)"
          value={form.ca_bundle}
          onChange={set("ca_bundle")}
          rows={4}
          inputClassName="mono"
          spellCheck={false}
          hint={
            editing?.has_ca_bundle
              ? "A CA bundle is stored. Paste a new one to replace it."
              : "Only for systems with a company-internal certificate authority."
          }
        />
        {editing?.has_ca_bundle && !form.ca_bundle && <Toggle label="Remove the stored CA bundle" checked={removeCa} onChange={setRemoveCa} />}
        <ErrorNote error={save.error} />
        <div className="dialog-actions">
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="primary" type="submit" pending={save.isPending}>
            {editing ? "Save" : "Add integration"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

/** Admin → Integrations (02 §7, 07 §4.13). */
export default function IntegrationsAdminPage() {
  const { user } = useAuth();
  const qc = useQueryClient();
  usePageTitle({ label: "Admin", to: "/admin" }, "Integrations");
  const [editing, setEditing] = useState<AdminIntegration | null>(null);
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState<AdminIntegration | null>(null);
  const integrations = useQuery({
    queryKey: ["admin-integrations"],
    queryFn: () => api<AdminIntegration[]>("/api/admin/integrations"),
    enabled: user?.role === "admin",
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["admin-integrations"] });
    qc.invalidateQueries({ queryKey: ["integrations"] });
  };
  const toggle = useMutation({
    mutationFn: (i: AdminIntegration) => api(`/api/admin/integrations/${i.id}`, { method: "PATCH", json: { enabled: !i.enabled } }),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (i: AdminIntegration) => api(`/api/admin/integrations/${i.id}`, { method: "DELETE" }),
    onSuccess: () => {
      setDeleting(null);
      refresh();
    },
  });
  if (user?.role !== "admin") return <Navigate to="/" replace />;
  return (
    <div className="page">
      <PageHeader title="Admin">
        <Button variant="primary" icon={<Plus size={16} aria-hidden />} onClick={() => setAdding(true)}>
          Add integration
        </Button>
      </PageHeader>
      <AdminTabs />
      <p className="muted lede">
        The systems MyWorkBuddy can read. Each user adds their own credentials, so everyone sees only what they could
        see themselves.
      </p>
      <ErrorNote error={integrations.error || toggle.error} />
      <DataTable
        label="Integrations"
        rows={integrations.data}
        loading={integrations.isLoading}
        rowKey={(i) => i.id}
        rowLabel={(i) => i.name}
        empty={
          <EmptyState action={<Button variant="primary" onClick={() => setAdding(true)}>Add integration</Button>}>
            No integrations yet. Add GitLab or Gerrit first, so people can index their repositories.
          </EmptyState>
        }
        columns={[
          { key: "name", header: "Name", sortValue: (i) => i.name, cell: (i) => i.name },
          { key: "kind", header: "Kind", sortValue: (i) => i.kind, cell: (i) => KIND_LABEL[i.kind] },
          { key: "url", header: "URL", cell: (i) => <span className="mono small">{i.base_url}</span> },
          { key: "status", header: "Status", cell: (i) => <StatusPill status={i.enabled ? "active" : "disabled"} /> },
          { key: "users", header: "Users", numeric: true, cell: (i) => i.credentials },
          { key: "repos", header: "Repositories", numeric: true, cell: (i) => i.projects },
          {
            key: "actions",
            header: <span className="visually-hidden">Actions</span>,
            cell: (i) => (
              <div className="row-actions">
                <Button size="sm" icon={<Pencil size={14} aria-hidden />} onClick={() => setEditing(i)}>
                  Edit
                </Button>
                <Button size="sm" pending={toggle.isPending && toggle.variables?.id === i.id} onClick={() => toggle.mutate(i)}>
                  {i.enabled ? "Disable" : "Enable"}
                </Button>
                <Button size="sm" variant="danger" icon={<Trash2 size={14} aria-hidden />} onClick={() => setDeleting(i)}>
                  Delete
                </Button>
              </div>
            ),
          },
        ]}
      />
      {adding && <IntegrationDialog editing={null} open onOpenChange={setAdding} />}
      {editing && <IntegrationDialog key={editing.id} editing={editing} open onOpenChange={(o) => !o && setEditing(null)} />}
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => {
          if (!o) {
            setDeleting(null);
            remove.reset();
          }
        }}
        title={`Delete ${deleting?.name ?? ""}?`}
        body={
          <div className="stack">
            <p>
              Everyone's credentials for it are deleted. Repositories indexed through it must be removed first; to
              pause it instead, disable it.
            </p>
            <ErrorNote error={remove.error} />
          </div>
        }
        confirmLabel="Delete integration"
        danger
        pending={remove.isPending}
        onConfirm={() => deleting && remove.mutate(deleting)}
      />
    </div>
  );
}
