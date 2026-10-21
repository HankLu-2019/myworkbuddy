import { Navigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorNote, PageHeader, formatDate } from "../components/ui";

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

/** Admin → System (design 01 §H14): health, jobs, backups, disk, knowledge upkeep and AI spend. */
export default function SystemPage() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const system = useQuery({
    queryKey: ["system"],
    queryFn: () => api<System>("/api/admin/system"),
    enabled: user?.role === "admin",
    refetchInterval: 30_000,
  });
  const retry = useMutation({
    mutationFn: (id: number) => api(`/api/admin/system/jobs/${id}/retry`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["system"] }),
  });
  if (user?.role !== "admin") return <Navigate to="/chat" replace />;
  const s = system.data;
  return (
    <div className="page">
      <PageHeader title="System" />
      <ErrorNote error={system.error || retry.error} />
      {s && (
        <>
          <section aria-labelledby="sys-alerts">
            <h2 id="sys-alerts">Needs attention</h2>
            {s.alerts.length ? (
              <ul className="alerts" role="alert">
                {s.alerts.map((a) => (
                  <li key={a} className="error-text">
                    {a}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">Everything looks fine.</p>
            )}
          </section>

          <section aria-labelledby="sys-health">
            <h2 id="sys-health">Health</h2>
            <ul>
              {Object.entries(s.checks).map(([k, v]) => (
                <li key={k}>
                  {CHECK_LABELS[k] ?? k}: {v === "ok" ? <span>OK</span> : <span className="error-text">{v}</span>}
                </li>
              ))}
              <li>
                Worker heartbeat:{" "}
                {s.worker.age_s === null ? "none yet" : `${s.worker.age_s} s ago`}
                {s.worker.heartbeat?.host && <span className="muted"> ({s.worker.heartbeat.host})</span>}
              </li>
            </ul>
          </section>

          <section aria-labelledby="sys-jobs">
            <h2 id="sys-jobs">Background jobs</h2>
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
            {s.jobs.failed_24h.length ? (
              <div className="table-wrap">
                <table aria-label="Failed jobs">
                  <thead>
                    <tr>
                      <th>Task</th>
                      <th>Queue</th>
                      <th>Attempts</th>
                      <th>Failed</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {s.jobs.failed_24h.map((j) => (
                      <tr key={j.id}>
                        <td className="mono">{j.task}</td>
                        <td>{j.queue}</td>
                        <td className="num">{j.attempts}</td>
                        <td>{formatDate(j.failed_at)}</td>
                        <td>
                          <button onClick={() => retry.mutate(j.id)} disabled={retry.isPending} aria-label={`Retry job ${j.id}`}>
                            Retry
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted">None.</p>
            )}
            <h3>Scheduled tasks, last run</h3>
            <ul>
              {s.jobs.periodic.map((p) => (
                <li key={p.task}>
                  <span className="mono">{p.task}</span>: {formatDate(p.last_at)}
                </li>
              ))}
            </ul>
          </section>

          <section aria-labelledby="sys-backup">
            <h2 id="sys-backup">Backups</h2>
            {s.backup.status === "not_configured" ? (
              <p className="error-text">Not configured. Set MWB_BACKUP_REPO and RESTIC_PASSWORD (operations guide §5).</p>
            ) : (
              <ul>
                <li>
                  Last successful backup: {s.backup.last_success_at ? formatDate(s.backup.last_success_at) : "never"}
                  {s.backup.snapshot_id && <span className="muted mono"> ({s.backup.snapshot_id.slice(0, 8)})</span>}
                </li>
                {s.backup.status === "failed" && <li className="error-text">Latest attempt failed: {s.backup.error}</li>}
                {s.backup.sizes && (
                  <li>
                    Size:{" "}
                    {Object.entries(s.backup.sizes)
                      .map(([k, v]) => `${k} ${bytes(v)}`)
                      .join(", ")}
                  </li>
                )}
              </ul>
            )}
            <p>
              Last restore drill:{" "}
              {s.backup_drill ? (
                <>
                  {formatDate(s.backup_drill.at)} · {s.backup_drill.ok ? "passed" : <span className="error-text">failed</span>}
                  {s.backup_drill.note && <span className="muted"> · {s.backup_drill.note}</span>}
                </>
              ) : (
                <span className="muted">never recorded</span>
              )}
            </p>
          </section>

          <section aria-labelledby="sys-disk">
            <h2 id="sys-disk">Disk</h2>
            <p>
              {s.disk.path}: {bytes(s.disk.free_bytes)} free of {bytes(s.disk.total_bytes)}
              {s.disk.free_ratio != null && ` (${Math.round(s.disk.free_ratio * 100)} %)`}
            </p>
            {s.disk_guard && !s.disk_guard.ok && <p className="error-text">{s.disk_guard.problem}</p>}
          </section>

          <section aria-labelledby="sys-knowledge">
            <h2 id="sys-knowledge">Knowledge upkeep</h2>
            <ul>
              <li>
                Wiki reconcile:{" "}
                {s.wiki_reconcile ? (
                  <span className="mono small">{JSON.stringify(s.wiki_reconcile)}</span>
                ) : (
                  <span className="muted">not run yet</span>
                )}
              </li>
              <li>
                Chunks that failed embedding 5 times: {s.embeddings.failed}
                {s.embeddings.sample.length > 0 && (
                  <ul>
                    {s.embeddings.sample.map((c) => (
                      <li key={c.chunk_id} className="small">
                        chunk {c.chunk_id} (document {c.document_id}): {c.error}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            </ul>
          </section>

          <section aria-labelledby="sys-spend">
            <h2 id="sys-spend">AI spend this month</h2>
            <p>
              USD {s.llm_spend.month_to_date_usd.toFixed(2)} across {s.llm_spend.users_counted} users
              {s.llm_spend.users_unknown > 0 && <span className="muted"> ({s.llm_spend.users_unknown} unknown)</span>}
            </p>
          </section>
        </>
      )}
    </div>
  );
}
