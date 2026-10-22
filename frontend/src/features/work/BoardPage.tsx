import { useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ExternalLink, Plus, RefreshCw } from "lucide-react";
import { api, ApiError, type Integration, type Plan, type Project, type WorkItem, type WorkItemDetail, type WorkItemPage } from "../../api";
import { usePageTitle } from "../../app/pageTitle";
import {
  Banner,
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorNote,
  LinkTabs,
  PageHeader,
  Select,
  SkeletonRows,
  StatusPill,
  TextField,
  buttonClass,
  formatUsd,
  useToast,
} from "../../components/ui";
import { StartJobButton } from "./JobTab";
import { COLUMNS, COLUMN_LABEL, nextAction } from "./states";

/** Repositories the user can plan against: ready ones they can see. */
export function useRepositories() {
  return useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/api/projects"), staleTime: 60_000 });
}

/** Jira integrations the user has a token for (Add ticket asks which one when there are several). */
function useJiras() {
  return useQuery({
    queryKey: ["integrations"],
    queryFn: () => api<Integration[]>("/api/integrations"),
    staleTime: 300_000,
    select: (all) => all.filter((i) => i.kind === "jira" && i.credentials.some((c) => c.status !== "not_set")),
  });
}

function repoOptions(projects: Project[] | undefined, placeholder: string): [string, string][] {
  return [["", placeholder], ...(projects ?? []).filter((p) => p.status === "ready").map((p) => [String(p.id), p.name] as [string, string])];
}

/** Start the planner for a ticket, choosing its repository first when it has none (07 §5.8 step 1). */
export function useDraftPlan(item: Pick<WorkItem, "id" | "key">) {
  const qc = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: async (projectId?: number) => {
      if (projectId) await api<WorkItemDetail>(`/api/work-items/${item.id}`, { method: "PATCH", json: { project_id: projectId } });
      return api<Plan>(`/api/work-items/${item.id}/plans`, { method: "POST" });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["work"] });
      qc.invalidateQueries({ queryKey: ["activity"] });
      toast({ title: `Planning ${item.key}; you can leave this page`, tone: "success" });
    },
  });
}

export function DraftPlanButton({ item, primary = true }: { item: WorkItem; primary?: boolean }) {
  const draft = useDraftPlan(item);
  const repos = useRepositories();
  const [choosing, setChoosing] = useState(false);
  const [repo, setRepo] = useState("");
  if (choosing)
    return (
      <form
        className="row"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          if (repo) draft.mutate(Number(repo));
        }}
      >
        <Select label={`Repository for ${item.key}`} options={repoOptions(repos.data, "Choose a repository")} value={repo} onChange={(e) => setRepo(e.target.value)} />
        <Button type="submit" variant="primary" size="sm" pending={draft.isPending} disabled={!repo}>
          Draft plan
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setChoosing(false)}>
          Cancel
        </Button>
        <ErrorNote error={draft.error} />
      </form>
    );
  return (
    <>
      <Button
        variant={primary ? "primary" : "secondary"}
        size="sm"
        pending={draft.isPending}
        onClick={() => (item.project ? draft.mutate(undefined) : setChoosing(true))}
      >
        Draft plan
      </Button>
      <ErrorNote error={draft.error} />
    </>
  );
}

