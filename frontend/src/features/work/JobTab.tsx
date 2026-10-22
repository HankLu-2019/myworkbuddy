import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  apiText,
  parseEvent,
  type Job,
  type JobEvent,
  type JobPage,
  type JobPush,
  type JobResult,
  type JobStatus,
  type Plan,
  type PushPayload,
  type WorkItem,
  type WorkItemDetail,
} from "../../api";
import {
  Banner,
  Button,
  Card,
  Checkbox,
  CodeBlock,
  Dialog,
  ConfirmDialog,
  EmptyState,
  ErrorNote,
  KeyValue,
  Markdown,
  SkeletonRows,
  StatusPill,
  TextArea,
  TextField,
  formatElapsed,
  formatUsd,
  statusLabel,
  useToast,
} from "../../components/ui";
import { PatchView } from "../../components/Patch";

/** Statuses in which the job works on its own (the timeline stays live). */
export const WORKING: readonly string[] = ["created", "preparing", "running", "verifying", "approved_push", "pushing"];
/** …of which these can be cancelled (a push in flight can't). */
const CANCELLABLE: readonly string[] = ["created", "preparing", "running", "verifying"];
const PUSH_ACK = "push despite failing checks";
const ENDED_BADLY: readonly string[] = ["failed", "timed_out", "budget_exceeded"];
const STAGE: Record<string, string> = {
  preparing: "preparing the workspace",
  running: "running the agent",
  verifying: "verifying",
  pushing: "pushing",
};

/** The latest job of a work item (the Job tab shows that one). */
export function useLatestJob(workItemId: string, jobId?: string) {
  return useQuery({
    queryKey: ["jobs", "for", workItemId, jobId ?? "latest"],
    queryFn: async () =>
      jobId ? api<Job>(`/api/jobs/${jobId}`) : ((await api<JobPage>(`/api/jobs?work_item_id=${workItemId}&limit=1`)).items[0] ?? null),
    refetchInterval: (q) => (q.state.data && WORKING.includes(q.state.data.status) ? 10_000 : false),
  });
}

/** Start a job from the ticket's approved plan, after a confirmation that states the limits (07 §5.9 step 1). */
export function StartJobButton({ item, primary = true, size }: { item: Pick<WorkItem, "id" | "key" | "current_plan_id">; primary?: boolean; size?: "sm" }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const plan = useQuery({
    queryKey: ["plans", item.current_plan_id],
    queryFn: () => api<Plan>(`/api/plans/${item.current_plan_id}`),
    enabled: open && !!item.current_plan_id,
  });
  const start = useMutation({
    mutationFn: () => api<Job>(`/api/work-items/${item.id}/jobs`, { method: "POST", json: { plan_revision_id: plan.data!.revision!.id } }),
    onSuccess: (job) => {
      qc.invalidateQueries({ queryKey: ["work"] });
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["activity"] });
      setOpen(false);
      toast({ title: `Job started for ${item.key}; you can leave this page`, tone: "success" });
      navigate(`/work/${job.work_item_id}/job`);
    },
  });
  const sandbox = plan.data?.revision?.content.sandbox;
  const network = sandbox?.optional_hosts.length ? `package mirrors and ${sandbox.optional_hosts.join(", ")}` : "package mirrors only";
  return (
    <>
      <Button variant={primary ? "primary" : "secondary"} size={size} onClick={() => setOpen(true)}>
        Start job
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={(o) => {
          setOpen(o);
          if (!o) start.reset();
        }}
        title={`Start a job for ${item.key}?`}
        body={
          <div className="stack-tight">
            {sandbox ? (
              <p>
                Up to {sandbox.timeout_min} min, {sandbox.max_attempts} {sandbox.max_attempts === 1 ? "attempt" : "attempts"}, within your AI budget;
                network: {network}.
              </p>
            ) : plan.error ? null : (
              <SkeletonRows rows={1} />
            )}
            <p className="muted small">The agent works in a sandbox on a copy of the repository. Nothing is pushed until you review and approve.</p>
            <ErrorNote error={plan.error ?? start.error} />
          </div>
        }
        confirmLabel="Start job"
        pending={start.isPending}
        disabled={!plan.data?.revision}
        onConfirm={() => plan.data?.revision && start.mutate()}
      />
    </>
  );
}

