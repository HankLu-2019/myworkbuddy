import { Link, useNavigate } from "react-router-dom";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Play, Plus } from "lucide-react";
import { api, type Schedule, type SchedulePage } from "../../api";
import { usePageTitle } from "../../app/pageTitle";
import {
  Banner,
  Button,
  DataTable,
  EmptyState,
  ErrorNote,
  PageHeader,
  RelativeTime,
  StatusPill,
  Toggle,
  buttonClass,
  formatDate,
  useToast,
  type Column,
} from "../../components/ui";

export function useSchedules() {
  return useInfiniteQuery({
    queryKey: ["schedules", "list"],
    queryFn: ({ pageParam }) =>
      api<SchedulePage>(`/api/schedules?limit=50${pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ""}`),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** Schedules list (07 §4.7): human timing, next run, last result, enable toggle, Run now; paused banner rows. */
export default function SchedulesPage() {
  usePageTitle("Schedules");
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const list = useSchedules();
  const rows = list.data?.pages.flatMap((p) => p.items);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["schedules"] });
    qc.invalidateQueries({ queryKey: ["home"] });
  };
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api<Schedule>(`/api/schedules/${id}`, { method: "PATCH", json: { enabled } }),
    onSuccess: (s) => {
      refresh();
      toast({ title: s.enabled ? `${s.name} resumed` : `${s.name} paused` });
    },
  });
  const runNow = useMutation({
    mutationFn: (s: Schedule) => api<{ run_id: string }>(`/api/schedules/${s.id}/run`, { method: "POST" }),
    onSuccess: (r, s) => {
      qc.invalidateQueries({ queryKey: ["activity"] });
      toast({ title: `${s.name} is running; we'll tell you when it's done`, action: { label: "Open", onClick: () => navigate(`/runs/${r.run_id}`) } });
    },
  });
  const paused = rows?.filter((s) => !s.enabled && s.paused_reason) ?? [];

  const columns: Column<Schedule>[] = [
    {
      key: "name",
      header: "Name",
      cell: (s) => (
        <span className="stack-tight">
          <Link to={`/schedules/${s.id}`}>{s.name}</Link>
          <span className="muted small">{s.agent.name}</span>
        </span>
      ),
      sortValue: (s) => s.name.toLowerCase(),
    },
    { key: "when", header: "When", cell: (s) => s.description },
    {
      key: "next",
      header: "Next run",
      cell: (s) => (s.next_run_at ? formatDate(s.next_run_at) : <span className="muted">—</span>),
      sortValue: (s) => s.next_run_at ?? "",
    },
    {
      key: "last",
      header: "Last result",
      cell: (s) =>
        s.last_status ? (
          <span className="row">
            <StatusPill status={s.last_status} />
            {s.last_run_at && (
              <span className="muted small">
                <RelativeTime iso={s.last_run_at} />
              </span>
            )}
          </span>
        ) : (
          <span className="muted">Not run yet</span>
        ),
    },
    {
      key: "enabled",
      header: "Active",
      cell: (s) => (
        <Toggle
          label={s.enabled ? `${s.name} is active` : `${s.name} is paused`}
          checked={s.enabled}
          disabled={toggle.isPending}
          onChange={(enabled) => toggle.mutate({ id: s.id, enabled })}
        />
      ),
    },
    {
      key: "run",
      header: <span className="visually-hidden">Actions</span>,
      cell: (s) => (
        <Button size="sm" icon={<Play size={14} aria-hidden />} onClick={() => runNow.mutate(s)} pending={runNow.isPending && runNow.variables?.id === s.id}>
          Run now
        </Button>
      ),
    },
  ];

  return (
    <div className="page">
      <PageHeader title="Schedules">
        <Link to="/schedules/new" className={buttonClass("primary")}>
          <Plus size={16} aria-hidden />
          <span className="btn-label">New schedule</span>
        </Link>
      </PageHeader>
      <ErrorNote error={list.error ?? toggle.error ?? runNow.error} />
      {paused.map((s) => (
        <Banner key={s.id} tone="warning">
          {s.name}: {s.paused_reason} <Link to={`/schedules/${s.id}`}>Review</Link>
        </Banner>
      ))}
      {rows && !rows.length ? (
        <EmptyState
          action={
            <Link to="/schedules/new" className={buttonClass("primary", "sm")}>
              <span className="btn-label">Create a schedule</span>
            </Link>
          }
        >
          No schedules yet. A schedule runs a check — Kubernetes health, CI failures, a Jira digest or your morning brief
          — and tells you what changed. You can also ask for one in chat.
        </EmptyState>
      ) : (
        <DataTable
          label="Schedules"
          columns={columns}
          rows={rows}
          rowKey={(s) => s.id}
          loading={list.isLoading}
          onLoadMore={list.hasNextPage ? () => list.fetchNextPage() : undefined}
          loadingMore={list.isFetchingNextPage}
        />
      )}
    </div>
  );
}
