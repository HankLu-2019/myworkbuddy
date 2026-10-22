import { Link } from "react-router-dom";
import { AlertTriangle, CheckCircle2, Info, OctagonAlert } from "lucide-react";
import type { Finding, Report, RunDetail } from "../../api";
import { Banner, Card, KeyValue, Markdown, StatusPill, formatDate, formatUsd } from "../../components/ui";
import { describeError } from "../../app/errors";
import { ApiError } from "../../api";

export const ACTIVE = ["queued", "running"];

/** "gitlab.example.com/team/etl/-/jobs/1" as text: links in findings come from untrusted output (07 §4.8). */
export function linkText(url: string): { host: string; rest: string } {
  try {
    const u = new URL(url);
    return { host: u.host, rest: `${u.pathname}${u.search}`.replace(/\/$/, "") };
  } catch {
    return { host: "", rest: url };
  }
}

function SeverityIcon({ severity }: { severity: Finding["severity"] }) {
  if (severity === "alert") return <OctagonAlert size={18} aria-hidden className="sev sev-alert" />;
  if (severity === "warning") return <AlertTriangle size={18} aria-hidden className="sev sev-warning" />;
  return <Info size={18} aria-hidden className="sev sev-info" />;
}

const SEVERITY: Record<Finding["severity"], string> = { alert: "Alert", warning: "Warning", info: "Info" };

export function duration(run: RunDetail): string | null {
  if (!run.started_at || !run.finished_at) return null;
  const s = Math.max(0, Math.round((Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

/** What a failed run means and what to do (07 §6.8, §5.10). */
export function runFailure(run: RunDetail): { message: string; action?: { label: string; href: string } } {
  switch (run.error_code) {
    case "budget_exceeded":
      return { message: run.error ?? "Your AI budget is used up.", action: { label: "View usage", href: "/settings/usage" } };
    case "timeout":
      return {
        message: `${run.error ?? "The run took too long."} Narrow the inputs or raise the maximum run time.`,
        action: run.schedule_id ? { label: "Edit schedule", href: `/schedules/${run.schedule_id}/edit` } : undefined,
      };
    case "interrupted":
      return { message: "The worker stopped during the run. Run it again." };
    case "invalid_report":
      return { message: "The model didn't return a usable report. Run it again; if it keeps failing, simplify the instructions." };
    case "usage_limit":
      return { message: "The run needed more steps than allowed. Narrow the inputs (fewer projects or namespaces)." };
    default:
      return describeError(new ApiError(500, run.error ?? "", run.error_code ?? "internal"));
  }
}

/** The body of a run result, used on /runs/:id and in the test-run drawer (07 §4.8). */
export function RunResult({ run }: { run: RunDetail }) {
  const report = run.report as Report | null;
  const fresh = new Set(run.new_fingerprints);
  const d = duration(run);
  return (
    <div className="stack">
      <KeyValue
        items={[
          ["Status", <StatusPill key="s" status={run.status === "succeeded" && report ? report.status : run.status} />],
          ["Scheduled for", formatDate(run.scheduled_for)],
          ...(d ? ([["Duration", d]] as [string, string][]) : []),
          ...(run.cost_usd != null ? ([["Cost", formatUsd(Number(run.cost_usd))]] as [string, string][]) : []),
        ]}
      />
      {ACTIVE.includes(run.status) && (
        <Banner tone="info" role="status">
          {run.status === "queued" ? "Waiting to start…" : "Running the checks — this usually takes under a minute."}
        </Banner>
      )}
      {run.status === "skipped" && <Banner tone="info">Skipped: {run.error}</Banner>}
      {(run.status === "failed" || run.status === "timed_out") && (
        <Banner tone="danger" role="alert">
          {runFailure(run).message}{" "}
          {runFailure(run).action && <Link to={runFailure(run).action!.href}>{runFailure(run).action!.label}</Link>}
        </Banner>
      )}
      {report && (
        <>
          <h2 className="run-headline">{report.headline}</h2>
          {report.findings.length === 0 && (
            <p className="row">
              <CheckCircle2 size={18} aria-hidden className="sev sev-ok" /> Nothing to report.
            </p>
          )}
          <ul className="plain-list stack" aria-label="Findings">
            {report.findings.map((f) => (
              <li key={f.fingerprint}>
                <Card className={`finding finding-${f.severity}`}>
                  <div className="row">
                    <SeverityIcon severity={f.severity} />
                    <strong className="grow">{f.title}</strong>
                    <span className="badge badge-neutral">{SEVERITY[f.severity]}</span>
                    {fresh.has(f.fingerprint) && (
                      <span className="badge badge-new">New since last run</span>
                    )}
                    {f.verified === false && (
                      <span className="badge badge-neutral" title="Not found in what the tools returned; ignored for alerts">
                        Unverified
                      </span>
                    )}
                  </div>
                  {f.detail && <Markdown>{f.detail}</Markdown>}
                  {f.links?.length > 0 && (
                    <ul className="plain-list finding-links" aria-label="Links">
                      {f.links.map((l) => {
                        const { host, rest } = linkText(l);
                        return (
                          <li key={l} className="mono small">
                            <strong>{host}</strong>
                            {rest}
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </Card>
              </li>
            ))}
          </ul>
          {run.resolved.length > 0 && (
            <section aria-labelledby="resolved" className="stack">
              <h3 id="resolved">Resolved since last run</h3>
              <ul className="plain-list stack">
                {run.resolved.map((f) => (
                  <li key={String(f.fingerprint)} className="row">
                    <CheckCircle2 size={16} aria-hidden className="sev sev-ok" /> {String(f.title)}
                  </li>
                ))}
              </ul>
            </section>
          )}
          {report.summary_md && (
            <details className="run-summary" open={report.findings.length > 0}>
              <summary>Summary</summary>
              <Markdown>{report.summary_md}</Markdown>
            </details>
          )}
        </>
      )}
    </div>
  );
}
