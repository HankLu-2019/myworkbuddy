import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, GitCompare, MessageSquare, Pencil, Send, X } from "lucide-react";
import {
  api,
  type Plan,
  type PlanDiff,
  type PlanValidation,
  type Revision,
  type RevisionPage,
  type Session,
  type WorkItemDetail,
} from "../../api";
import { useDraft, draftKey } from "../../app/drafts";
import { useAuth } from "../../auth";
import {
  Banner,
  Button,
  ConfirmDialog,
  Dialog,
  EmptyState,
  ErrorNote,
  RelativeTime,
  Select,
  SkeletonRows,
  TextArea,
  formatUsd,
  useToast,
} from "../../components/ui";
import { DraftPlanButton } from "./BoardPage";
import { PlanEditor } from "./PlanEditor";
import { PlanDiffList, PlanSections, SECTIONS, ValidationPanel, sectionOf } from "./PlanView";

/** Why Approve is disabled, in words (07 §4.10: "disabled with a tooltip listing what's missing"). */
export function approveBlockers(validation: PlanValidation, acks: Set<string>): string[] {
  const out: string[] = [];
  const errors = validation.issues.filter((i) => i.level === "error").length;
  if (errors) out.push(`Fix ${errors} validation ${errors === 1 ? "error" : "errors"} (edit or revise the plan)`);
  const missing = validation.needs_ack.filter((k) => !acks.has(k)).length;
  if (missing) out.push(`Acknowledge ${missing} ${missing === 1 ? "warning" : "warnings"}`);
  return out;
}

function CompareDialog({ plan, open, onOpenChange, to }: { plan: Plan; open: boolean; onOpenChange: (o: boolean) => void; to: number }) {
  const [from, setFrom] = useState(Math.max(1, to - 1));
  const [until, setUntil] = useState(to);
  const diff = useQuery({
    queryKey: ["plans", plan.id, "diff", from, until],
    queryFn: () => api<PlanDiff>(`/api/plans/${plan.id}/diff?from=${from}&to=${until}`),
    enabled: open,
  });
  const options = Array.from({ length: plan.current_revision }, (_, i) => [String(i + 1), `Revision ${i + 1}`] as [string, string]);
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="Compare revisions">
      <div className="row">
        <Select label="From" options={options} value={String(from)} onChange={(e) => setFrom(Number(e.target.value))} />
        <Select label="To" options={options} value={String(until)} onChange={(e) => setUntil(Number(e.target.value))} />
      </div>
      {diff.data ? <PlanDiffList fields={diff.data.fields} /> : diff.error ? <ErrorNote error={diff.error} /> : <SkeletonRows />}
    </Dialog>
  );
}

