import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw, Trash2 } from "lucide-react";
import { api, type Project, type Visibility } from "../../api";
import { useAuth } from "../../auth";
import {
  Button,
  DataTable,
  EmptyState,
  ErrorNote,
  RelativeTime,
  StatusPill,
  VisibilityBadge,
  VisibilityControl,
  type Column,
} from "../../components/ui";
import { AddRepositoryDialog, RemoveRepositoryDialog } from "./repoParts";

const WORKING = ["queued", "running"];

/** Sources → Repositories (07 §4.6). `?add=1` opens Add repository (from Home and "Back to Add repository"). */
export default function ReposTab() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const [adding, setAdding] = useState(params.get("add") === "1");
  const [removing, setRemoving] = useState<Project | null>(null);
  useEffect(() => {
    if (params.get("add") === "1") {
      setAdding(true);
      setParams({}, { replace: true });
    }
  }, [params, setParams]);

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/api/projects"),
    // Poll while any ingestion is in flight (07 §6.4); paused while the tab is hidden.
    refetchInterval: (q) => (q.state.data?.some((p) => WORKING.includes(p.status)) ? 3000 : false),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["projects"] });
  const ingest = useMutation({
    mutationFn: (id: number) => api(`/api/projects/${id}/ingest?force=true`, { method: "POST" }),
    onSuccess: () => {
      refresh();
      qc.invalidateQueries({ queryKey: ["activity"] });
    },
  });
  const visibility = useMutation({
    mutationFn: ({ id, v }: { id: number; v: Visibility }) =>
      api(`/api/projects/${id}/visibility`, { method: "POST", json: { visibility: v } }),
    onSuccess: refresh,
  });

  const columns: Column<Project>[] = [
    {
      key: "project",
      header: "Repository",
      sortValue: (p) => p.name,
      cell: (p) => (
        <>
          <Link to={`/sources/repos/${p.id}`}>{p.name}</Link>
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
    { key: "owner", header: "Owner", cell: (p) => (p.mine ? "you" : <span className="muted">a teammate</span>) },
    { key: "updated", header: "Updated", sortValue: (p) => p.updated_at, cell: (p) => <RelativeTime iso={p.updated_at} /> },
    {
      key: "actions",
      header: <span className="visually-hidden">Actions</span>,
      cell: (p) =>
        p.mine ? (
          <div className="row-actions">
            <Button
              size="sm"
              icon={<RefreshCw size={14} aria-hidden />}
              disabled={WORKING.includes(p.status)}
              onClick={() => ingest.mutate(p.id)}
            >
              Re-index
            </Button>
            <VisibilityControl
              size="sm"
              item="repository"
              name={p.name}
              visibility={p.visibility}
              shareLabel="Share code index"
              onChange={(v) => visibility.mutateAsync({ id: p.id, v })}
            />
            <Button size="sm" variant="danger" icon={<Trash2 size={14} aria-hidden />} onClick={() => setRemoving(p)}>
              Remove
            </Button>
          </div>
        ) : (
          user?.role === "admin" &&
          p.visibility === "public" && (
            <VisibilityControl
              size="sm"
              item="repository"
              name={p.name}
              mine={false}
              visibility={p.visibility}
              onChange={(v) => visibility.mutateAsync({ id: p.id, v })}
            />
          )
        ),
    },
  ];

  return (
    <>
      <div className="page-header">
        <p className="muted lede">
          Repositories you work on. MyWorkBuddy indexes their code and docs and writes a project card into your
          private wiki from their files and your commits.
        </p>
        <Button variant="primary" icon={<Plus size={16} aria-hidden />} onClick={() => setAdding(true)}>
          Add repository
        </Button>
      </div>
      <ErrorNote error={ingest.error || visibility.error || projects.error} />
      <DataTable
        label="Repositories"
        columns={columns}
        rows={projects.data}
        rowKey={(p) => p.id}
        rowLabel={(p) => p.name}
        loading={projects.isLoading}
        empty={
          <EmptyState action={<Button variant="primary" onClick={() => setAdding(true)}>Add repository</Button>}>
            No repositories yet. Add one to search its code and docs.
          </EmptyState>
        }
      />
      <AddRepositoryDialog open={adding} onOpenChange={setAdding} />
      <RemoveRepositoryDialog project={removing} onOpenChange={(o) => !o && setRemoving(null)} />
    </>
  );
}