/** Live job events: SSE with replay from the last seq on reconnect (07 §10); EventSource resends Last-Event-ID. */
export function useJobEvents(jobId: string | undefined, live: boolean, onStatus?: () => void) {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const last = useRef(0);
  const notify = useRef(onStatus);
  notify.current = onStatus;
  useEffect(() => {
    setEvents([]);
    last.current = 0;
  }, [jobId]);
  useEffect(() => {
    if (!jobId) return;
    const add = (e: JobEvent) => {
      if (e.seq <= last.current) return;
      last.current = e.seq;
      setEvents((prev) => [...prev, e]);
      if (e.type === "status") notify.current?.();
    };
    if (typeof EventSource === "undefined") {
      // No EventSource (old browsers, tests): read the stream once; it ends at once for a job that isn't working.
      const ctl = new AbortController();
      fetch(`/api/jobs/${jobId}/events?after=${last.current}`, { credentials: "same-origin", signal: ctl.signal })
        .then((r) => (r.ok ? r.text() : ""))
        .then((body) => {
          for (const block of body.split("\n\n")) {
            if (!block.trim() || block.startsWith(":")) continue;
            const { event, data } = parseEvent(block);
            if (event !== "end" && data && typeof data === "object") add({ ...data, type: event } as JobEvent);
          }
        })
        .catch(() => undefined);
      return () => ctl.abort();
    }
    const source = new EventSource(`/api/jobs/${jobId}/events?after=${last.current}`, { withCredentials: true });
    const types: JobEvent["type"][] = ["status", "agent_text", "agent_tool", "verify_cmd", "check", "push", "error"];
    const handler = (type: JobEvent["type"]) => (msg: MessageEvent) => {
      try {
        add({ ...JSON.parse(msg.data), type });
      } catch {
        /* a broken line: skip it */
      }
    };
    const handlers = types.map((t) => [t, handler(t)] as const);
    for (const [t, h] of handlers) source.addEventListener(t, h as EventListener);
    source.addEventListener("end", () => {
      source.close();
      notify.current?.();
    });
    return () => source.close();
    // `live` restarts the stream when a stopped job starts working again (request changes).
  }, [jobId, live]);
  return events;
}

function eventText(e: JobEvent): string {
  switch (e.type) {
    case "status":
      return e.note ? String(e.note) : `${statusLabel(String(e.to))}${e.from ? ` (was ${statusLabel(String(e.from)).toLowerCase()})` : ""}`;
    case "agent_tool":
      return `${e.tool}: ${e.summary}${e.more ? ` (+${e.more} more)` : ""}`;
    case "verify_cmd":
      return `\`${e.command}\` ${e.timed_out ? "timed out" : e.exit_code === 0 ? "passed" : `failed (exit ${e.exit_code})`}`;
    case "check":
      return `Check ${e.name}: ${e.status}${e.detail ? `. ${e.detail}` : ""}`;
    case "error":
      return String(e.text ?? "Error");
    case "push":
      return String(e.note ?? e.text ?? "Push update");
    default:
      return "";
  }
}

const EVENT_LABEL: Record<JobEvent["type"], string> = {
  status: "Status",
  agent_text: "Agent",
  agent_tool: "Tool",
  verify_cmd: "Verify",
  check: "Check",
  push: "Push",
  error: "Problem",
};

