import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import { useAuth } from "../../auth";
import { RotateCcw } from "lucide-react";
import { usePageTitle } from "../../app/pageTitle";
import { Banner, Button, Card, DataTable, EmptyState, ErrorNote, KeyValue, PageHeader, SkeletonRows, StatusPill, formatDate, useToast } from "../../components/ui";
import { AdminTabs } from "./AdminTabs";

type FailedJob = { id: number; queue: string; task: string; attempts: number; failed_at: string };
type System = {
  checks: Record<string, string>;
  alerts: string[];
  worker: { heartbeat: { at: string; host?: string } | null; age_s: number | null };
  jobs: {
    queues: { queue: string; status: string; count: number }[];
    failed_24h: FailedJob[];
    periodic: { task: string; last_at: string }[];
  };
  backup: {
    status: string;
    at?: string;
    last_success_at?: string | null;
    snapshot_id?: string | null;
    sizes?: Record<string, number>;
    error?: string;
  };
  backup_drill: { ok: boolean; at: string; note: string } | null;
  disk: { path: string; total_bytes?: number; free_bytes?: number; free_ratio?: number | null };
  disk_guard: { ok: boolean; problem?: string | null; cache_bytes?: number } | null;
  wiki_reconcile: Record<string, unknown> | null;
  embeddings: { failed: number; sample: { chunk_id: number; document_id: number; error: string }[] };
  llm_spend: { month_to_date_usd: number; users_counted: number; users_unknown: number };
};

const CHECK_LABELS: Record<string, string> = { database: "Database", llm_gateway: "AI gateway", worker: "Worker" };

export function bytes(n: number | undefined): string {
  if (n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <Card as="section" aria-labelledby={id} className="stack">
      <h2 id={id}>{title}</h2>
      {children}
    </Card>
  );
}

