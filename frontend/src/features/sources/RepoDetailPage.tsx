import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, RefreshCw, Trash2 } from "lucide-react";
import { api, type ProjectDetail, type Visibility } from "../../api";
import { usePageTitle } from "../../app/pageTitle";
import {
  Banner,
  Button,
  Card,
  ErrorNote,
  KeyValue,
  PageHeader,
  RelativeTime,
  SkeletonRows,
  StatusPill,
  VisibilityBadge,
  VisibilityControl,
} from "../../components/ui";
import { RemoveRepositoryDialog } from "./repoParts";

/** Repository detail (07 §4.6): status, the last error with what to do, counts, project card, actions. */
export default function RepoDetailPage() {
  const id = Number(useParams().repoId);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [removing, setRemoving] = useState(false);
  const project = useQuery({
    queryKey: ["projects", id],
    queryFn: () => api<ProjectDetail>(`/api/projects/${id}`),
    refetchInterval: (q) => (["queued", "running"].includes(q.state.data?.status ?? "") ? 3000 : false),
  });
  const p = project.data;
  usePageTitle({ label: "Sources", to: "/sources/repos" }, { label: "Repositories", to: "/sources/repos" }, p?.name);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["projects"] });
    qc.invalidateQueries({ queryKey: ["activity"] });
    qc.invalidateQueries({ queryKey: ["home"] });
  };
  const ingest = useMutation({
    mutationFn: () => api(`/api/projects/${id}/ingest?force=true`, { method: "POST" }),
    onSuccess: refresh,
  });
  const visibility = useMutation({
    mutationFn: (v: Visibility) => api(`/api/projects/${id}/visibility`, { method: "POST", json: { visibility: v } }),
    onSuccess: refresh,
  });

  if (project.error) {
    return (
      <div className="page">
        <ErrorNote error={project.error} />
      </div>
    );
  }
  if (!p) {
    return (
      <div className="page">
        <SkeletonRows rows={5} />
      </div>
    );
  }
  const working = ["queued", "running"].includes(p.status);
  return (
    <div className="page">
      <PageHeader title={p.name}>
        <VisibilityBadge visibility={p.visibility} />
        {p.mine && (
          <>
            <Button icon={<RefreshCw size={16} aria-hidden />} pending={ingest.isPending} disabled={working} onClick={() => ingest.mutate()}>
              {p.status === "failed" ? "Retry" : "Re-index"}
            </Button>
            <VisibilityControl
              item="repository"
              name={p.name}
              visibility={p.visibility}
              shareLabel="Share code index with team"
              onChange={(v) => visibility.mutateAsync(v)}
            />
            <Button variant="danger" icon={<Trash2 size={16} aria-hidden />} onClick={() => setRemoving(true)}>
              Remove
            </Button>
          </>
        )}
      </PageHeader>
      <ErrorNote error={ingest.error || visibility.error} />
      {p.status === "failed" && (
        <Banner tone="danger">
          Indexing {p.name} failed: {p.error ?? "unknown error"}
        </Banner>
      )}
      {p.hint && (
        <Banner tone="warning">
          {p.hint.message} <Link to={p.hint.href}>{p.hint.action}</Link>
        </Banner>
      )}
      {p.status !== "failed" && p.error && <Banner tone="warning">{p.error}</Banner>}
      <Card className="stack">
        <KeyValue
          items={[
            [
              "Status",
              <>
                <StatusPill status={p.status} />
                {p.progress && <span className="muted small"> {p.progress}</span>}
              </>,
            ],
            ["Repository", <span className="mono small">{p.repo_url}</span>],
            ["Branch", p.branch ?? "default"],
            ["Indexed commit", <span className="mono small">{p.last_ingested_sha?.slice(0, 12) ?? "not yet"}</span>],
            ["Updated", <RelativeTime iso={p.updated_at} />],
            ["Files indexed", p.files],
            ["Code chunks", p.code_chunks],
            ["Docs", p.docs],
            ["Owner", p.mine ? "you" : "a teammate"],
          ]}
        />
        {p.card_page_id && (
          <div>
            <Link to={`/wiki/${p.card_page_id}`}>
              <BookOpen size={14} aria-hidden /> Project card in the wiki
            </Link>
          </div>
        )}
        {working && <p className="muted small">Still working — large repositories take a few minutes. You can leave this page.</p>}
      </Card>
      <RemoveRepositoryDialog
        project={removing ? p : null}
        onOpenChange={setRemoving}
        onRemoved={() => navigate("/sources/repos")}
      />
    </div>
  );
}
