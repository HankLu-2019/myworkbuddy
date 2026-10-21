import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CalendarClock } from "lucide-react";
import { api, type ProposalCard as Card, type Schedule, type ScheduleIn } from "../../api";
import { Button, Drawer, ErrorNote, formatDate, useToast } from "../../components/ui";
import { ScheduleForm } from "../schedules/ScheduleForm";
import { NOTIFY_LABEL } from "../schedules/presets";

/**
 * A schedule the assistant proposed (02 §6, 07 §5.6). Nothing exists until the user clicks Create, or saves the
 * pre-filled form from Edit.
 */
export function ProposalCard({ card }: { card: Card }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [state, setState] = useState<{ status: Card["status"]; scheduleId?: string | null }>({
    status: card.status ?? "pending",
    scheduleId: card.schedule_id,
  });
  const created = (s: Schedule) => {
    setState({ status: "accepted", scheduleId: s.id });
    setEditing(false);
    qc.invalidateQueries({ queryKey: ["schedules"] });
    qc.invalidateQueries({ queryKey: ["messages"] });
    toast({ title: `Schedule “${s.name}” created`, tone: "success" });
  };
  const accept = useMutation({
    mutationFn: (body?: ScheduleIn) =>
      api<Schedule>(`/api/schedules/proposals/${card.proposal_id}/accept`, { method: "POST", json: body ?? {} }),
    onSuccess: created,
  });
  const dismiss = useMutation({
    mutationFn: () => api(`/api/schedules/proposals/${card.proposal_id}/dismiss`, { method: "POST" }),
    onSuccess: () => setState({ status: "dismissed" }),
  });
  const expired = state.status === "pending" && Date.parse(card.expires_at) <= Date.now();
  const status = expired ? "expired" : state.status;

  return (
    <section className="proposal-card" aria-label={`Proposed schedule: ${card.name}`}>
      <div className="row">
        <CalendarClock size={18} aria-hidden />
        <strong className="grow">{card.name}</strong>
        <span className="muted small">{card.agent_name ?? card.agent}</span>
      </div>
      <p className="small">
        {card.description ?? card.cron} ({card.timezone}) · {NOTIFY_LABEL[card.notify_mode]}
        {card.next_runs?.length ? ` · Next: ${card.next_runs.slice(0, 3).map(formatDate).join(", ")}` : ""}
      </p>
      {Object.keys(card.inputs ?? {}).length > 0 && <p className="muted small mono">{JSON.stringify(card.inputs)}</p>}
      {status === "pending" && (
        <div className="row">
          <Button variant="primary" size="sm" onClick={() => accept.mutate(undefined)} pending={accept.isPending && !editing}>
            Create
          </Button>
          <Button size="sm" onClick={() => setEditing(true)}>
            Edit
          </Button>
          <Button size="sm" variant="ghost" onClick={() => dismiss.mutate()} pending={dismiss.isPending}>
            Dismiss
          </Button>
        </div>
      )}
      {status === "accepted" && (
        <p className="row small" role="status">
          Schedule created {state.scheduleId && <Link to={`/schedules/${state.scheduleId}`}>Open</Link>}
        </p>
      )}
      {status === "dismissed" && <p className="muted small">Dismissed.</p>}
      {status === "expired" && <p className="muted small">This proposal expired. Ask again to get a new one.</p>}
      <ErrorNote error={editing ? null : (accept.error ?? dismiss.error)} />
      <Drawer open={editing} onOpenChange={setEditing} title="Edit the proposed schedule">
        <ScheduleForm
          initial={{ ...card, prompt: card.prompt ?? "" }}
          draftId={`proposal-${card.proposal_id}`}
          submitLabel="Create schedule"
          onSubmit={(body) => accept.mutate(body)}
          pending={accept.isPending}
          saved={accept.isSuccess}
          error={accept.error}
        />
      </Drawer>
    </section>
  );
}
