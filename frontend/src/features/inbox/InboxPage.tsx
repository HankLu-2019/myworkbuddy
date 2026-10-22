import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCheck, Info, MoreHorizontal, OctagonAlert } from "lucide-react";
import { api, type InboxItem, type InboxPage as Page, type SchedulePage } from "../../api";
import { usePageTitle } from "../../app/pageTitle";
import {
  Button,
  Dot,
  EmptyState,
  ErrorNote,
  LinkTabs,
  Menu,
  PageHeader,
  RelativeTime,
  SkeletonRows,
  buttonClass,
  displayTimezone,
  useToast,
} from "../../components/ui";

const FILTERS = { all: "All", unread: "Unread", alerts: "Alerts" } as const;
type Filter = keyof typeof FILTERS;
export const UNDO_MS = 5_000;

const SOURCE: Record<InboxItem["kind"], string> = {
  run: "Schedule",
  alert: "Schedule",
  recovered: "Schedule",
  system: "System",
  credential: "Integrations",
  proposal: "Chat",
  plan: "Work",
  job: "Work",
};

function SeverityIcon({ severity }: { severity: InboxItem["severity"] }) {
  if (severity === "alert") return <OctagonAlert size={18} aria-label="Alert" className="sev sev-alert" />;
  if (severity === "warning") return <AlertTriangle size={18} aria-label="Warning" className="sev sev-warning" />;
  return <Info size={18} aria-label="Info" className="sev sev-info" />;
}

/** "Today", "Yesterday" or the date, in the user's timezone (07 §6.10). */
export function dayLabel(iso: string, now: Date = new Date()): string {
  const zone = displayTimezone();
  const fmt = new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" });
  const day = fmt.format(new Date(iso));
  if (day === fmt.format(now)) return "Today";
  if (day === fmt.format(new Date(now.getTime() - 86_400_000))) return "Yesterday";
  return new Intl.DateTimeFormat(undefined, { timeZone: zone, weekday: "short", day: "numeric", month: "short" }).format(
    new Date(iso),
  );
}

export function groupByDay(items: InboxItem[], now?: Date): [string, InboxItem[]][] {
  const groups: [string, InboxItem[]][] = [];
  for (const item of items) {
    const label = dayLabel(item.created_at, now);
    const last = groups[groups.length - 1];
    if (last && last[0] === label) last[1].push(item);
    else groups.push([label, [item]]);
  }
  return groups;
}

