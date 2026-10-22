import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Project, type TicketCreated, type TicketDraft } from "../../api";
import { Button, Checkbox, Dialog, ErrorNote, Select, SkeletonRows, TextArea, TextField, useToast } from "../../components/ui";

/** Create a Jira issue from a CI finding (08 §F6): every field editable; what's shown is what's sent. */
export function TicketDialog({ runId, fingerprint, open, onOpenChange }: { runId: string; fingerprint: string; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const repos = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/api/projects"), enabled: open });
  const draft = useQuery({
    queryKey: ["runs", runId, "ticket-draft", fingerprint],
    queryFn: () => api<TicketDraft>(`/api/runs/${runId}/ticket-draft?fingerprint=${encodeURIComponent(fingerprint)}`),
    enabled: open,
  });
  const [d, setD] = useState<TicketDraft | null>(null);
  const [created, setCreated] = useState<TicketCreated | null>(null);
  useEffect(() => {
    if (open && draft.data) setD(draft.data);
    if (!open) {
      setD(null);
      setCreated(null);
    }
  }, [open, draft.data]);
  const create = useMutation({
    mutationFn: (x: TicketDraft) =>
      api<TicketCreated>(`/api/runs/${runId}/tickets`, {
        method: "POST",
        json: {
          fingerprint: x.fingerprint, integration_id: x.integration_id, project_key: x.project_key, issue_type: x.issue_type,
          summary: x.summary, description: x.description, labels: x.labels, autoplan: x.autoplan, repository_id: x.repository_id,
        }, // prettier-ignore
      }),
    onSuccess: (r) => {
      setCreated(r);
      qc.invalidateQueries({ queryKey: ["work"] });
      qc.invalidateQueries({ queryKey: ["runs", runId, "ticket-draft"] });
      toast({ title: `${r.jira_key} created`, tone: "success" });
    },
  });
  const set = (patch: Partial<TicketDraft>) => setD((x) => (x ? { ...x, ...patch } : x));
  const existing = d?.existing;
  const ready = repos.data?.filter((p) => p.status === "ready") ?? [];
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Create a Jira ticket"
      description="Review the issue before it's created. It goes on your Board, and can be planned right away."
      actions={
        created || existing ? (
          <Button onClick={() => onOpenChange(false)}>Close</Button>
        ) : (
          <>
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              pending={create.isPending}
              disabled={!d || !d.integration_id || !d.project_key || !d.summary.trim() || (d.autoplan && !d.repository_id)}
              onClick={() => d && create.mutate(d)}
            >
              Create Jira issue
            </Button>
          </>
        )
      }
    >
      {draft.error ? (
        <ErrorNote error={draft.error} />
      ) : !d ? (
        <SkeletonRows rows={4} />
      ) : created || existing ? (
        <div className="stack-tight">
          <p>
            {created ? "Created" : "This finding already has"} <strong>{created?.jira_key ?? existing!.jira_key}</strong>
            {created?.plan_started ? "; the planner is drafting a plan." : "."}
          </p>
          {(created?.work_item_id ?? existing?.work_item_id) && (
            <Link to={`/work/${created?.work_item_id ?? existing?.work_item_id}`}>Open it on the Board</Link>
          )}
        </div>
      ) : (
        <div className="stack">
          {d.integrations.length > 1 && (
            <Select
              label="Jira"
              options={d.integrations.map((i) => [String(i.id), i.name] as [string, string])}
              value={String(d.integration_id ?? "")}
              onChange={(e) => set({ integration_id: Number(e.target.value) })}
            />
          )}
          {!d.integrations.length && <p className="warn-text">Add your Jira token in Settings → Integrations first.</p>}
          <Select
            label="Project"
            options={[["", "Choose a project"], ...d.projects.map((p) => [p.key, `${p.key} · ${p.name}`] as [string, string])]}
            value={d.project_key ?? ""}
            hint={d.projects_error ? `Couldn't list projects: ${d.projects_error}` : undefined}
            onChange={(e) => set({ project_key: e.target.value || null })}
          />
          <TextField label="Issue type" value={d.issue_type} onChange={(e) => set({ issue_type: e.target.value })} />
          <TextField label="Summary" value={d.summary} maxLength={255} onChange={(e) => set({ summary: e.target.value })} />
          <TextArea label="Description" rows={8} value={d.description} maxLength={20000} onChange={(e) => set({ description: e.target.value })} />
          <Checkbox label="Draft a plan for it right away" checked={d.autoplan} onChange={(v) => set({ autoplan: v })} />
          <Select
            label="Repository to plan against"
            options={[["", "None"], ...ready.map((p) => [String(p.id), p.name] as [string, string])]}
            value={d.repository_id ? String(d.repository_id) : ""}
            onChange={(e) => set({ repository_id: e.target.value ? Number(e.target.value) : null })}
          />
          <ErrorNote error={create.error} />
        </div>
      )}
    </Dialog>
  );
}
