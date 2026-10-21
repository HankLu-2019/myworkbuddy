import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, MessageSquare, Pencil, RotateCcw } from "lucide-react";
import { api, type RunDetail } from "../../api";
import { usePageTitle } from "../../app/pageTitle";
import { Button, ErrorNote, PageHeader, SkeletonRows, buttonClass, useToast } from "../../components/ui";
import { ACTIVE, RunResult } from "./RunResult";

/** Run result (07 §4.8): header, findings, and the ways onward (chat, transcript, run again, edit). */
export default function RunPage() {
  const { runId } = useParams();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const run = useQuery({
    queryKey: ["runs", runId],
    queryFn: () => api<RunDetail>(`/api/runs/${runId}`),
    refetchInterval: (q) => (ACTIVE.includes(q.state.data?.status ?? "") ? 3000 : false),
  });
  const r = run.data;
  usePageTitle(
    { label: "Schedules", to: "/schedules" },
    r?.schedule_id ? { label: r.schedule_name, to: `/schedules/${r.schedule_id}` } : r?.schedule_name,
    "Run",
  );
  const again = useMutation({
    mutationFn: () => api<{ run_id: string }>(`/api/schedules/${r!.schedule_id}/run`, { method: "POST" }),
    onSuccess: ({ run_id }) => {
      qc.invalidateQueries({ queryKey: ["activity"] });
      toast({ title: "Run started; you can leave this page", tone: "success" });
      navigate(`/runs/${run_id}`);
    },
  });

  if (run.error)
    return (
      <div className="page">
        <ErrorNote error={run.error} />
      </div>
    );
  if (!r)
    return (
      <div className="page">
        <SkeletonRows rows={5} />
      </div>
    );
  return (
    <div className="page">
      <PageHeader title={r.schedule_name || "Run"}>
        <Link to={`/chat?context=run:${r.id}`} className={buttonClass("primary")}>
          <MessageSquare size={16} aria-hidden />
          <span className="btn-label">Investigate in chat</span>
        </Link>
        {r.session_id && (
          <Link to={`/chat/${r.session_id}`} className={buttonClass("secondary")}>
            <FileText size={16} aria-hidden />
            <span className="btn-label">Open transcript</span>
          </Link>
        )}
        {r.schedule_id && (
          <>
            <Button icon={<RotateCcw size={16} aria-hidden />} onClick={() => again.mutate()} pending={again.isPending} disabled={ACTIVE.includes(r.status)}>
              Run again
            </Button>
            <Link to={`/schedules/${r.schedule_id}/edit`} className={buttonClass("secondary")}>
              <Pencil size={16} aria-hidden />
              <span className="btn-label">Edit schedule</span>
            </Link>
          </>
        )}
      </PageHeader>
      <ErrorNote error={again.error} />
      {r.schedule_id === null && !r.schedule_name && <p className="muted">This was a test run of an unsaved schedule.</p>}
      <RunResult run={r} />
    </div>
  );
}