/** Inbox (07 §4.2): grouped by day; opening an item marks it read; Mark all read and Dismiss can be undone. */
export default function InboxPage() {
  usePageTitle("Inbox");
  const [params] = useSearchParams();
  const filter = (params.get("filter") as Filter) in FILTERS ? (params.get("filter") as Filter) : "all";
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const [hidden, setHidden] = useState<ReadonlySet<number>>(new Set());

  const query = new URLSearchParams();
  if (filter === "unread") query.set("unread", "true");
  if (filter === "alerts") query.set("kind", "alert");
  const items = useInfiniteQuery({
    queryKey: ["inbox", filter],
    queryFn: ({ pageParam }) =>
      api<Page>(`/api/inbox?${query}${pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ""}`),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: 30_000,
  });
  // The list just loaded fresh data: bring the sidebar's unread count in line with it.
  useEffect(() => {
    if (items.dataUpdatedAt) qc.invalidateQueries({ queryKey: ["inbox-count"] });
  }, [items.dataUpdatedAt, qc]);
  const schedules = useQuery({
    queryKey: ["schedules", "any"],
    queryFn: () => api<SchedulePage>("/api/schedules?limit=1"),
    staleTime: 60_000,
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["inbox"] });
    qc.invalidateQueries({ queryKey: ["inbox-count"] });
    qc.invalidateQueries({ queryKey: ["home"] });
  };
  const read = useMutation({ mutationFn: (id: number) => api(`/api/inbox/${id}/read`, { method: "POST" }), onSuccess: refresh });
  const unread = useMutation({
    mutationFn: (ids: number[]) => api("/api/inbox/unread", { method: "POST", json: { ids } }),
    onSuccess: refresh,
  });
  const readAll = useMutation({
    mutationFn: () => api<{ marked: number; ids: number[] }>("/api/inbox/read-all", { method: "POST" }),
    onSuccess: (r) => {
      refresh();
      if (r.marked)
        toast({
          title: `Marked ${r.marked} ${r.marked === 1 ? "item" : "items"} read`,
          action: { label: "Undo", onClick: () => unread.mutate(r.ids) },
        });
    },
  });

  function dismiss(item: InboxItem) {
    setHidden((h) => new Set(h).add(item.id));
    let undone = false;
    toast({
      title: "Dismissed",
      action: {
        label: "Undo",
        onClick: () => {
          undone = true;
          setHidden((h) => {
            const next = new Set(h);
            next.delete(item.id);
            return next;
          });
        },
      },
    });
    setTimeout(() => {
      if (!undone) api(`/api/inbox/${item.id}`, { method: "DELETE" }).then(refresh, refresh);
    }, UNDO_MS);
  }

  function open(item: InboxItem) {
    if (!item.read_at) read.mutate(item.id);
    if (item.link) navigate(item.link);
  }

  const rows = (items.data?.pages.flatMap((p) => p.items) ?? []).filter((i) => !hidden.has(i.id));
  const hasSchedules = (schedules.data?.items.length ?? 0) > 0;

  return (
    <div className="page">
      <PageHeader title="Inbox">
        <Button icon={<CheckCheck size={16} aria-hidden />} onClick={() => readAll.mutate()} pending={readAll.isPending}>
          Mark all read
        </Button>
      </PageHeader>
      <LinkTabs
        label="Show"
        tabs={Object.entries(FILTERS).map(([key, label]) => ({
          to: key === "all" ? "/inbox" : `/inbox?filter=${key}`,
          label,
          active: key === filter,
        }))}
      />
      <ErrorNote error={items.error ?? readAll.error} />
      {items.isLoading && <SkeletonRows rows={4} />}
      {items.data && !rows.length && (
        <EmptyState
          action={
            !hasSchedules && (
              <Link to="/schedules/new" className={buttonClass("primary", "sm")}>
                <span className="btn-label">Create a schedule</span>
              </Link>
            )
          }
        >
          {filter === "all" ? "You're all caught up." : filter === "unread" ? "Nothing unread." : "No alerts."}
          {!hasSchedules && filter === "all" && <p className="muted">Create a schedule to get checks delivered here.</p>}
        </EmptyState>
      )}
      {groupByDay(rows).map(([day, list]) => (
        <section key={day} aria-label={day} className="stack">
          <h2 className="list-label">{day}</h2>
          <ul className="plain-list inbox-list">
            {list.map((item) => (
              <li key={item.id} className={`inbox-row${item.read_at ? "" : " unread"}`}>
                <SeverityIcon severity={item.severity} />
                <button type="button" className="inbox-open grow" onClick={() => open(item)}>
                  <span className="inbox-title">{item.title}</span>
                  <span className="muted small">
                    {SOURCE[item.kind]} · <RelativeTime iso={item.created_at} />
                  </span>
                </button>
                {!item.read_at && <Dot label="Unread" />}
                <Menu
                  label={`Actions for ${item.title}`}
                  trigger={
                    <button type="button" className="btn btn-ghost btn-sm" aria-label={`Actions for ${item.title}`}>
                      <MoreHorizontal size={16} aria-hidden />
                    </button>
                  }
                  items={[
                    item.read_at
                      ? { label: "Mark unread", onSelect: () => unread.mutate([item.id]) }
                      : { label: "Mark read", onSelect: () => read.mutate(item.id) },
                    { label: "Dismiss", onSelect: () => dismiss(item) },
                  ]}
                />
              </li>
            ))}
          </ul>
        </section>
      ))}
      {items.hasNextPage && (
        <Button onClick={() => items.fetchNextPage()} pending={items.isFetchingNextPage}>
          Load more
        </Button>
      )}
    </div>
  );
}
