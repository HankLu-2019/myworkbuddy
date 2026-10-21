import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { api, type Integration, type Project } from "../../api";
import { missingCredentials } from "../settings/IntegrationsTab";
import { useAuth } from "../../auth";
import { draftKey, readDraft, writeDraft } from "../../app/drafts";
import { Banner, Button, ConfirmDialog, Dialog, ErrorNote, Select, TextField, useToast } from "../../components/ui";

type Form = { integration_id: string; repo_url: string; branch: string; name: string };
const EMPTY: Form = { integration_id: "", repo_url: "", branch: "", name: "" };

/**
 * Add repository (07 §4.6, §5.3). Repositories live on a GitLab or Gerrit integration an admin added (02 §1);
 * the dialog checks that you saved credentials for it and links to them. The fields are kept as a draft, so
 * "Back to Add repository" from Settings reopens the dialog with them.
 */
export function AddRepositoryDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { user } = useAuth();
  const key = draftKey(user?.id, "add-repository", "form");
  const [form, setFormState] = useState<Form>(() => {
    try {
      return { ...EMPTY, ...JSON.parse(readDraft(key) ?? "{}") };
    } catch {
      return EMPTY;
    }
  });
  const setForm = (next: Form) => {
    setFormState(next);
    writeDraft(key, JSON.stringify(next));
  };
  const integrations = useQuery({
    queryKey: ["integrations"],
    queryFn: () => api<Integration[]>("/api/integrations"),
    enabled: open,
  });
  const git = (integrations.data ?? []).filter((i) => i.kind === "gitlab" || i.kind === "gerrit");
  const selected = git.find((i) => String(i.id) === form.integration_id) ?? git[0];
  const missing = selected ? missingCredentials(selected) : [];
  const add = useMutation({
    mutationFn: () =>
      api<Project>("/api/projects", {
        method: "POST",
        json: {
          repo_url: form.repo_url.trim(),
          integration_id: selected?.id,
          branch: form.branch || null,
          name: form.name || null,
        },
      }),
    onSuccess: (p) => {
      writeDraft(key, "");
      setFormState(EMPTY);
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["activity"] });
      onOpenChange(false);
      toast({ title: "Indexing started", description: p.notice ?? `${p.name}: we'll let you know when it's ready.` });
    },
  });
  const back = encodeURIComponent("/sources/repos?add=1");
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) add.reset();
        onOpenChange(o);
      }}
      title="Add repository"
      description="MyWorkBuddy clones it with your credentials, indexes its code and docs, and writes a project card into your private wiki."
    >
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          add.mutate();
        }}
      >
        <ErrorNote error={integrations.error} />
        {integrations.data && !git.length && (
          <Banner tone="warning">
            There's no GitLab or Gerrit integration yet. An admin adds one in Admin → Integrations.
          </Banner>
        )}
        {git.length > 0 && (
          <Select
            label="Where it lives"
            value={selected ? String(selected.id) : ""}
            options={git.map((i) => [String(i.id), `${i.name} (${new URL(i.base_url).host})`] as [string, string])}
            onChange={(e) => setForm({ ...form, integration_id: e.target.value })}
          />
        )}
        {selected && missing.length > 0 && (
          <Banner tone="warning">
            You need your {selected.name} {missing.join(" and ").toLowerCase()} first.{" "}
            <Link
              to={`/settings/integrations?focus=${selected.id}:${selected.credentials.find((c) => c.status === "not_set")?.kind}&back=${back}`}
            >
              Add token
            </Link>
          </Banner>
        )}
        <TextField
          label="Repository URL"
          placeholder={selected ? `${selected.base_url}/team/pipeline${selected.kind === "gitlab" ? ".git" : ""}` : "https://…"}
          value={form.repo_url}
          onChange={(e) => setForm({ ...form, repo_url: e.target.value })}
          hint={selected?.kind === "gerrit" ? "Gerrit URLs get the /a/ prefix automatically." : undefined}
          required
        />
        <TextField
          label="Branch"
          placeholder="default branch"
          value={form.branch}
          onChange={(e) => setForm({ ...form, branch: e.target.value })}
        />
        <TextField
          label="Name"
          placeholder="from the URL"
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
        <ErrorNote error={add.error} />
        <div className="dialog-actions">
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button
            variant="primary"
            type="submit"
            icon={<Plus size={16} aria-hidden />}
            pending={add.isPending}
            disabled={!form.repo_url.trim() || !selected || missing.length > 0}
          >
            Add repository
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

/** Remove, saying what goes and what stays; type the name when the index is shared (07 §4.6, §6.7). */
export function RemoveRepositoryDialog({
  project,
  onOpenChange,
  onRemoved,
}: {
  project: Project | null;
  onOpenChange: (open: boolean) => void;
  onRemoved?: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const [typed, setTyped] = useState("");
  const remove = useMutation({
    mutationFn: (id: number) => api(`/api/projects/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      toast({ title: `Removed ${project?.name}` });
      setTyped("");
      qc.invalidateQueries({ queryKey: ["projects"] });
      onOpenChange(false);
      onRemoved?.();
    },
  });
  const needsTyping = project?.visibility === "public";
  return (
    <ConfirmDialog
      open={!!project}
      onOpenChange={(o) => {
        if (!o) {
          setTyped("");
          remove.reset();
        }
        onOpenChange(o);
      }}
      title={`Remove ${project?.name ?? ""}?`}
      danger
      pending={remove.isPending}
      disabled={needsTyping && typed !== project?.name}
      confirmLabel="Remove repository"
      onConfirm={() => project && remove.mutate(project.id)}
      body={
        <div className="stack">
          <p>
            Deletes its code index{needsTyping ? ", which your team can search," : ""} and the local checkout. Its
            project card in your wiki and your session summaries that mention it stay. This can't be undone.
          </p>
          {needsTyping && (
            <TextField
              label={`Type ${project?.name} to confirm`}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
            />
          )}
          <ErrorNote error={remove.error} />
        </div>
      }
    />
  );
}