/** Admin → System (design 01 §H14): health, jobs, backups, disk, knowledge upkeep and AI spend. */
export default function SystemPage() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  usePageTitle({ label: "Admin", to: "/admin" }, "System");
  const system = useQuery({
    queryKey: ["system"],
    queryFn: () => api<System>("/api/admin/system"),
    enabled: user?.role === "admin",
    refetchInterval: 30_000,
  });
  const retry = useMutation({
    mutationFn: (id: number) => api(`/api/admin/system/jobs/${id}/retry`, { method: "POST" }),
    onSuccess: (_, id) => {
      qc.invalidateQueries({ queryKey: ["system"] });
      toast({ title: `Job ${id} queued again`, tone: "success" });
    },
  });
  if (user?.role !== "admin") return <Navigate to="/chat" replace />;
  const s = system.data;
  return (
    <div className="page">
      <PageHeader title="Admin" />
      <AdminTabs />
      <ErrorNote error={system.error || retry.error} />
      {system.isLoading && <SkeletonRows rows={6} />}
      {s && (
        <>
          <section aria-labelledby="sys-alerts">
            <h2 id="sys-alerts" className="visually-hidden">
              Needs attention
            </h2>
            {s.alerts.length ? (
              <Banner tone="danger" role="alert">
                <strong>Needs attention</strong>
                <ul className="alerts">
                  {s.alerts.map((a) => (
                    <li key={a}>{a}</li>
                  ))}
                </ul>
              </Banner>
            ) : (
              <Banner tone="success">Everything looks fine.</Banner>
            )}
          </section>

          <div className="card-grid">
            <Section id="sys-health" title="Health">
              <KeyValue
                items={[
                  ...Object.entries(s.checks).map(
                    ([k, v]) =>
                      [CHECK_LABELS[k] ?? k, v === "ok" ? <StatusPill status="ok" /> : <StatusPill status="failed" label={v} />] as [
                        ReactNode,
                        ReactNode,
                      ],
                  ),
                  [
                    "Worker heartbeat",
                    <>
                      {s.worker.age_s === null ? "none yet" : `${s.worker.age_s} s ago`}
                      {s.worker.heartbeat?.host && <span className="muted"> ({s.worker.heartbeat.host})</span>}
                    </>,
                  ],
                ]}
              />
            </Section>

            <Section id="sys-backup" title="Backups">
              <KeyValue
                items={[
                  [
                    "Status",
                    <StatusPill
                      status={s.backup.status}
                      label={s.backup.status === "failed" ? `Failed: ${s.backup.error ?? ""}` : undefined}
                    />,
                  ],
                  [
                    "Last success",
                    <>
                      {s.backup.last_success_at ? formatDate(s.backup.last_success_at) : "never"}
                      {s.backup.snapshot_id && <span className="muted mono"> ({s.backup.snapshot_id.slice(0, 8)})</span>}
                    </>,
                  ],
                  [
                    "Size",
                    s.backup.sizes
                      ? Object.entries(s.backup.sizes)
                          .map(([k, v]) => `${k} ${bytes(v)}`)
                          .join(", ")
                      : "—",
                  ],
                  [
                    "Restore drill",
                    s.backup_drill ? (
                      <>
                        {formatDate(s.backup_drill.at)} · <StatusPill status={s.backup_drill.ok ? "ok" : "failed"} label={s.backup_drill.ok ? "Passed" : "Failed"} />
                        {s.backup_drill.note && <span className="muted"> · {s.backup_drill.note}</span>}
                      </>
                    ) : (
                      <span className="muted">never recorded</span>
                    ),
                  ],
                ]}
              />
              {s.backup.status === "not_configured" && (
                <p className="muted small">Set MWB_BACKUP_REPO and RESTIC_PASSWORD (operations guide §5).</p>
              )}
            </Section>

            <Section id="sys-disk" title="Disk">
              <p>
                {s.disk.path}: {bytes(s.disk.free_bytes)} free of {bytes(s.disk.total_bytes)}
                {s.disk.free_ratio != null && ` (${Math.round(s.disk.free_ratio * 100)} %)`}
              </p>
              {s.disk.free_ratio != null && (
                <progress max={1} value={1 - s.disk.free_ratio} aria-label="Share of the data disk used" />
              )}
              {s.disk_guard && !s.disk_guard.ok && <Banner tone="danger">{s.disk_guard.problem}</Banner>}
            </Section>

            <Section id="sys-spend" title="AI spend this month">
              <p>
                USD {s.llm_spend.month_to_date_usd.toFixed(2)} across {s.llm_spend.users_counted} users
                {s.llm_spend.users_unknown > 0 && <span className="muted"> ({s.llm_spend.users_unknown} unknown)</span>}
              </p>
            </Section>
          </div>

          <Section id="sys-jobs" title="Background jobs">
            {s.jobs.queues.length ? (
              <ul>
                {s.jobs.queues.map((q) => (
                  <li key={`${q.queue}-${q.status}`}>
                    {q.queue}: {q.count} {q.status === "doing" ? "running" : "waiting"}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">No jobs waiting or running.</p>
            )}
            <h3>Failed in the last 24 hours</h3>
            <DataTable
              label="Failed jobs"
              rows={s.jobs.failed_24h}
              rowKey={(j) => j.id}
              empty={<EmptyState>None.</EmptyState>}
              columns={[
                { key: "task", header: "Task", cell: (j) => <span className="mono">{j.task}</span>, sortValue: (j) => j.task },
                { key: "queue", header: "Queue", cell: (j) => j.queue, sortValue: (j) => j.queue },
                { key: "attempts", header: "Attempts", numeric: true, cell: (j) => j.attempts, sortValue: (j) => j.attempts },
                { key: "failed", header: "Failed", cell: (j) => formatDate(j.failed_at), sortValue: (j) => j.failed_at },
                {
                  key: "retry",
                  header: <span className="visually-hidden">Actions</span>,
                  cell: (j) => (
                    <Button
                      size="sm"
                      icon={<RotateCcw size={14} aria-hidden />}
                      pending={retry.isPending && retry.variables === j.id}
                      aria-label={`Retry job ${j.id}`}
                      onClick={() => retry.mutate(j.id)}
                    >
                      Retry
                    </Button>
                  ),
                },
              ]}
            />
            <h3>Scheduled tasks, last run</h3>
            <KeyValue items={s.jobs.periodic.map((p) => [<span className="mono">{p.task}</span>, formatDate(p.last_at)])} />
          </Section>

          <Section id="sys-knowledge" title="Knowledge upkeep">
            <KeyValue
              items={[
                [
                  "Wiki reconcile",
                  s.wiki_reconcile ? (
                    <span className="mono small">{JSON.stringify(s.wiki_reconcile)}</span>
                  ) : (
                    <span className="muted">not run yet</span>
                  ),
                ],
                ["Chunks that failed embedding 5 times", s.embeddings.failed],
              ]}
            />
            {s.embeddings.sample.length > 0 && (
              <ul>
                {s.embeddings.sample.map((c) => (
                  <li key={c.chunk_id} className="small">
                    chunk {c.chunk_id} (document {c.document_id}): {c.error}
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </>
      )}
    </div>
  );
}
