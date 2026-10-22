import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type Insights, type InsightsOptions } from "../../api";
import { useAuth } from "../../auth";
import { usePageTitle } from "../../app/pageTitle";
import {
  BarChart,
  ColumnChart,
  EmptyState,
  ErrorNote,
  PageHeader,
  Select,
  SkeletonRows,
  StatTile,
  TextField,
  formatUsd,
  statusLabel,
  type ChartRow,
} from "../../components/ui";

const FILTERS = ["from", "to", "project_id", "issue_type", "scope"] as const;

/** "45 min", "3 h 20 min", "2 d 4 h". */
export function formatMinutes(min: number | null): string {
  if (min === null) return "—";
  const m = Math.round(min);
  if (m < 60) return `${m} min`;
  if (m < 48 * 60) return `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ""}`;
  const h = Math.round(m / 60);
  return `${Math.floor(h / 24)} d${h % 24 ? ` ${h % 24} h` : ""}`;
}

const humanize = (code: string) => (code.charAt(0).toUpperCase() + code.slice(1)).replaceAll("_", " ");

function outcomeLabel(o: Insights["outcomes"][number]): string {
  if (o.outcome === "in_progress") return "In progress";
  return o.outcome === "failed" && o.error_code ? `Failed: ${humanize(o.error_code).toLowerCase()}` : statusLabel(o.outcome);
}

const weekLabel = (iso: string) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, { day: "numeric", month: "short", timeZone: "UTC" });

const count = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function buckets(d: Insights["attempts"]): ChartRow[] {
  return d.distribution.map((b) => ({ key: String(b.value), label: b.label, value: b.jobs, display: count(b.jobs, "job") }));
}

/** Insights (08 §F9): how jobs ended, how long they took and what they cost, in a period of local dates. */
export default function InsightsPage() {
  usePageTitle("Insights");
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const scope = params.get("scope") === "all" && user?.role === "admin" ? "all" : "me";
  const query = new URLSearchParams();
  for (const k of FILTERS) {
    const v = k === "scope" ? scope : params.get(k);
    if (v) query.set(k, v);
  }
  const data = useQuery({ queryKey: ["insights", query.toString()], queryFn: () => api<Insights>(`/api/insights?${query}`) });
  const options = useQuery({
    queryKey: ["insights-options", scope],
    queryFn: () => api<InsightsOptions>(`/api/insights/options?scope=${scope}`),
  });
  const set = (key: (typeof FILTERS)[number], value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };
  const d = data.data;

  return (
    <div className="page">
      <PageHeader title="Insights" />
      <form className="row" aria-label="Filters" onSubmit={(e) => e.preventDefault()}>
        <TextField label="From" type="date" value={params.get("from") ?? d?.start ?? ""} onChange={(e) => set("from", e.target.value)} />
        <TextField label="To" type="date" value={params.get("to") ?? d?.end ?? ""} onChange={(e) => set("to", e.target.value)} />
        <Select
          label="Repository"
          value={params.get("project_id") ?? ""}
          options={[["", "All repositories"], ...(options.data?.projects ?? []).map((p) => [String(p.id), p.name] as [string, string])]}
          onChange={(e) => set("project_id", e.target.value)}
        />
        <Select
          label="Issue type"
          value={params.get("issue_type") ?? ""}
          options={[["", "All types"], ...(options.data?.issue_types ?? []).map((t) => [t, t] as [string, string])]}
          onChange={(e) => set("issue_type", e.target.value)}
        />
        {user?.role === "admin" && (
          <Select
            label="Whose jobs"
            value={scope}
            options={[["me", "Mine"], ["all", "Everyone's"]]}
            onChange={(e) => set("scope", e.target.value === "all" ? "all" : "")}
          />
        )}
      </form>
      <ErrorNote error={data.error ?? options.error} />
      {data.isLoading && <SkeletonRows rows={4} />}
      {d && d.jobs === 0 && (
        <EmptyState action={<Link to="/work">Open the Board</Link>}>
          No jobs started between these dates{scope === "me" ? "" : " by anyone"}. Approve a plan and start a job to see how it goes.
        </EmptyState>
      )}
      {d && d.jobs > 0 && (
        <>
          <p className="muted">
            Jobs started from {d.start} to {d.end}, in {d.timezone} time.
          </p>
          <div className="kpis">
            <StatTile label="Jobs" value={d.jobs} hint={`${d.merged} merged (${Math.round((100 * d.merged) / d.jobs)} %)`} />
            <StatTile label="Start to review" value={formatMinutes(d.median_minutes_to_review)} hint="Median" />
            <StatTile label="Push to merge" value={formatMinutes(d.median_minutes_push_to_merge)} hint="Median" />
            <StatTile
              label="AI cost per merged change"
              value={d.cost.per_merged_usd === null ? "—" : formatUsd(d.cost.per_merged_usd)}
              hint="The job and its plan"
            />
            <StatTile
              label="AI cost"
              value={formatUsd(d.cost.total_usd)}
              hint={`${formatUsd(d.cost.jobs_usd)} jobs · ${formatUsd(d.cost.planning_usd)} planning`}
            />
          </div>
          <div className="card-grid">
            <BarChart
              title="How jobs ended"
              valueHeader="Jobs"
              rows={d.outcomes.map((o) => ({
                key: `${o.outcome}:${o.error_code ?? ""}`,
                label: outcomeLabel(o),
                value: o.jobs,
                display: count(o.jobs, "job"),
              }))}
            />
            <BarChart
              title="AI cost by outcome"
              valueHeader="AI cost"
              rows={d.outcomes.map((o) => ({
                key: `${o.outcome}:${o.error_code ?? ""}`,
                label: outcomeLabel(o),
                value: o.cost_usd,
                display: formatUsd(o.cost_usd),
              }))}
            />
          </div>
          <ColumnChart
            title="AI cost per week"
            valueHeader="AI cost"
            rows={d.weeks.map((w) => ({ key: w.week_start, label: weekLabel(w.week_start), value: w.cost_usd, display: formatUsd(w.cost_usd) }))}
          />
          <div className="card-grid">
            <BarChart title="Attempts per job" valueHeader="Jobs" rows={buckets(d.attempts)} />
            <BarChart title="Review rounds per job" valueHeader="Jobs" rows={buckets(d.review_rounds)} />
            <BarChart title="Plan revisions before approval" valueHeader="Jobs" rows={buckets(d.plan_revisions)} />
          </div>
        </>
      )}
    </div>
  );
}
