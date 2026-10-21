import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Project, type Visibility } from "../api";
import { Empty, ErrorNote, PageHeader, StatusPill, VisibilityBadge, formatDate } from "../components/ui";

export default function ProjectsPage() {
  const qc = useQueryClient();
  const [repoUrl, setRepoUrl] = useState("");
  const [host, setHost] = useState<"gitlab" | "gerrit">("gitlab");
  const [branch, setBranch] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/api/projects"),
    // Poll while any ingestion is in flight.
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
    onSuccess: refresh,
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    add.mutate();
  }

  return (
    <div className="page">
      <PageHeader title="Projects" />
      <p className="muted lede">
        Add a repository you work on. MyWorkBuddy clones it with your token from Settings, indexes its code and
        docs, and writes a project card into your private wiki from its files and your commits.
      </p>
      <form className="card form-row" onSubmit={submit}>
        <input
          aria-label="Repository URL"
          placeholder="https://gitlab.example.com/team/pipeline.git"
          value={repoUrl}
          onChange={(e) => setRepoUrl(e.target.value)}
          required
        />
        <select aria-label="Host" value={host} onChange={(e) => setHost(e.target.value as "gitlab" | "gerrit")}>
          <option value="gitlab">GitLab</option>
          <option value="gerrit">Gerrit</option>
        </select>
        <input aria-label="Branch" placeholder="branch (default)" value={branch} onChange={(e) => setBranch(e.target.value)} />
        <button className="primary" type="submit" disabled={add.isPending}>
          Add repository
        </button>
      </form>
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      <ErrorNote error={add.error || ingest.error || visibility.error || remove.error || projects.error} />
      {projects.data && !projects.data.length && <Empty>No projects yet.</Empty>}
      {!!projects.data?.length && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Project</th>
                <th>Status</th>
                <th>Visibility</th>
                <th>Indexed commit</th>
                <th>Updated</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {projects.data.map((p) => (
                <tr key={p.id}>
                  <td>
                    <div>{p.name}</div>
                    <div className="muted small mono truncate">{p.repo_url}</div>
                    {p.error && <div className="error-text small">{p.error}</div>}
                  </td>
                  <td>
                    <StatusPill status={p.status} />
                    {p.status === "running" && p.progress && <div className="muted small">{p.progress}</div>}
                  </td>
                  <td>
                    <VisibilityBadge visibility={p.visibility} />
                  </td>
                  <td className="mono small">{p.last_ingested_sha?.slice(0, 10) ?? "—"}</td>
                  <td>{formatDate(p.updated_at)}</td>
                  <td className="row-actions">
                    {p.mine && (
                      <>
                        <button onClick={() => ingest.mutate(p.id)}>Re-index</button>
                        <button
                          onClick={() =>
                            visibility.mutate({ id: p.id, v: p.visibility === "public" ? "private" : "public" })
                          }
                        >
                          {p.visibility === "public" ? "Make private" : "Share code index"}
                        </button>
                        <button
                          className="danger"
                          onClick={() => window.confirm(`Remove ${p.name} and its index?`) && remove.mutate(p.id)}
                        >
                          Remove
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
