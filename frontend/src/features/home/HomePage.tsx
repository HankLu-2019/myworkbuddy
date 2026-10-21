import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, Circle, FolderGit2, MessageSquarePlus, Search } from "lucide-react";
import { api, type Checklist, type Home } from "../../api";
import { useAuth } from "../../auth";
import { usePageTitle } from "../../app/pageTitle";
import {
  Button,
  Card,
  EmptyState,
  ErrorNote,
  PageHeader,
  RelativeTime,
  SkeletonRows,
  StatusPill,
  VisibilityBadge,
  buttonClass,
  formatElapsed,
} from "../../components/ui";

export const FROM_START = "from=getting-started";

const CHECKLIST_TITLE: Record<Checklist["kind"], string> = {
  getting_started: "Getting started",
  admin_setup: "Admin setup",
};

function dismissedKey(userId: number | undefined, kind: string) {
  return `mwb:checklist-dismissed:${userId ?? "anon"}:${kind}`;
}

function isDismissed(userId: number | undefined, kind: string): boolean {
  try {
    return localStorage.getItem(dismissedKey(userId, kind)) === "1";
  } catch {
    return false;
  }
}

/** Ticks itself as the user works; hides when every required step is done or when dismissed (07 §5.1). */
function ChecklistCard({ checklist }: { checklist: Checklist }) {
  const { user } = useAuth();
  const [dismissed, setDismissed] = useState(() => isDismissed(user?.id, checklist.kind));
  const required = checklist.steps.filter((s) => !s.optional);
  if (dismissed || required.every((s) => s.done)) return null;
  const done = checklist.steps.filter((s) => s.done).length;
  const id = `checklist-${checklist.kind}`;
  return (
    <Card as="section" aria-labelledby={id} className="stack checklist">
      <div className="row">
        <h2 id={id} className="grow">
          {CHECKLIST_TITLE[checklist.kind]}
        </h2>
        <span className="muted small">
          {done} of {checklist.steps.length} done
        </span>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            try {
              localStorage.setItem(dismissedKey(user?.id, checklist.kind), "1");
            } catch {
              // hidden for this visit only
            }
            setDismissed(true);
          }}
        >
          Dismiss
        </Button>
      </div>
      <ol className="plain-list stack">
        {checklist.steps.map((s) => (
          <li key={s.key} className="row" data-done={s.done || undefined}>
            {s.done ? (
              <CheckCircle2 size={18} className="ok-icon" aria-label="Done" />
            ) : (
              <Circle size={18} className="muted" aria-label="To do" />
            )}
            {s.done ? (
              <span className="muted">{s.label}</span>
            ) : (
              <Link to={`${s.href}${s.href.includes("?") ? "&" : "?"}${FROM_START}`}>{s.label}</Link>
            )}
            {s.optional && <span className="muted small">(optional)</span>}
          </li>
        ))}
      </ol>
    </Card>
  );
}

/** Home (07 §4.1): what needs me, what's running, what changed. */
export default function HomePage() {
  usePageTitle("Home");
  const home = useQuery({
    queryKey: ["home"],
    queryFn: () => api<Home>("/api/home"),
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });
  const h = home.data;
  return (
    <div className="page">
      <PageHeader title="Home">
        <Link to="/chat?new=1" className={buttonClass("primary")}>
          <MessageSquarePlus size={16} aria-hidden />
          <span className="btn-label">New chat</span>
        </Link>
        <Link to="/search" className={buttonClass("secondary")}>
          <Search size={16} aria-hidden />
          <span className="btn-label">Search</span>
        </Link>
        <Link to="/sources/repos?add=1" className={buttonClass("secondary")}>
          <FolderGit2 size={16} aria-hidden />
          <span className="btn-label">Add repository</span>
        </Link>
      </PageHeader>
      <ErrorNote error={home.error} />
      {home.isLoading && <SkeletonRows rows={5} />}
      {h && (
        <>
          {h.checklists.map((c) => (
            <ChecklistCard key={c.kind} checklist={c} />
          ))}

          <section aria-labelledby="needs-you" className="stack">
            <h2 id="needs-you">Needs you</h2>
            {h.needs_you.length ? (
              <ul className="plain-list stack">
                {h.needs_you.map((n) => (
                  <li key={n.key}>
                    <Card className={`row needs needs-${n.kind}`}>
                      <span className="grow">{n.message}</span>
                      {n.at && (
                        <span className="muted small">
                          <RelativeTime iso={n.at} />
                        </span>
                      )}
                      <Link to={n.href} className={buttonClass("primary", "sm")}>
                        <span className="btn-label">{n.action}</span>
                      </Link>
                    </Card>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState>Nothing needs you right now.</EmptyState>
            )}
          </section>

          {h.today && (
            <section aria-labelledby="today" className="stack">
              <h2 id="today">Today</h2>
              <Card className="row">
                <StatusPill status={h.today.status} />
                <span className="grow">
                  <strong>{h.today.schedule_name}:</strong> {h.today.headline}
                </span>
                <span className="muted small">
                  <RelativeTime iso={h.today.at} />
                </span>
                <Link to={`/runs/${h.today.run_id}`} className={buttonClass("secondary", "sm")}>
                  <span className="btn-label">Open</span>
                </Link>
              </Card>
            </section>
          )}

          {h.running.length > 0 && (
            <section aria-labelledby="running-now" className="stack">
              <h2 id="running-now">Running now</h2>
              <ul className="plain-list stack">
                {h.running.map((r) => (
                  <li key={`${r.kind}-${r.id}`} className="row">
                    <Link to={r.href}>{r.title}</Link>
                    <span className="muted small grow">{r.step}</span>
                    <span className="muted small">{formatElapsed(r.since)}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section aria-labelledby="recent" className="stack">
            <h2 id="recent">Recent</h2>
            {h.librarian && (
              <p>
                The librarian changed {h.librarian.pages_changed} {h.librarian.pages_changed === 1 ? "page" : "pages"}
                {h.librarian.rejected > 0 && ` and held back ${h.librarian.rejected}`} <RelativeTime iso={h.librarian.at} /> —{" "}
                <Link to="/wiki/changes">Review</Link>
              </p>
            )}
            <div className="card-grid">
              <Card as="section" aria-labelledby="recent-chats" className="stack">
                <h3 id="recent-chats">Chats</h3>
                {h.recent_chats.length ? (
                  <ul className="plain-list stack">
                    {h.recent_chats.map((c) => (
                      <li key={c.id} className="row">
                        <Link to={`/chat/${c.id}`} className="grow truncate">
                          {c.title}
                        </Link>
                        <span className="muted small">
                          <RelativeTime iso={c.updated_at} />
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <EmptyState
                    action={
                      <Link to="/chat?new=1" className={buttonClass("primary", "sm")}>
                        <span className="btn-label">Ask your first question</span>
                      </Link>
                    }
                  >
                    No chats yet.
                  </EmptyState>
                )}
              </Card>
              <Card as="section" aria-labelledby="recent-pages" className="stack">
                <h3 id="recent-pages">Wiki pages</h3>
                {h.recent_pages.length ? (
                  <ul className="plain-list stack">
                    {h.recent_pages.map((p) => (
                      <li key={p.id} className="row">
                        <Link to={`/wiki/${p.id}`} className="grow truncate">
                          {p.title}
                        </Link>
                        <VisibilityBadge visibility={p.visibility} />
                        <span className="muted small">
                          <RelativeTime iso={p.updated_at} />
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <EmptyState>No wiki pages yet. The librarian writes your first ones after a few chats.</EmptyState>
                )}
              </Card>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