function DecisionBar({ plan, revision, validation, acks }: { plan: Plan; revision: Revision; validation: PlanValidation; acks: Set<string> }) {
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const { user } = useAuth();
  const feedbackDraft = useDraft(draftKey(user?.id, "plan-feedback", plan.id));
  const feedback = feedbackDraft.value;
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const done = (p: Plan) => {
    qc.setQueryData(["plans", p.id], p);
    qc.invalidateQueries({ queryKey: ["plans", p.id] });
    qc.invalidateQueries({ queryKey: ["work"] });
    qc.invalidateQueries({ queryKey: ["home"] });
  };
  const revise = useMutation({
    mutationFn: () => api<Plan>(`/api/plans/${plan.id}/revise`, { method: "POST", json: { revision: revision.revision, feedback: feedback.trim() } }),
    onSuccess: (p) => {
      feedbackDraft.clear();
      done(p);
      qc.invalidateQueries({ queryKey: ["activity"] });
      toast({ title: `Planner is writing revision ${p.current_revision + 1}; you can leave this page`, tone: "success" });
    },
  });
  const approve = useMutation({
    mutationFn: () =>
      api<Plan>(`/api/plans/${plan.id}/approve`, {
        method: "POST",
        json: { revision: revision.revision, content_hash: revision.content_hash, acknowledged: [...acks] },
      }),
    onSuccess: (p) => {
      done(p);
      toast({ title: `Revision ${revision.revision} approved`, tone: "success" });
    },
  });
  const reject = useMutation({
    mutationFn: () => api<Plan>(`/api/plans/${plan.id}/reject`, { method: "POST", json: { revision: revision.revision, comment: reason.trim() || null } }),
    onSuccess: (p) => {
      setRejecting(false);
      done(p);
      toast({ title: "Plan rejected; the ticket is back in New", tone: "success" });
    },
  });
  const discuss = useMutation({
    mutationFn: () =>
      api<Session>("/api/chat/sessions", {
        method: "POST",
        json: { title: `Discuss plan revision ${revision.revision}`, context: [{ type: "plan_revision", id: revision.id }] },
      }),
    onSuccess: (s) => navigate(`/chat/${s.id}`, { state: { prefill: "What are the weak spots in this plan?" } }),
  });
  const blockers = approveBlockers(validation, acks);
  const busy = revise.isPending || approve.isPending || reject.isPending;
  return (
    <section className="decision-bar stack" aria-label="Decide on this revision">
      <form
        className="row revise-box"
        onSubmit={(e) => {
          e.preventDefault();
          if (feedback.trim()) revise.mutate();
        }}
      >
        <TextArea
          label="Revise with feedback"
          className="grow"
          rows={2}
          maxLength={4000}
          placeholder="Answer a question, ask for a change…"
          value={feedback}
          onChange={(e) => feedbackDraft.setValue(e.target.value)}
          hint={feedbackDraft.restored ? "Restored your unsent feedback." : undefined}
        />
        <Button type="submit" icon={<Send size={16} aria-hidden />} pending={revise.isPending} disabled={!feedback.trim() || busy}>
          Revise with feedback
        </Button>
      </form>
      <div className="row">
        <Button icon={<MessageSquare size={16} aria-hidden />} pending={discuss.isPending} onClick={() => discuss.mutate()}>
          Discuss in chat
        </Button>
        <Button variant="danger" icon={<X size={16} aria-hidden />} disabled={busy} onClick={() => setRejecting(true)}>
          Reject
        </Button>
        <span className="grow" />
        <Button
          variant="primary"
          icon={<Check size={16} aria-hidden />}
          pending={approve.isPending}
          disabled={!!blockers.length || busy}
          aria-describedby={blockers.length ? "approve-why" : undefined}
          onClick={() => approve.mutate()}
        >
          Approve revision {revision.revision}
        </Button>
        <span className="mono small muted" title="Content hash the approval binds to">
          {revision.content_hash.slice(0, 8)}
        </span>
      </div>
      {blockers.length > 0 && (
        <p id="approve-why" className="small muted">
          To approve: {blockers.join("; ")}.
        </p>
      )}
      <ErrorNote error={revise.error ?? approve.error ?? discuss.error} />
      <ConfirmDialog
        open={rejecting}
        onOpenChange={setRejecting}
        title={`Reject revision ${revision.revision}?`}
        body={
          <div className="stack">
            <p>The ticket goes back to New. To try again, draft a new plan; this one stays in its history.</p>
            <TextArea label="Reason (optional)" rows={2} maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} />
            <ErrorNote error={reject.error} />
          </div>
        }
        confirmLabel="Reject plan"
        danger
        pending={reject.isPending}
        onConfirm={() => reject.mutate()}
      />
    </section>
  );
}

