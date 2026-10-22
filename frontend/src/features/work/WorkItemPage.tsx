import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { api, type Plan, type WorkItemDetail } from "../../api";
import { usePageTitle } from "../../app/pageTitle";
import {
  Button,
  Card,
  ConfirmDialog,
  ErrorNote,
  KeyValue,
  LinkTabs,
  Markdown,
  PageHeader,
  ProgressStrip,
  Select,
  SkeletonRows,
  StatusPill,
  buttonClass,
  formatUsd,
  useToast,
} from "../../components/ui";
import { DraftPlanButton, useRepositories } from "./BoardPage";
import PlanTab from "./PlanTab";
import JobTab, { StartJobButton } from "./JobTab";
import type { Job } from "../../api";
import { COLUMN_LABEL, STEPS, nextAction, stepOf } from "./states";

type Tab = "ticket" | "plan" | "job";

function TicketTab({ item }: { item: WorkItemDetail }) {
  const qc = useQueryClient();
  const toast = useToast();
  const repos = useRepositories();
  const [confirm, setConfirm] = useState(false);
  const patch = useMutation({
    mutationFn: (json: { project_id?: number | null; state?: "dismissed" | "new" }) =>
      api<WorkItemDetail>(`/api/work-items/${item.id}`, { method: "PATCH", json }),
    onSuccess: (w, json) => {
      qc.invalidateQueries({ queryKey: ["work"] });
      setConfirm(false);
      if (json.state) toast({ title: json.state === "dismissed" ? `${w.key} dismissed` : `${w.key} restored`, tone: "success" });
      else toast({ title: "Repository saved", tone: "success" });
    },
  });
  const canChangeRepo = item.state === "new" || item.state === "dismissed";
  const options: [string, string][] = [
    ["", "None"],
    ...(repos.data ?? []).filter((p) => p.status === "ready" || p.id === item.project?.id).map((p) => [String(p.id), p.name] as [string, string]),
  ];
  return (
    <div className="stack">
      <Card as="section" className="stack" aria-labelledby="from-jira">
        <h2 id="from-jira">From Jira</h2>
        <KeyValue
          items={[
            ["Status", item.jira_status],
            ["Type", item.issue_type ?? "—"],
            ["Priority", item.priority ?? "—"],
            ["Assignee", item.assignee ?? "Unassigned"],
            ["Labels", item.labels.length ? item.labels.join(", ") : "—"],
            ["Components", item.components.length ? item.components.join(", ") : "—"],
            ...(item.links.length ? [["Links", item.links.map((l) => `${l.type ?? "relates to"} ${l.key}`).join(", ")] as [string, string]] : []),
          ]}
        />
        {item.description ? <Markdown>{item.description}</Markdown> : <p className="muted">No description.</p>}
      </Card>
      <Card as="section" className="stack narrow" aria-labelledby="ticket-repo">
        <h2 id="ticket-repo">Repository</h2>
        <Select
          label="Repository"
          options={options}
          value={item.project ? String(item.project.id) : ""}
          disabled={!canChangeRepo || patch.isPending}
          hint={canChangeRepo ? "The planner reads this repository at its default branch." : "The repository can only change before planning starts."}
          onChange={(e) => patch.mutate({ project_id: e.target.value ? Number(e.target.value) : null })}
        />
        <ErrorNote error={patch.error} />
      </Card>
      <div className="row">
        {item.state === "dismissed" ? (
          <Button onClick={() => patch.mutate({ state: "new" })} pending={patch.isPending}>
            Restore
          </Button>
        ) : item.state === "new" ? (
          <Button variant="danger" onClick={() => setConfirm(true)}>
            Dismiss
          </Button>
        ) : null}
      </div>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={`Dismiss ${item.key}?`}
        body="It leaves the board. You can restore it from Board → Dismissed; Jira isn't changed."
        confirmLabel="Dismiss"
        danger
        pending={patch.isPending}
        onConfirm={() => patch.mutate({ state: "dismissed" })}
      />
    </div>
  );
}

