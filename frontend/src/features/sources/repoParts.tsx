import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { api, type Project, type SecretStatus } from "../../api";
import { useAuth } from "../../auth";
import { draftKey, readDraft, writeDraft } from "../../app/drafts";
import { Banner, Button, ConfirmDialog, Dialog, ErrorNote, Select, TextField, useToast } from "../../components/ui";

export const HOSTS = [
  ["gitlab", "GitLab"],
  ["gerrit", "Gerrit"],
] as const;
type Host = (typeof HOSTS)[number][0];

/** The credentials a host needs before a repository can be cloned. */
export const HOST_SECRETS: Record<Host, string[]> = { gitlab: ["gitlab_token"], gerrit: ["gerrit_username", "gerrit_password"] };
const HOST_NAME: Record<Host, string> = { gitlab: "GitLab", gerrit: "Gerrit" };

type Form = { host: Host; repo_url: string; branch: string; name: string };
const EMPTY: Form = { host: "gitlab", repo_url: "", branch: "", name: "" };

/**
 * Add repository (07 §4.6, §5.3). Checks for a token first and links to it; the fields are kept as a draft, so
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
  const secrets = useQuery({ queryKey: ["secrets"], queryFn: () => api<SecretStatus[]>("/api/secrets"), enabled: open });
  const missing =
    secrets.data &&
    HOST_SECRETS[form.host].filter((k) => secrets.data.find((s) => s.kind === k)?.status !== "set");
  const add = useMutation({
    mutationFn: () =>
      api<Project>("/api/projects", {
        method: "POST",
        json: { repo_url: form.repo_url.trim(), host: form.host, branch: form.branch || null, name: form.name || null },
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
      description="MyWorkBuddy clones it with your token, indexes its code and docs, and writes a project card into your private wiki."
    >
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          add.mutate();
        }}
      >
        <Select label="Host" value={form.host} options={HOSTS} onChange={(e) => setForm({ ...form, host: e.target.value as Host })} />
        {!!missing?.length && (
          <Banner tone="warning">
            You need a {HOST_NAME[form.host]} {form.host === "gerrit" ? "username and HTTP password" : "token"} first.{" "}
            <Link to={`/settings/integrations?focus=${missing[0]}&back=${back}`}>Add token</Link>
          </Banner>
        )}
        <TextField
          label="Repository URL"
          placeholder="https://gitlab.example.com/team/pipeline.git"
          value={form.repo_url}
          onChange={(e) => setForm({ ...form, repo_url: e.target.value })}
          hint={form.host === "gerrit" ? "Gerrit URLs get the /a/ prefix automatically." : undefined}
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
            disabled={!form.repo_url.trim() || !!missing?.length}
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
