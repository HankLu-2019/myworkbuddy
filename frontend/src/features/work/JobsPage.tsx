import { Link, useSearchParams } from "react-router-dom";
import { useInfiniteQuery } from "@tanstack/react-query";
import { api, type Job, type JobPage, type JobStatus } from "../../api";
import { usePageTitle } from "../../app/pageTitle";
import { DataTable, EmptyState, ErrorNote, PageHeader, RelativeTime, Select, StatusPill, formatUsd, type Column } from "../../components/ui";

/** Status filters (07 §4.11): groups, not every status. */
const FILTERS: [string, string, JobStatus[]][] = [
  ["", "All", []],
  ["working", "Working", ["created", "preparing", "running", "verifying"]],
  ["review", "Waiting for review", ["awaiting_review"]],
  ["pushed", "Pushed", ["approved_push", "pushing", "pushed", "merged", "closed"]],
  ["stopped", "Stopped", ["failed", "timed_out", "budget_exceeded", "cancelled", "discarded"]],
];

function duration(job: Job): string {
  if (!job.started_at) return "—";
  const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
  const min = Math.max(0, Math.round((end - new Date(job.started_at).getTime()) / 60_000));
  return min < 60 ? `${min} min` : `${Math.floor(min / 60)} h ${min % 60} min`;
}

/** The user's jobs (07 §4.11): rows open the work item hub on the Job tab. */
export default function JobsPage() {
  usePageTitle("Jobs");
  const [params, setParams] = useSearchParams();
  const filter = params.get("status") ?? "";
  const statuses = FILTERS.find(([k]) => k === filter)?.[2] ?? [];
  const list = useInfiniteQuery({
    queryKey: ["jobs", "list", filter],
    queryFn: ({ pageParam }) =>
      api<JobPage>(`/api/jobs?limit=50${statuses.map((s) => `&status=${s}`).join("")}${pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ""}`),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: 30_000,
  });
  const rows = list.data?.pages.flatMap((p) => p.items);
  const columns: Column<Job>[] = [
    {
      key: "ticket",
      header: "Ticket",
      cell: (j) => (
        <span className="stack-tight">
          <Link to={`/work/${j.work_item_id}/job`}>{j.key}</Link>
          <span className="muted small">{j.summary}</span>
        </span>
      ),
      sortValue: (j) => j.key,
    },
    { key: "status", header: "Status", cell: (j) => <StatusPill status={j.status} /> },
    { key: "attempt", header: "Attempt", cell: (j) => `${j.attempt} of ${j.max_attempts}`, numeric: true },
    {
      key: "started",
      header: "Started",
      cell: (j) => (j.started_at ? <RelativeTime iso={j.started_at} /> : <span className="muted">Not yet</span>),
      sortValue: (j) => j.started_at ?? j.created_at,
    },
    { key: "duration", header: "Duration", cell: duration },
    { key: "cost", header: "Cost", cell: (j) => formatUsd(j.cost_usd), sortValue: (j) => j.cost_usd, numeric: true },
  ];
  return (
    <div className="page">
      <PageHeader title="Jobs" />
      <div className="row">
        <Select
          label="Status"
          options={FILTERS.map(([k, label]) => [k, label] as [string, string])}
          value={filter}
          onChange={(e) => setParams(e.target.value ? { status: e.target.value } : {}, { replace: true })}
        />
      </div>
      <ErrorNote error={list.error} />
      {rows && !rows.length && !filter ? (
        <EmptyState action={<Link to="/work">Open the board</Link>}>No jobs yet. Start one from an approved plan.</EmptyState>
      ) : (
        <DataTable
          label="Jobs"
          columns={columns}
          rows={rows}
          rowKey={(j) => j.id}
          rowLabel={(j) => j.key}
          loading={list.isLoading}
          empty="No jobs match this filter."
          onLoadMore={list.hasNextPage ? () => list.fetchNextPage() : undefined}
          loadingMore={list.isFetchingNextPage}
        />
      )}
    </div>
  );
}
