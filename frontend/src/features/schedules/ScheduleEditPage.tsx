import { useEffect } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Schedule, type ScheduleIn } from "../../api";
import { usePageTitle } from "../../app/pageTitle";
import { ErrorNote, PageHeader, SkeletonRows, useToast } from "../../components/ui";
import { ScheduleForm } from "./ScheduleForm";

export function scheduleToInput(s: Schedule): Partial<ScheduleIn> {
  return {
    name: s.name, agent: s.agent.slug, inputs: s.inputs, cron: s.cron, timezone: s.timezone, prompt: s.prompt,
    notify_mode: s.notify_mode, notify_email: s.notify_email, max_runtime_s: s.max_runtime_s,
  };
}

/** `/schedules/new` (optionally `?agent=k8s-health`) and `/schedules/:id/edit` (07 §4.7). */
export default function ScheduleEditPage() {
  const { scheduleId } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const existing = useQuery({
    queryKey: ["schedules", scheduleId],
    queryFn: () => api<Schedule>(`/api/schedules/${scheduleId}`),
    enabled: !!scheduleId,
  });
  usePageTitle(
    { label: "Schedules", to: "/schedules" },
    scheduleId ? { label: existing.data?.name ?? "Schedule", to: `/schedules/${scheduleId}` } : undefined,
    scheduleId ? "Edit" : "New schedule",
  );
  const save = useMutation({
    mutationFn: (body: ScheduleIn) =>
      scheduleId
        ? api<Schedule>(`/api/schedules/${scheduleId}`, { method: "PATCH", json: { ...body, enabled: undefined } })
        : api<Schedule>("/api/schedules", { method: "POST", json: body }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["schedules"] });
      toast({ title: scheduleId ? "Schedule saved" : "Schedule created", tone: "success" });
    },
  });
  // Navigate after the form has seen `saved`, so its unsaved-changes guard lets us go.
  useEffect(() => {
    if (save.isSuccess) navigate(`/schedules/${save.data.id}`);
  }, [save.isSuccess]);

  if (scheduleId && existing.error)
    return (
      <div className="page">
        <ErrorNote error={existing.error} />
      </div>
    );
  if (scheduleId && !existing.data)
    return (
      <div className="page">
        <SkeletonRows rows={6} />
      </div>
    );
  const initial = existing.data ? scheduleToInput(existing.data) : { agent: params.get("agent") ?? undefined };
  return (
    <div className="page narrow">
      <PageHeader title={scheduleId ? `Edit ${existing.data?.name}` : "New schedule"} />
      <ScheduleForm
        initial={initial}
        draftId={scheduleId ?? "new"}
        submitLabel={scheduleId ? "Save" : "Create schedule"}
        onSubmit={(body) => save.mutate(body)}
        pending={save.isPending}
        saved={save.isSuccess}
        error={save.error}
      />
    </div>
  );
}