export function Timeline({ events }: { events: JobEvent[] }) {
  if (!events.length) return <p className="muted">No events yet.</p>;
  return (
    // Focusable: once long, the list scrolls, and keyboard users must be able to scroll it too.
    <ol className="timeline" aria-label="Job timeline" tabIndex={0}>
      {events.map((e) => (
        <li key={e.seq} className={`timeline-item tl-${e.type}`}>
          <span className="muted small mono">{new Date(e.at).toLocaleTimeString()}</span> <strong className="small">{EVENT_LABEL[e.type] ?? e.type}</strong>{" "}
          {e.type === "agent_text" ? <span className="pre-wrap">{String(e.text)}</span> : <span>{eventText(e)}</span>}
        </li>
      ))}
    </ol>
  );
}

const CHECK_LABEL: Record<string, string> = {
  no_changes: "Has changes",
  outside_plan: "Stays within the plan",
  size: "Size",
  binary: "Binary files",
  file_modes: "Symlinks, submodules, executables",
  ci_config: "CI configuration",
  secret_scan: "Secret scan",
  verification: "Verification commands",
};
const CHECK_STATE: Record<string, string> = { pass: "succeeded", warn: "needs_decision", fail: "failed" };
const CHECK_WORD: Record<string, string> = { pass: "Passed", warn: "Warning", fail: "Failed" };

export function ChecksList({ checks }: { checks: JobResult["checks"] }) {
  return (
    <ul className="plain" aria-label="Checks">
      {checks.map((c) => (
        <li key={c.name} className="row">
          <StatusPill status={CHECK_STATE[c.status] ?? c.status} label={CHECK_WORD[c.status] ?? c.status} />
          <span>
            <strong>{CHECK_LABEL[c.name] ?? c.name}</strong>
            {c.detail ? <span className="muted"> · {c.detail}</span> : null}
          </span>
        </li>
      ))}
    </ul>
  );
}

const TAIL = 200;

/** One verification command; its log loads when opened and shows the last lines. */
function VerifyLog({ v }: { v: JobResult["verify"][number] }) {
  const [open, setOpen] = useState(false);
  const log = useQuery({ queryKey: ["artifact", v.log_artifact_id], queryFn: () => apiText(`/api/artifacts/${v.log_artifact_id}`), enabled: open && !!v.log_artifact_id });
  const lines = log.data?.split("\n") ?? [];
  const ok = v.exit_code === 0 && !v.timed_out;
  return (
    <details onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>
        <span className="mono">{v.command}</span> · {v.timed_out ? "timed out" : ok ? "passed" : `failed (exit ${v.exit_code})`} · {Math.round(v.duration_s)} s
      </summary>
      {log.isLoading ? (
        <SkeletonRows rows={2} />
      ) : log.error ? (
        <ErrorNote error={log.error} />
      ) : (
        <>
          {lines.length > TAIL && <p className="muted small">Last {TAIL} lines of {lines.length}.</p>}
          <CodeBlock>{lines.slice(-TAIL).join("\n")}</CodeBlock>
        </>
      )}
    </details>
  );
}

function RequestChanges({ job }: { job: Job }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [feedback, setFeedback] = useState("");
  const send = useMutation({
    mutationFn: () => api<Job>(`/api/jobs/${job.id}/request-changes`, { method: "POST", json: { feedback } }),
    onSuccess: () => {
      setFeedback("");
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["work"] });
      toast({ title: "Feedback sent; the agent is working on it", tone: "success" });
    },
  });
  return (
    <form
      className="stack-tight"
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        if (feedback.trim()) send.mutate();
      }}
    >
      <TextArea
        label="What should change?"
        value={feedback}
        maxLength={4000}
        rows={4}
        hint="The agent gets this as feedback and works on the same branch."
        onChange={(e) => setFeedback(e.target.value)}
      />
      <div className="row">
        <Button type="submit" pending={send.isPending} disabled={!feedback.trim()}>
          Request changes
        </Button>
      </div>
      <ErrorNote error={send.error} />
    </form>
  );
}