/** The Plan tab of the work item hub (07 §4.10). */
export default function PlanTab({ item }: { item: WorkItemDetail }) {
  const [params, setParams] = useSearchParams();
  const [editing, setEditing] = useState(false);
  const [comparing, setComparing] = useState(false);
  const [acks, setAcks] = useState<Set<string>>(new Set());
  const planId = item.current_plan_id;
  const plan = useQuery({
    queryKey: ["plans", planId],
    queryFn: () => api<Plan>(`/api/plans/${planId}`),
    enabled: !!planId,
    refetchInterval: (q) => (q.state.data?.status === "drafting" ? 3000 : false),
  });
  const p = plan.data;
  const asked = Number(params.get("revision")) || 0;
  const selected = p ? (asked >= 1 && asked <= p.current_revision ? asked : p.current_revision) : 0;
  const isCurrent = !!p && selected === p.current_revision;
  const older = useQuery({
    queryKey: ["plans", planId, "revisions", selected],
    queryFn: () => api<Revision>(`/api/plans/${planId}/revisions/${selected}`),
    enabled: !!p && selected >= 1 && !isCurrent,
  });
  const history = useQuery({
    queryKey: ["plans", planId, "revisions", "list", p?.current_revision],
    queryFn: () => api<RevisionPage>(`/api/plans/${planId}/revisions?limit=100`),
    enabled: !!p && p.current_revision > 0,
  });
  const revision = isCurrent ? p?.revision : older.data;
  // "Changed from revision N-1" highlights (07 §5.8 step 4).
  const changes = useQuery({
    queryKey: ["plans", planId, "diff", selected - 1, selected],
    queryFn: () => api<PlanDiff>(`/api/plans/${planId}/diff?from=${selected - 1}&to=${selected}`),
    enabled: !!p && selected > 1,
  });
  const changed = useMemo(() => new Set((changes.data?.fields ?? []).map((f) => sectionOf(f.field))), [changes.data]);
  const problems = useMemo(
    () => new Map((revision?.validation.issues ?? []).filter((i) => i.level === "error").map((i) => [i.field, i.message])),
    [revision],
  );
  // Acknowledgements belong to one revision: start over when it changes.
  useEffect(() => setAcks(new Set()), [revision?.id]);

  if (!planId)
    return (
      <EmptyState action={item.state === "new" ? <DraftPlanButton item={item} /> : undefined}>
        No plan yet. The planner reads the ticket and the repository and proposes one.
      </EmptyState>
    );
  if (plan.error) return <ErrorNote error={plan.error} />;
  if (!p) return <SkeletonRows rows={6} />;

  const status = (
    <>
      {p.status === "drafting" && (
        <Banner tone="info">
          Planner is working on revision {p.current_revision + 1}… usually 1–3 min. You can leave this page; your inbox tells you when it's
          ready.
        </Banner>
      )}
      {p.status === "failed" && (
        <Banner tone="danger">
          Planning failed: {p.error ?? "unknown error"}. {item.state === "new" && <DraftPlanButton item={item} primary={false} />}
        </Banner>
      )}
      {p.status === "needs_decision" && p.error && <Banner tone="warning">The last revise didn't finish: {p.error}</Banner>}
      {p.status === "approved" && (
        <Banner tone="success">
          Revision {p.current_revision} is approved. Starting a job from it arrives with Phase 4.
        </Banner>
      )}
      {p.status === "rejected" && <Banner tone="info">This plan was rejected. Draft a new plan from the ticket when you're ready.</Banner>}
    </>
  );
  if (!revision) return <div className="stack">{status}{p.current_revision > 0 && <SkeletonRows rows={6} />}</div>;

  const validation = revision.validation;
  const decisions = p.approvals;
  const canDecide = isCurrent && p.status === "needs_decision" && !editing;
  const options = (history.data?.items ?? []).map(
    (r) => [String(r.revision), `Revision ${r.revision} of ${p.current_revision} · by ${r.author}${r.ok ? "" : " · has errors"}`] as [string, string],
  );
  return (
    <div className="stack plan-tab">
      {status}
      <div className="row">
        {options.length > 1 ? (
          <Select
            label="Revision"
            hideLabel
            options={options}
            value={String(selected)}
            onChange={(e) => setParams(Number(e.target.value) === p.current_revision ? {} : { revision: e.target.value }, { replace: true })}
          />
        ) : null}
        <span className="muted small grow">
          Revision {revision.revision} of {p.current_revision} · by {revision.author} · <RelativeTime iso={revision.created_at} />
          {revision.cost_usd ? ` · ${formatUsd(revision.cost_usd)}` : ""}
          {revision.change_note ? ` · "${revision.change_note}"` : ""}
        </span>
        {p.current_revision > 1 && (
          <Button size="sm" icon={<GitCompare size={14} aria-hidden />} onClick={() => setComparing(true)}>
            Compare revisions
          </Button>
        )}
        {canDecide && (
          <Button size="sm" icon={<Pencil size={14} aria-hidden />} onClick={() => setEditing(true)}>
            Edit
          </Button>
        )}
        {revision.session_id && (
          <Link to={`/chat/${revision.session_id}`} className="small">
            Planner transcript
          </Link>
        )}
      </div>
      {!isCurrent && <Banner tone="info">You're looking at an older revision. Decisions apply to the latest one.</Banner>}
      {changes.data && changes.data.fields.length > 0 && (
        <Banner tone="info">
          Changed from revision {selected - 1}: {SECTIONS.filter(([id]) => changed.has(id)).map(([, label]) => label).join(", ")}.
        </Banner>
      )}
      {editing && isCurrent ? (
        <PlanEditor plan={p} revision={revision} onDone={() => setEditing(false)} />
      ) : (
        <>
          <ValidationPanel
            validation={validation}
            acks={acks}
            readOnly={!canDecide}
            onAck={(key, on) =>
              setAcks((prev) => {
                const next = new Set(prev);
                if (on) next.add(key);
                else next.delete(key);
                return next;
              })
            }
          />
          <PlanSections doc={revision.content} problems={problems} changed={changed} />
          {decisions.length > 0 && (
            <section className="stack" aria-labelledby="decisions-title">
              <h2 id="decisions-title">Decisions</h2>
              <ul className="plain-list stack-tight">
                {decisions.map((d) => (
                  <li key={`${d.revision_id}-${d.decided_at}`} className="small">
                    Revision {d.revision} {d.decision.replace("_", " ")} <RelativeTime iso={d.decided_at} />
                    {d.acknowledged.length ? ` · acknowledged ${d.acknowledged.length}` : ""}
                    {d.comment ? ` · "${d.comment}"` : ""}
                  </li>
                ))}
              </ul>
            </section>
          )}
          {canDecide && <DecisionBar plan={p} revision={revision} validation={validation} acks={acks} />}
        </>
      )}
      <CompareDialog plan={p} open={comparing} onOpenChange={setComparing} to={selected} />
    </div>
  );
}
