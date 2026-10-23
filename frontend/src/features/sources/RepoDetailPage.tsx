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
  TextArea,
  VisibilityBadge,
  VisibilityControl,
  Toggle,
  useToast,
} from "../../components/ui";
import { useAuth } from "../../auth";
import { RemoveRepositoryDialog } from "./repoParts";

/** The owner's conventions text (08 §F4), for repositories that carry no rules file of their own. */
function Conventions({ p }: { p: ProjectDetail }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [text, setText] = useState(p.conventions_md ?? "");
  const save = useMutation({
    mutationFn: () => api(`/api/projects/${p.id}/conventions`, { method: "PUT", json: { text } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects", p.id] });
      toast({ title: "Conventions saved", tone: "success" });
    },
  });
  return (
    <Card as="section" className="stack" aria-labelledby="repo-conventions">
      <h2 id="repo-conventions">Conventions for the planner and the agent</h2>
      <p className="muted small">
        Used when the repository has no <code>.mwb/rules.md</code>, <code>AGENTS.md</code> or <code>CLAUDE.md</code>. A front-matter block can protect
        paths: <code>mwb: {"{"} protected: ["migrations/**"], test: "pytest -q" {"}"}</code>.
      </p>
      {p.mine ? (
        <form
          className="stack-tight"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <TextArea label="Conventions" hideLabel rows={8} maxLength={20000} className="mono" value={text} onChange={(e) => setText(e.target.value)} />
          <div className="row">
            <Button type="submit" pending={save.isPending} disabled={text === (p.conventions_md ?? "")}>
              Save conventions
            </Button>
          </div>
          <ErrorNote error={save.error} />
        </form>
      ) : p.conventions_md ? (
        <pre className="pre-wrap small">{p.conventions_md}</pre>
      ) : (
        <p className="muted">None set.</p>
      )}
    </Card>
  );
}

/** 08 §F10: admins let a repository's verification start containers (needs Sysbox on the host). */
function VerifyServices({ p }: { p: ProjectDetail }) {
  const qc = useQueryClient();
  const toast = useToast();
  const save = useMutation({
    mutationFn: (enabled: boolean) => api<ProjectDetail>(`/api/projects/${p.id}/verify-services`, { method: "PUT", json: { enabled } }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["projects", p.id] });
      toast({ title: r.verify_services ? "Containers allowed in verification" : "Containers turned off in verification", tone: "success" });
    },
  });
  return (
    <Card as="section" className="stack" aria-labelledby="repo-services">
      <h2 id="repo-services">Verification with containers</h2>
      <Toggle label="Let plans for this repository run containers when verifying" checked={p.verify_services} disabled={save.isPending} onChange={(on) => save.mutate(on)} />
      <p className="muted small">
        For tests that need a database or another service in Docker. The sandbox gets its own Docker under Sysbox (see System), with twice the CPU,
        memory and disk; images come only through the proxy. Each plan still asks for it, and the approver sees it.
      </p>
      <ErrorNote error={save.error} />
    </Card>
  );
}

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
  const isAdmin = useAuth().user?.role === "admin";
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
      <Conventions p={p} />
      {(isAdmin || p.verify_services) && (isAdmin ? <VerifyServices p={p} /> : <p className="muted">Plans for this repository may run containers when verifying.</p>)}
      <RemoveRepositoryDialog
        project={removing ? p : null}
        onOpenChange={setRemoving}
        onRemoved={() => navigate("/sources/repos")}
      />
    </div>
  );
}