/** The push preview (07 §4.10): the exact outbound content, texts editable, then one approval (04 §5.6). */
export function PushDialog({ job, open, onOpenChange }: { job: Job; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const preview = useQuery({
    queryKey: ["jobs", job.id, "push-preview", job.updated_at],
    queryFn: () => api<PushPayload>(`/api/jobs/${job.id}/push-preview`),
    enabled: open,
  });
  const [draft, setDraft] = useState<PushPayload | null>(null);
  useEffect(() => {
    if (open && preview.data) setDraft(structuredClone(preview.data));
    if (!open) setDraft(null);
  }, [open, preview.data]);
  const failed = !!(job.result as JobResult | null)?.failed_checks;
  const approve = useMutation({
    mutationFn: (payload: PushPayload) => api<Job>(`/api/jobs/${job.id}/approve-push`, { method: "POST", json: payload }),
    onSuccess: () => {
      onOpenChange(false);
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["work"] });
      toast({ title: "Push approved; pushing now", tone: "success" });
    },
  });
  const gerrit = draft?.target.host === "gerrit";
  const acked = !!draft?.acknowledgements?.includes(PUSH_ACK);
  const set = (fn: (d: PushPayload) => void) =>
    setDraft((d) => {
      if (!d) return d;
      const next = structuredClone(d);
      fn(next);
      return next;
    });
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o);
        if (!o) approve.reset();
      }}
      title="Review push"
      description="This is exactly what will be sent. You can edit the texts."
      actions={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" pending={approve.isPending} disabled={!draft || (failed && !acked)} onClick={() => draft && approve.mutate(draft)}>
            {gerrit ? "Upload change to Gerrit" : "Push branch and open merge request"}
          </Button>
        </>
      }
    >
      {preview.error ? (
        <ErrorNote error={preview.error} />
      ) : !draft ? (
        <SkeletonRows rows={4} />
      ) : (
        <div className="stack">
          <KeyValue
            items={[
              ["Repository", draft.target.repo_url],
              [gerrit ? "Change for" : "Branch", gerrit ? `${draft.target.base_branch} (topic ${draft.gerrit?.topic ?? ""})` : `${draft.target.branch} → ${draft.target.base_branch}`],
              ["Author", `${draft.commit.author_name} <${draft.commit.author_email}>`],
            ]}
          />
          <TextArea
            label="Commit message"
            className="mono"
            rows={6}
            value={draft.commit.message}
            hint={gerrit ? "Keep the Change-Id line: later rounds become new patch sets of the same change." : undefined}
            onChange={(e) => set((d) => void (d.commit.message = e.target.value))}
          />
          {draft.merge_request && (
            <>
              <TextField label="Merge request title" value={draft.merge_request.title} onChange={(e) => set((d) => void (d.merge_request!.title = e.target.value))} />
              <TextArea
                label="Merge request description"
                rows={6}
                value={draft.merge_request.description}
                onChange={(e) => set((d) => void (d.merge_request!.description = e.target.value))}
              />
            </>
          )}
          {draft.jira_comment ? (
            <>
              <Checkbox label="Comment on the Jira ticket" checked={draft.jira_comment.enabled} onChange={(v) => set((d) => void (d.jira_comment!.enabled = v))} />
              {draft.jira_comment.enabled && (
                <TextArea
                  label="Jira comment"
                  rows={4}
                  value={draft.jira_comment.body}
                  hint="<link placeholder> becomes the link to the review request."
                  onChange={(e) => set((d) => void (d.jira_comment!.body = e.target.value))}
                />
              )}
            </>
          ) : (
            <p className="muted small">No Jira comment: it was posted on an earlier round, or you have no Jira access.</p>
          )}
          {failed && (
            <Checkbox
              label="Push despite failing checks"
              checked={acked}
              onChange={(v) => set((d) => void (d.acknowledgements = v ? [PUSH_ACK] : []))}
            />
          )}
          <ErrorNote error={approve.error} />
        </div>
      )}
    </Dialog>
  );
}

