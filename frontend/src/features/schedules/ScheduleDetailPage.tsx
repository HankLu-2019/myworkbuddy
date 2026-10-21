import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Play, Trash2 } from "lucide-react";
import { api, type RunPage, type RunSummary, type Schedule } from "../../api";
import { usePageTitle } from "../../app/pageTitle";
import {
  Banner,
  Button,
  ConfirmDialog,
  DataTable,
  ErrorNote,
  KeyValue,
  PageHeader,
  RelativeTime,
  SkeletonRows,
  StatusPill,
  buttonClass,
  formatDate,
  formatUsd,
  useToast,
  type Column,
} from "../../components/ui";
import { NOTIFY_LABEL } from "./presets";
import { ACTIVE } from "./RunResult";

function runDuration(r: RunSummary): string {
  if (!r.started_at || !r.finished_at) return "—";
  const s = Math.round((Date.parse(r.finished_at) - Date.parse(r.started_at)) / 1000);
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min`;
}

/** Schedule detail (07 §4.7): configuration, edit, pause/resume, delete, and its runs with "Load more". */
export default function ScheduleDetailPage() {
  const { scheduleId } = useParams();
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const [deleting, setDeleting] = useState(false);
  const schedule = useQuery({
    queryKey: ["schedules", scheduleId],
    queryFn: () => api<Schedule>(`/api/schedules/${scheduleId}`),
  });
  const runs = useInfiniteQuery({
    queryKey: ["runs", "schedule", scheduleId],
    queryFn: ({ pageParam }) =>
      api<RunPage>(`/api/schedules/${scheduleId}/runs?limit=20${pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ""}`),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: (q) =>
      q.state.data?.pages.some((p) => p.items.some((r) => ACTIVE.includes(r.status))) ? 3000 : false,
  });
  const s = schedule.data;
  usePageTitle({ label: "Schedules", to: "/schedules" }, s?.name);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["schedules"] });
    qc.invalidateQueries({ queryKey: ["runs", "schedule", scheduleId] });
    qc.invalidateQueries({ queryKey: ["home"] });
  };
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api<Schedule>(`/api/schedules/${scheduleId}`, { method: "PATCH", json: { enabled } }),
    onSuccess: (x) => {
      refresh();
      toast({ title: x.enabled ? "Schedule resumed" : "Schedule paused", tone: "success" });
    },
  });
  const runNow = useMutation({
    mutationFn: () => api<{ run_id: string }>(`/api/schedules/${scheduleId}/run`, { method: "POST" }),
    onSuccess: () => {
      refresh();
      qc.invalidateQueries({ queryKey: ["activity"] });
      toast({ title: "Run started; you can leave this page" });
    },
  });
  const remove = useMutation({
    mutationFn: () => api(`/api/schedules/${scheduleId}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["schedules"] });
      toast({ title: "Schedule deleted" });
      navigate("/schedules");
    },
  });

  if (schedule.error)
    return (
      <div className="page">
        <ErrorNote error={schedule.error} />
      </div>
    );
  if (!s)
    return (
      <div className="page">
        <SkeletonRows rows={6} />
      </div>
    );

  const columns: Column<RunSummary>[] = [
    {
      key: "status",
      header: "Result",
      cell: (r) => (
        <Link to={`/runs/${r.id}`} className="row" aria-label={`Run of ${formatDate(r.scheduled_for)}`}>
          <StatusPill status={r.status === "succeeded" && r.report_status ? r.report_status : r.status} />
          {r.headline && <span className="truncate">{r.headline}</span>}
        </Link>
      ),
    },
    { key: "started", header: "Started", cell: (r) => <RelativeTime iso={r.started_at ?? r.scheduled_for} /> },
    { key: "duration", header: "Duration", cell: runDuration },
    { key: "cost", header: "Cost", numeric: true, cell: (r) => (r.cost_usd != null ? formatUsd(Number(r.cost_usd)) : "—") },
    { key: "changed", header: "Changed", cell: (r) => (r.changed == null ? "—" : r.changed ? "Yes" : "No") },
    { key: "trigger", header: "How", cell: (r) => (r.trigger === "manual" ? "By hand" : "Scheduled") },
  ];

  return (
    <div className="page">
      <PageHeader title={s.name}>
        <Button icon={<Play size={16} aria-hidden />} onClick={() => runNow.mutate()} pending={runNow.isPending}>
          Run now
        </Button>
        <Link to={`/schedules/${s.id}/edit`} className={buttonClass("secondary")}>
          <Pencil size={16} aria-hidden />
          <span className="btn-label">Edit</span>
        </Link>
        <Button variant="danger" icon={<Trash2 size={16} aria-hidden />} onClick={() => setDeleting(true)}>
          Delete
        </Button>
      </PageHeader>
      <ErrorNote error={toggle.error ?? runNow.error} />
      {!s.enabled && (
        <Banner tone={s.paused_reason ? "warning" : "info"}>
          {s.paused_reason ?? "This schedule is paused."}{" "}
          <Button size="sm" variant="primary" onClick={() => toggle.mutate(true)} pending={toggle.isPending}>
            Resume
          </Button>
        </Banner>
      )}
      <KeyValue
        items={[
          ["Check type", s.agent.name],
          ["When", `${s.description} (${s.timezone})`],
          ["Next run", s.next_run_at ? formatDate(s.next_run_at) : "Paused"],
          ["Notify", `${NOTIFY_LABEL[s.notify_mode]}${s.notify_email ? ", by email too" : ", in the inbox only"}`],
          ["Settings", Object.keys(s.inputs).length ? <code key="i">{JSON.stringify(s.inputs)}</code> : "—"],
          ["Instructions", s.prompt || "—"],
        ]}
      />
      {s.enabled && (
        <div>
          <Button onClick={() => toggle.mutate(false)} pending={toggle.isPending}>
            Pause
          </Button>
        </div>
      )}
      <h2>Runs</h2>
      <DataTable
        label="Runs"
        columns={columns}
        rows={runs.data?.pages.flatMap((p) => p.items)}
        rowKey={(r) => r.id}
        loading={runs.isLoading}
        empty="No runs yet. Use Run now to try it."
        onLoadMore={runs.hasNextPage ? () => runs.fetchNextPage() : undefined}
        loadingMore={runs.isFetchingNextPage}
      />
      <ConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        title={`Delete ${s.name}?`}
        body="The schedule stops running. Its past runs and their results stay available from the inbox."
        confirmLabel="Delete schedule"
        danger
        pending={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
    </div>
  );
}