function NextActionButton({ item, tab }: { item: WorkItemDetail; tab: Tab }) {
  const action = nextAction(item);
  if (action.kind === "draft" || action.kind === "choose_repo") return <DraftPlanButton item={item} />;
  if (action.kind === "start_job") return tab === "job" ? null : <StartJobButton item={item} />;
  if (action.kind === "wait") return <StatusPill status="drafting" label={action.label} />;
  if (action.kind === "link" && !(tab === "plan" && action.to.endsWith("/plan")) && !(tab === "job" && action.to.endsWith("/job")))
    return (
      <Link to={action.to} className={buttonClass("primary")}>
        <span className="btn-label">{action.label}</span>
      </Link>
    );
  return null;
}

/** The work item hub (07 §4.10): header with the progress strip and next action, tabs Ticket · Plan. */
function Hub({ workId, tab, jobId }: { workId: string; tab: Tab; jobId?: string }) {
  const item = useQuery({
    queryKey: ["work", "item", workId],
    queryFn: () => api<WorkItemDetail>(`/api/work-items/${workId}`),
    refetchInterval: (q) => (q.state.data?.state === "planning" ? 3000 : q.state.data?.state === "in_progress" ? 10_000 : false),
  });
  const w = item.data;
  usePageTitle({ label: "Board", to: "/work" }, w ? { label: w.key, to: `/work/${w.id}` } : undefined, { plan: "Plan", job: "Job", ticket: "Ticket" }[tab]);
  if (item.error)
    return (
      <div className="page">
        <ErrorNote error={item.error} />
      </div>
    );
  if (!w)
    return (
      <div className="page">
        <SkeletonRows rows={5} />
      </div>
    );
  return (
    <div className="page">
      <PageHeader title={`${w.key}: ${w.summary}`}>
        <NextActionButton item={w} tab={tab} />
      </PageHeader>
      <div className="row small">
        <a href={w.jira_url} target="_blank" rel="noreferrer">
          {w.key} in Jira <ExternalLink size={12} aria-label="(opens Jira)" />
        </a>
        <span className="muted">·</span>
        <span>{w.project ? w.project.name : <span className="warn-text">No repository yet</span>}</span>
        <span className="muted">·</span>
        <StatusPill status={w.state === "new" ? "queued" : w.state} label={COLUMN_LABEL[w.state]} />
        {w.cost_usd > 0 && <span className="muted">AI cost so far {formatUsd(w.cost_usd)}</span>}
      </div>
      <ProgressStrip steps={STEPS} current={stepOf(w.state)} label="Progress" />
      <LinkTabs
        label={`${w.key} tabs`}
        tabs={[
          { to: `/work/${w.id}`, label: "Ticket", active: tab === "ticket" },
          { to: `/work/${w.id}/plan`, label: "Plan", active: tab === "plan" },
          { to: `/work/${w.id}/job`, label: "Job", active: tab === "job" },
        ]}
      />
      {tab === "ticket" ? <TicketTab item={w} /> : tab === "plan" ? <PlanTab item={w} /> : <JobTab item={w} jobId={jobId} />}
    </div>
  );
}

export default function WorkItemPage({ tab = "ticket" }: { tab?: Tab }) {
  const { workId, planId, jobId } = useParams();
  // /jobs/:id is a deep link too: the hub on the Job tab for that job.
  const job = useQuery({ queryKey: ["jobs", jobId], queryFn: () => api<Job>(`/api/jobs/${jobId}`), enabled: !!jobId && !workId });
  if (jobId && !workId) {
    if (job.error)
      return (
        <div className="page">
          <ErrorNote error={job.error} />
        </div>
      );
    if (!job.data)
      return (
        <div className="page">
          <SkeletonRows rows={5} />
        </div>
      );
    return <Hub workId={job.data.work_item_id} tab="job" jobId={jobId} />;
  }
  // /plans/:id is a deep link: render the hub on the Plan tab and keep the URL (07 §3.2).
  const plan = useQuery({ queryKey: ["plans", planId], queryFn: () => api<Plan>(`/api/plans/${planId}`), enabled: !!planId && !workId });
  if (planId && !workId) {
    if (plan.error)
      return (
        <div className="page">
          <ErrorNote error={plan.error} />
        </div>
      );
    if (!plan.data)
      return (
        <div className="page">
          <SkeletonRows rows={5} />
        </div>
      );
    return <Hub workId={plan.data.work_item_id} tab="plan" />;
  }
  return <Hub workId={workId!} tab={tab} />;
}