/** After the push (07 §4.10): the review request and Jira comment links, and whether it's merged. */
function Pushed({ job }: { job: Job }) {
  const push = (job.push ?? {}) as JobPush;
  const link = push.mr_url ?? push.change_url;
  const word = push.change_url ? "Gerrit change" : "Merge request";
  return (
    <Card as="section" className="stack" aria-labelledby="job-pushed">
      <h2 id="job-pushed">Review request</h2>
      <ul className="plain">
        {link && (
          <li>
            <a href={link} target="_blank" rel="noreferrer">
              {word}
            </a>
          </li>
        )}
        {push.jira_comment_url && (
          <li>
            <a href={push.jira_comment_url} target="_blank" rel="noreferrer">
              Jira comment
            </a>
          </li>
        )}
      </ul>
      {job.status === "pushed" && <p className="muted">Watching for merge.</p>}
      {job.status === "merged" && <Banner tone="success">Merged.</Banner>}
      {job.status === "closed" && <Banner tone="warning">The review request was closed without merging.</Banner>}
    </Card>
  );
}

function Review({ job, result }: { job: Job; result: JobResult }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [discarding, setDiscarding] = useState(false);
  const [pushing, setPushing] = useState(false);
  const patch = useQuery({ queryKey: ["jobs", job.id, "patch", result.patch_sha256], queryFn: () => apiText(`/api/jobs/${job.id}/patch`) });
  const discard = useMutation({
    mutationFn: () => api<Job>(`/api/jobs/${job.id}/discard`, { method: "POST" }),
    onSuccess: () => {
      setDiscarding(false);
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["work"] });
      toast({ title: "Changes discarded; the ticket is back to Approved", tone: "success" });
    },
  });
  const reviewing = job.status === "awaiting_review";
  const added = result.files.reduce((n, f) => n + f.added, 0);
  const removed = result.files.reduce((n, f) => n + f.removed, 0);
  return (
    <div className="stack">
      {result.failed_checks && reviewing && <Banner tone="warning">Some checks failed after the last attempt. Look at them before you push.</Banner>}
      <Card as="section" className="stack" aria-labelledby="job-files">
        <h2 id="job-files">
          Files changed <span className="muted small">{result.files.length} files, +{added} −{removed}</span>
        </h2>
        <ul className="plain small mono" aria-label="Files">
          {result.files.map((f) => (
            <li key={f.path}>
              {f.status} {f.path} {f.binary ? "(binary)" : `+${f.added} −${f.removed}`}
            </li>
          ))}
        </ul>
        {patch.error ? <ErrorNote error={patch.error} /> : patch.data === undefined ? <SkeletonRows rows={4} /> : <PatchView patch={patch.data} />}
      </Card>
      <Card as="section" className="stack" aria-labelledby="job-checks">
        <h2 id="job-checks">Checks</h2>
        <ChecksList checks={result.checks} />
      </Card>
      <Card as="section" className="stack" aria-labelledby="job-verify">
        <h2 id="job-verify">Verification logs</h2>
        {result.verify.length ? result.verify.map((v, i) => <VerifyLog key={i} v={v} />) : <p className="muted">The plan has no verification commands.</p>}
      </Card>
      <Card as="section" className="stack" aria-labelledby="job-notes">
        <h2 id="job-notes">
          Agent notes <span className="muted small">Written by the agent</span>
        </h2>
        {result.agent_notes.trim() ? <Markdown>{result.agent_notes}</Markdown> : <p className="muted">The agent left no notes.</p>}
      </Card>
      {(reviewing || job.status === "pushed") && (
        <Card as="section" className="stack" aria-labelledby="job-actions">
          <h2 id="job-actions">Your review</h2>
          {reviewing && (
            <div className="row">
              <Button variant="primary" onClick={() => setPushing(true)}>
                Review push
              </Button>
              <Button variant="danger" onClick={() => setDiscarding(true)}>
                Discard
              </Button>
            </div>
          )}
          <RequestChanges job={job} />
        </Card>
      )}
      <PushDialog job={job} open={pushing} onOpenChange={setPushing} />
      <ConfirmDialog
        open={discarding}
        onOpenChange={setDiscarding}
        title="Discard these changes?"
        body="The sandbox and its changes are deleted and the ticket goes back to Approved. You can start a new job from the same plan."
        confirmLabel="Discard"
        danger
        pending={discard.isPending}
        onConfirm={() => discard.mutate()}
      />
      <ErrorNote error={discard.error} />
    </div>
  );
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

