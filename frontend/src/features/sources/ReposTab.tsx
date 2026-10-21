import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { GitBranch, Plus, RefreshCw, Trash2 } from "lucide-react";
import { api, type Project, type Visibility } from "../../api";
import {
  Banner,
  Button,
  Card,
  ConfirmDialog,
  DataTable,
  EmptyState,
  ErrorNote,
  Select,
  StatusPill,
  TextField,
  VisibilityBadge,
  VisibilityControl,
  formatDate,
  type Column,
} from "../../components/ui";

const HOSTS = [
  ["gitlab", "GitLab"],
  ["gerrit", "Gerrit"],
] as const;

/** Sources → Repositories (07 §4.6). */
export default function ReposTab() {
  const qc = useQueryClient();
  const [repoUrl, setRepoUrl] = useState("");
  const [host, setHost] = useState<"gitlab" | "gerrit">("gitlab");
  const [branch, setBranch] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [removing, setRemoving] = useState<Project | null>(null);
  const [typed, setTyped] = useState("");

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/api/projects"),
    // Poll while any ingestion is in flight (07 §6.4).
    refetchInterval: (q) => (q.state.data?.some((p) => ["queued", "running"].includes(p.status)) ? 3000 : false),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["projects"] });

  const add = useMutation({
    mutationFn: () =>
      api<Project>("/api/projects", { method: "POST", json: { repo_url: repoUrl, host, branch: branch || null } }),
    onSuccess: (p) => {
      setRepoUrl("");
      setBranch("");
      setNotice(p.notice ?? null);
      refresh();
    },
  });
  const ingest = useMutation({
    mutationFn: (id: number) => api(`/api/projects/${id}/ingest?force=true`, { method: "POST" }),
    onSuccess: refresh,
  });
  const visibility = useMutation({
    mutationFn: ({ id, v }: { id: number; v: Visibility }) =>
      api(`/api/projects/${id}/visibility`, { method: "POST", json: { visibility: v } }),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id: number) => api(`/api/projects/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      setRemoving(null);
      refresh();
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    add.mutate();
  }

  const columns: Column<Project>[] = [
    {
      key: "project",
      header: "Repository",
      sortValue: (p) => p.name,
      cell: (p) => (
        <>
          <div>{p.name}</div>
          <div className="muted small mono truncate">{p.repo_url}</div>
          {p.error && <div className="error-text small">{p.error}</div>}
        </>
      ),
    },
    {
      key: "status",
      header: "Status",
      sortValue: (p) => p.status,
      cell: (p) => (
        <>
          <StatusPill status={p.status} />
          {p.status === "running" && p.progress && <div className="muted small">{p.progress}</div>}
        </>
      ),
    },
    { key: "visibility", header: "Visibility", cell: (p) => <VisibilityBadge visibility={p.visibility} /> },
    { key: "sha", header: "Indexed commit", cell: (p) => <span className="mono small">{p.last_ingested_sha?.slice(0, 10) ?? "—"}</span> },
    { key: "updated", header: "Updated", sortValue: (p) => p.updated_at, cell: (p) => formatDate(p.updated_at) },
    {
      key: "actions",
      header: <span className="visually-hidden">Actions</span>,
      cell: (p) =>
        p.mine && (
          <div className="row-actions">
            <Button size="sm" icon={<RefreshCw size={14} aria-hidden />} onClick={() => ingest.mutate(p.id)}>
              Re-index
            </Button>
            <VisibilityControl
              size="sm"
              visibility={p.visibility}
              shareLabel="Share code index"
              onChange={(v) => visibility.mutate({ id: p.id, v })}
            />
            <Button size="sm" variant="danger" icon={<Trash2 size={14} aria-hidden />} onClick={() => { setTyped(""); setRemoving(p); }}>
              Remove
            </Button>
          </div>
        ),
    },
  ];

  const needsTyping = removing?.visibility === "public";
  return (
    <>
      <p className="muted lede">
        Add a repository you work on. MyWorkBuddy clones it with your token from Settings, indexes its code and
        docs, and writes a project card into your private wiki from its files and your commits.
      </p>
      <Card as="form" className="form-row" onSubmit={submit}>
        <TextField
          label="Repository URL"
          placeholder="https://gitlab.example.com/team/pipeline.git"
          value={repoUrl}
          onChange={(e) => setRepoUrl(e.target.value)}
          required
        />
        <Select label="Host" value={host} options={HOSTS} onChange={(e) => setHost(e.target.value as "gitlab" | "gerrit")} />
        <TextField label="Branch" placeholder="default branch" value={branch} onChange={(e) => setBranch(e.target.value)} />
        <Button variant="primary" type="submit" pending={add.isPending} icon={<Plus size={16} aria-hidden />}>
          Add repository
        </Button>
      </Card>
      {notice && <Banner>{notice}</Banner>}
      <ErrorNote error={add.error || ingest.error || visibility.error || remove.error || projects.error} />
      <DataTable
        label="Repositories"
        columns={columns}
        rows={projects.data}
        rowKey={(p) => p.id}
        loading={projects.isLoading}
        empty={<EmptyState>No repositories yet. Add one above to index its code and docs.</EmptyState>}
      />
      <ConfirmDialog
        open={!!removing}
        onOpenChange={(open) => !open && setRemoving(null)}
        title={`Remove ${removing?.name ?? ""}?`}
        danger
        pending={remove.isPending}
        disabled={needsTyping && typed !== removing?.name}
        confirmLabel="Remove repository"
        onConfirm={() => removing && remove.mutate(removing.id)}
        body={
          <div className="stack">
            <p>
              Deletes its code index{needsTyping ? ", which your team can search," : ""} and the local checkout. Your session
              summaries that mention it stay. This can't be undone.
            </p>
            {needsTyping && (
              <TextField
                label={`Type ${removing?.name} to confirm`}
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                autoComplete="off"
              />
            )}
          </div>
        }
      />
      <p className="muted small">
        <GitBranch size={12} aria-hidden /> Gerrit URLs get the <code>/a/</code> prefix automatically.
      </p>
    </>
  );
}
