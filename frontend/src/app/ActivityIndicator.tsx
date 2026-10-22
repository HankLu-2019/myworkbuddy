import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { api, type Activity, type ProjectDetail, type Plan, type RunDetail } from "../api";
import { Popover, formatElapsed, useToast } from "../components/ui";

export const POLL_ACTIVE_MS = 5_000;
export const POLL_IDLE_MS = 30_000;

/**
 * Top-bar spinner and count of the user's background work (07 §6.3). Polls every 5 s while something runs or
 * the list is open, else every 30 s; TanStack pauses polling while the tab is hidden and refetches on focus.
 * When an item finishes while the user is in the app, a toast says how it went and links to it.
 */
export function ActivityIndicator() {
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const activity = useQuery({
    queryKey: ["activity"],
    queryFn: () => api<Activity>("/api/activity"),
    refetchInterval: (q) => (open || (q.state.data?.items.length ?? 0) > 0 ? POLL_ACTIVE_MS : POLL_IDLE_MS),
    refetchOnWindowFocus: true,
  });
  const items = activity.data?.items ?? [];

  const previous = useRef<Map<string, { kind: string; id: number | string; title: string }> | null>(null);
  useEffect(() => {
    if (!activity.data) return;
    const now = new Map(activity.data.items.map((i) => [`${i.kind}:${i.id}`, { kind: i.kind, id: i.id, title: i.title }]));
    const before = previous.current;
    previous.current = now;
    if (!before) return; // first load: nothing finished "while you were here"
    const finished = [...before.entries()].filter(([k]) => !now.has(k));
    if (!finished.length) return;
    qc.invalidateQueries({ queryKey: ["projects"] });
    qc.invalidateQueries({ queryKey: ["home"] });
    for (const [, item] of finished) {
      if (item.kind === "run") {
        qc.invalidateQueries({ queryKey: ["runs"] });
        qc.invalidateQueries({ queryKey: ["schedules"] });
        api<RunDetail>(`/api/runs/${item.id}`)
          .then((r) => {
            const open = { label: "Open", onClick: () => navigate(`/runs/${r.id}`) };
            if (r.status === "succeeded") toast({ title: `${r.schedule_name}: ${r.headline ?? "done"}`, tone: "success", action: open });
            else if (r.status !== "skipped") toast({ title: `${r.schedule_name} didn't finish`, tone: "error", action: open });
          })
          .catch(() => undefined);
        continue;
      }
      if (item.kind === "plan") {
        qc.invalidateQueries({ queryKey: ["work"] });
        qc.invalidateQueries({ queryKey: ["plans", item.id] });
        api<Plan>(`/api/plans/${item.id}`)
          .then((p) => {
            const open = { label: "Open", onClick: () => navigate(`/work/${p.work_item_id}/plan`) };
            const name = item.title.replace(/^Plan for /, "");
            if (p.status === "needs_decision") toast({ title: `Plan ready for ${name}`, tone: "success", action: open });
            else if (p.status === "failed") toast({ title: `Planning ${name} failed`, tone: "error", action: open });
          })
          .catch(() => undefined);
        continue;
      }
      api<ProjectDetail>(`/api/projects/${item.id}`)
        .then((p) => {
          const open = { label: "Open", onClick: () => navigate(`/sources/repos/${p.id}`) };
          if (p.status === "ready") toast({ title: `${p.name} is ready`, tone: "success", action: open });
          else if (p.status === "failed") toast({ title: `Indexing ${p.name} failed`, tone: "error", action: open });
        })
        .catch(() => undefined); // removed meanwhile: nothing to say
    }
  }, [activity.data, qc, toast, navigate]);

  if (!items.length && !open) return null;
  const label = `${items.length} background ${items.length === 1 ? "task" : "tasks"} running`;
  return (
    <Popover
      label="Background work"
      open={open}
      onOpenChange={setOpen}
      trigger={
        <button type="button" className="btn btn-ghost btn-sm activity-trigger" aria-label={label}>
          <Loader2 size={16} className="spin" aria-hidden />
          <span className="btn-label">{items.length}</span>
        </button>
      }
    >
      <h2 className="popover-title">Background work</h2>
      {items.length ? (
        <ul className="plain-list stack activity-list">
          {items.map((i) => (
            <li key={`${i.kind}-${i.id}`}>
              <Link to={i.href} onClick={() => setOpen(false)}>
                {i.title}
              </Link>
              <div className="muted small">
                {i.step ?? "Working"} · {formatElapsed(i.since)}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted small">Nothing running.</p>
      )}
    </Popover>
  );
}