/** The hub's Job tab (07 §4.10): timeline, current step, then the review (files, checks, logs, notes, cost). */
export default function JobTab({ item, jobId }: { item: WorkItemDetail; jobId?: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const latest = useLatestJob(item.id, jobId);
  const job = latest.data ?? undefined;
  const working = !!job && WORKING.includes(job.status);
  const events = useJobEvents(job?.id, working, () => {
    qc.invalidateQueries({ queryKey: ["jobs", "for", item.id] });
    qc.invalidateQueries({ queryKey: ["work", "item", item.id] });
  });
  const now = useNow(working);
  const [cancelling, setCancelling] = useState(false);
  const cancel = useMutation({
    mutationFn: () => api<Job>(`/api/jobs/${job!.id}/cancel`, { method: "POST" }),
    onSuccess: () => {
      setCancelling(false);
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["work"] });
      toast({ title: "Job cancelled", tone: "success" });
    },
  });
  if (latest.error) return <ErrorNote error={latest.error} />;
  if (latest.isLoading) return <SkeletonRows rows={4} />;
  if (!job)
    return (
      <EmptyState action={item.state === "approved" ? <StartJobButton item={item} /> : undefined}>
        {item.state === "approved" ? "No job yet. Start one to have the agent make the planned changes." : "A job can start once the plan is approved."}
      </EmptyState>
    );
  const result = job.result as JobResult | null;
  const status = job.status as JobStatus;
  return (
    <div className="stack">
      <Card as="section" className="stack" aria-labelledby="job-status">
        <div className="row">
          <h2 id="job-status" className="grow">
            Job <StatusPill status={status} />
          </h2>
          {CANCELLABLE.includes(job.status) && (
            <Button variant="danger" size="sm" onClick={() => setCancelling(true)}>
              Cancel
            </Button>
          )}
        </div>
        <KeyValue
          items={[
            ["Step", working ? `${statusLabel(status)}${job.started_at ? ` · ${formatElapsed(job.started_at, now)}` : ""}` : statusLabel(status)],
            ["Attempt", `Attempt ${job.attempt} of ${job.max_attempts}${job.review_round ? ` · review round ${job.review_round + 1}` : ""}`],
            ["Branch", `${job.branch_name} from ${job.base_branch}`],
            ["Cost", formatUsd(result?.cost_usd ?? job.cost_usd)],
          ]}
        />
        {ENDED_BADLY.includes(status) && (
          <Banner tone="danger">
            Job stopped{job.failed_stage ? ` at ${STAGE[job.failed_stage] ?? job.failed_stage}` : ""}: {job.error ?? statusLabel(status)}
          </Banner>
        )}
        {(ENDED_BADLY.includes(status) || status === "cancelled" || status === "discarded") && item.state === "approved" && (
          <div className="row">
            <StartJobButton item={item} />
          </div>
        )}
        <ErrorNote error={cancel.error} />
      </Card>
      {["pushed", "merged", "closed"].includes(status) && <Pushed job={job} />}
      {result && <Review job={job} result={result} />}
      <Card as="section" className="stack" aria-labelledby="job-timeline">
        <h2 id="job-timeline">Timeline {working && <span className="muted small">Live</span>}</h2>
        <Timeline events={events} />
      </Card>
      <ConfirmDialog
        open={cancelling}
        onOpenChange={setCancelling}
        title="Cancel this job?"
        body="The agent stops and the sandbox is deleted. The ticket goes back to Approved."
        confirmLabel="Cancel job"
        danger
        pending={cancel.isPending}
        onConfirm={() => cancel.mutate()}
      />
    </div>
  );
}