function BoardCard({ item }: { item: WorkItem }) {
  const action = nextAction(item);
  const titleId = `work-${item.id}`;
  return (
    <li>
      <Card as="section" className="work-card stack-tight" aria-labelledby={titleId}>
        <div className="row">
          <a href={item.jira_url} target="_blank" rel="noreferrer" className="mono small">
            {item.key}
            <ExternalLink size={12} aria-label="(opens Jira)" />
          </a>
          <span className="muted small grow">Jira: {item.jira_status}</span>
          {item.cost_usd > 0 && <span className="muted small" title="AI cost so far">{formatUsd(item.cost_usd)}</span>}
        </div>
        <Link id={titleId} to={`/work/${item.id}`} className="work-card-title">
          {item.summary}
        </Link>
        {item.project ? (
          <span className="small">{item.project.name}</span>
        ) : (
          <span className="small warn-text">
            <AlertTriangle size={12} aria-hidden /> Choose repository
          </span>
        )}
        {item.state === "new" && item.plan_status === "failed" && (
          <span className="small problem-text">Planning failed: {item.plan_error ?? "see the plan"}</span>
        )}
        <div className="row">
          {action.kind === "draft" || action.kind === "choose_repo" ? (
            <DraftPlanButton item={item} />
          ) : action.kind === "link" ? (
            <Link to={action.to} className={buttonClass(action.label === "Review plan" ? "primary" : "secondary", "sm")}>
              <span className="btn-label">{action.label}</span>
            </Link>
          ) : action.kind === "start_job" ? (
            <StartJobButton item={item} size="sm" />
          ) : action.kind === "wait" ? (
            <StatusPill status="drafting" label={action.label} />
          ) : null}
        </div>
      </Card>
    </li>
  );
}

function AddTicketDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const repos = useRepositories();
  const jiras = useJiras();
  const [key, setKey] = useState("");
  const [repo, setRepo] = useState("");
  const [jira, setJira] = useState("");
  const add = useMutation({
    mutationFn: () =>
      api<WorkItemDetail>("/api/work-items", {
        method: "POST",
        json: { jira_key: key.trim(), project_id: repo ? Number(repo) : null, integration_id: jira ? Number(jira) : null },
      }),
    onSuccess: (w) => {
      qc.invalidateQueries({ queryKey: ["work"] });
      toast({ title: `${w.key} added`, tone: "success", action: { label: "Open", onClick: () => navigate(`/work/${w.id}`) } });
      setKey("");
      onOpenChange(false);
    },
  });
  const noJira = jiras.data && !jiras.data.length;
  const fieldError = add.error instanceof ApiError && add.error.code === "validation_error" ? add.error.message : null;
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="Add ticket">
      <form
        className="stack"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          add.mutate();
        }}
      >
        {noJira && (
          <Banner tone="warning">
            Add your Jira token in <Link to="/settings/integrations">Settings → Integrations</Link> first.
          </Banner>
        )}
        <TextField label="Jira key" placeholder="PROJ-123" value={key} onChange={(e) => setKey(e.target.value)} required autoFocus error={fieldError} />
        {(jiras.data?.length ?? 0) > 1 && (
          <Select
            label="Jira"
            options={[["", "Choose a Jira"], ...jiras.data!.map((i) => [String(i.id), i.name] as [string, string])]}
            value={jira}
            onChange={(e) => setJira(e.target.value)}
          />
        )}
        <Select label="Repository" options={repoOptions(repos.data, "Choose later")} value={repo} onChange={(e) => setRepo(e.target.value)} hint="The planner reads this repository." />
        {!fieldError && <ErrorNote error={add.error} />}
        <div className="dialog-actions">
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" pending={add.isPending} disabled={!key.trim() || !!noJira}>
            Add ticket
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function RefreshButton() {
  const toast = useToast();
  const refresh = useMutation({
    mutationFn: () => api<{ queued: boolean }>("/api/work-items/refresh", { method: "POST" }),
    onSuccess: () => toast({ title: "Checking Jira; new tickets appear here in a minute", tone: "success" }),
  });
  const setup = refresh.error instanceof ApiError && refresh.error.code === "conflict";
  return (
    <>
      <Button icon={<RefreshCw size={16} aria-hidden />} pending={refresh.isPending} onClick={() => refresh.mutate()}>
        Refresh from Jira
      </Button>
      {setup ? (
        <Banner tone="warning">
          {(refresh.error as ApiError).message}{" "}
          <Link to={(refresh.error as ApiError & { body: { field?: string } }).body?.field === "jira" ? "/settings/integrations" : "/settings/notifications"}>
            Open Settings
          </Link>
        </Banner>
      ) : (
        <ErrorNote error={refresh.error} />
      )}
    </>
  );
}

function DismissedList() {
  const qc = useQueryClient();
  const items = useQuery({
    queryKey: ["work", "items", { state: "dismissed" }],
    queryFn: () => api<WorkItemPage>("/api/work-items?state=dismissed"),
  });
  const restore = useMutation({
    mutationFn: (id: string) => api(`/api/work-items/${id}`, { method: "PATCH", json: { state: "new" } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["work"] }),
  });
  if (!items.data) return <SkeletonRows />;
  if (!items.data.items.length) return <EmptyState>No dismissed tickets.</EmptyState>;
  return (
    <ul className="plain-list stack" aria-label="Dismissed tickets">
      {items.data.items.map((w) => (
        <li key={w.id}>
          <Card className="row">
            <span className="mono small">{w.key}</span>
            <Link to={`/work/${w.id}`} className="grow">
              {w.summary}
            </Link>
            <Button size="sm" pending={restore.isPending && restore.variables === w.id} onClick={() => restore.mutate(w.id)}>
              Restore
            </Button>
          </Card>
        </li>
      ))}
    </ul>
  );
}

/** Board (07 §4.9): one column per state; state changes come from actions, not dragging. */
export default function BoardPage() {
  usePageTitle("Board");
  const [params] = useSearchParams();
  const [adding, setAdding] = useState(false);
  const showDismissed = params.get("show") === "dismissed";
  const items = useQuery({
    queryKey: ["work", "items"],
    queryFn: () => api<WorkItemPage>("/api/work-items"),
    // Poll while a planner is working (07 §6.4).
    refetchInterval: (q) => (q.state.data?.items.some((w) => w.state === "planning") ? 3000 : false),
  });
  const all = items.data?.items ?? [];
  const byState = new Map(COLUMNS.map(([s]) => [s, all.filter((w) => w.state === s)]));
  // Phones show one column at a time (07 §4.9); default to the first column with something in it.
  const column = params.get("column") ?? COLUMNS.find(([s]) => byState.get(s)?.length)?.[0] ?? "new";

  return (
    <div className="page page-wide">
      <PageHeader title="Board">
        <Button variant="primary" icon={<Plus size={16} aria-hidden />} onClick={() => setAdding(true)}>
          Add ticket
        </Button>
        <RefreshButton />
      </PageHeader>
      <AddTicketDialog open={adding} onOpenChange={setAdding} />
      <LinkTabs
        label="Board view"
        tabs={[
          { to: "/work", label: "Board", active: !showDismissed },
          { to: "/work?show=dismissed", label: "Dismissed", active: showDismissed },
        ]}
      />
      {showDismissed ? (
        <DismissedList />
      ) : items.error ? (
        <ErrorNote error={items.error} />
      ) : !items.data ? (
        <SkeletonRows rows={4} />
      ) : !all.length ? (
        <EmptyState action={<Button variant="primary" onClick={() => setAdding(true)}>Add ticket</Button>}>
          No tickets yet. Add one by its Jira key, or turn on Jira intake in Settings → Notifications.
        </EmptyState>
      ) : (
        <>
          <div className="board-filter">
            <LinkTabs
              label="Column"
              tabs={COLUMNS.map(([s, label]) => ({
                to: `/work?column=${s}`,
                label: `${label} (${byState.get(s)?.length ?? 0})`,
                active: column === s,
              }))}
            />
          </div>
          <div className="board">
            {COLUMNS.map(([state, label]) => {
              const list = byState.get(state) ?? [];
              return (
                <section key={state} className="board-column" data-active={column === state || undefined} aria-labelledby={`col-${state}`}>
                  <h2 id={`col-${state}`} className="board-column-title">
                    {COLUMN_LABEL[state]} <span className="muted">{list.length}</span>
                  </h2>
                  {list.length ? (
                    <ul className="plain-list stack" aria-label={label}>
                      {list.map((w) => (
                        <BoardCard key={w.id} item={w} />
                      ))}
                    </ul>
                  ) : (
                    <p className="muted small">Nothing here.</p>
                  )}
                </section>
              );
            })}
          </div>
          {items.data.next_cursor && <p className="muted small">Showing the 100 most recently updated tickets.</p>}
        </>
      )}
    </div>
  );
}
