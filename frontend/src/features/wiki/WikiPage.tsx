import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { History, Pencil, Plus, Trash2, Undo2 } from "lucide-react";
import { useAuth } from "../../auth";
import { draftKey, readDraft, writeDraft } from "../../app/drafts";
import { UnsavedGuard } from "../../app/UnsavedGuard";
import { markLibrarianSeen } from "../../app/seen";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Commit, type LibrarianChange, type LibrarianRun, type Page, type PageSummary, type Visibility } from "../../api";
import { usePageTitle } from "../../app/pageTitle";
import {
  Banner,
  Button,
  Card,
  ConfirmDialog,
  DiffView,
  EmptyState,
  ErrorNote,
  Markdown,
  PageHeader,
  RelativeTime,
  SkeletonRows,
  TextArea,
  TextField,
  VisibilityBadge,
  VisibilityControl,
  formatDate,
  useToast,
} from "../../components/ui";


const KEEP_OPEN = "<!-- mwb:keep -->";
const KEEP_CLOSE = "<!-- /mwb:keep -->";

/** Wrap the selected text (or the current line) in keep markers, so the librarian must leave it as is. */
export function wrapInKeep(text: string, start: number, end: number): { text: string; cursor: number } {
  if (start === end) {
    start = text.lastIndexOf("\n", start - 1) + 1;
    const eol = text.indexOf("\n", end);
    end = eol === -1 ? text.length : eol;
  }
  const inner = text.slice(start, end).replace(/^\n+|\n+$/g, "");
  const block = `${KEEP_OPEN}\n${inner}\n${KEEP_CLOSE}`;
  return { text: text.slice(0, start) + block + text.slice(end), cursor: start + block.length };
}

function PageTree({ pages, current }: { pages: PageSummary[]; current?: number }) {
  const groups: [string, PageSummary[]][] = [
    ["My pages", pages.filter((p) => p.visibility === "private")],
    ["Team wiki", pages.filter((p) => p.visibility === "public")],
  ];
  return (
    <>
      {groups.map(([label, items]) => (
        <div key={label}>
          <div className="list-label" id={`tree-${label}`}>{label}</div>
          {!items.length && <div className="muted small">None yet</div>}
          <ul aria-labelledby={`tree-${label}`}>
            {items.map((p) => (
              <li key={p.id}>
                <Link
                  to={`/wiki/${p.id}`}
                  className={`list-item ${p.id === current ? "active" : ""}`}
                  aria-current={p.id === current ? "page" : undefined}
                >
                  <span className="mono small truncate">{p.path}</span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </>
  );
}

function LibrarianPanel() {
  const qc = useQueryClient();
  const toast = useToast();
  const { user } = useAuth();
  const [reverting, setReverting] = useState<LibrarianChange | null>(null);
  useEffect(() => markLibrarianSeen(user?.id), [user?.id]);
  const revert = useMutation({
    mutationFn: (sha: string) =>
      api<{ reverted: string[] }>(`/api/wiki/librarian/changes/${sha}/revert`, { method: "POST" }),
    onSuccess: (r) => {
      setReverting(null);
      qc.invalidateQueries({ queryKey: ["librarian"] });
      qc.invalidateQueries({ queryKey: ["wiki"] });
      qc.invalidateQueries({ queryKey: ["wiki-pages"] });
      toast({ title: "Change reverted", description: r.reverted.join(", "), tone: "success" });
    },
  });
  const changes = useQuery({
    queryKey: ["librarian"],
    queryFn: () => api<LibrarianChange[]>("/api/wiki/librarian/changes?days=7"),
  });
  const runs = useQuery({
    queryKey: ["librarian-runs"],
    queryFn: () => api<LibrarianRun[]>("/api/wiki/librarian/runs?days=7"),
  });
  const run = useMutation({ mutationFn: () => api<{ queued: boolean }>("/api/wiki/librarian/run", { method: "POST" }) });
  const heldBack = (runs.data ?? []).flatMap((r) => r.pages_rejected.map((x) => ({ ...x, run: r })));
  const failed = (runs.data ?? []).filter((r) => r.status === "failed");
  usePageTitle({ label: "Wiki", to: "/wiki" }, "Librarian changes");
  return (
    <div className="page">
      <PageHeader title="What the librarian changed">
        <Button onClick={() => run.mutate()} pending={run.isPending}>
          {run.isSuccess ? "Queued" : "Update from recent sessions now"}
        </Button>
      </PageHeader>
      <p className="muted lede">
        Each night the librarian folds your session summaries into your private pages (how you work, decisions,
        project cards). Every change is a git commit, so you can review it here and edit anything it got wrong.
      </p>
      <ErrorNote error={changes.error || runs.error || run.error} />
      {failed.length > 0 && (
        <Banner tone="warning">
          The last {failed.length === 1 ? "run" : `${failed.length} runs`} failed ({failed[0].error ?? "unknown error"}).
          Your pages were not changed.
        </Banner>
      )}
      {heldBack.length > 0 && (
        <section aria-labelledby="held-back">
          <h2 id="held-back">Not applied</h2>
          <p className="muted small">
            The librarian wanted to make these changes but they looked unsafe, so your pages were left as they are.
          </p>
          {heldBack.map((h, i) => (
            <details key={`${h.run.id}-${i}`} className="card change">
              <summary>
                <span className="mono">{h.path}</span> <span>Not applied: {h.reason}</span>{" "}
                <span className="muted small">{formatDate(h.run.started_at)}</span>
              </summary>
              <DiffView text={h.diff} />
            </details>
          ))}
        </section>
      )}
      {changes.isLoading && <SkeletonRows />}
      {changes.data && !changes.data.length && <EmptyState>No librarian changes in the last 7 days.</EmptyState>}
      {changes.data?.map((c) => (
        <details key={c.sha} className="card change">
          <summary>
            <span>{c.message}</span> <RelativeTime iso={c.date} />
          </summary>
          <DiffView text={c.diff} />
          <div className="actions">
            <Button size="sm" icon={<Undo2 size={14} aria-hidden />} onClick={() => setReverting(c)}>
              Revert this change
            </Button>
          </div>
        </details>
      ))}
      <ConfirmDialog
        open={!!reverting}
        onOpenChange={(open) => {
          if (!open) {
            setReverting(null);
            revert.reset();
          }
        }}
        title="Revert this change?"
        body={
          <div className="stack">
            <p>
              “{reverting?.message}” is undone with a new commit, so it stays in the page history. Pages edited
              since can't be reverted this way; change them by hand.
            </p>
            <ErrorNote error={revert.error} />
          </div>
        }
        confirmLabel="Revert change"
        pending={revert.isPending}
        onConfirm={() => reverting && revert.mutate(reverting.sha)}
      />
    </div>
  );
}

function NewPage({ onCreated }: { onCreated: (p: Page) => void }) {
  const [path, setPath] = useState("notes/");
  const [content, setContent] = useState("# Title\n\n");
  const create = useMutation({
    mutationFn: () => api<Page>("/api/wiki/pages", { method: "POST", json: { path, content } }),
    onSuccess: onCreated,
  });
  usePageTitle({ label: "Wiki", to: "/wiki" }, "New page");
  return (
    <form
      className="page"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate();
      }}
    >
      <PageHeader title="New page" />
      <p className="muted lede">New pages are private. Share a page with the team when you want them to see it.</p>
      <TextField
        label="Path"
        inputClassName="mono"
        value={path}
        onChange={(e) => setPath(e.target.value)}
        placeholder="runbooks/jenkins-oom.md"
        hint="Folders are created as needed; .md is added if missing."
      />
      <TextArea label="Content (Markdown)" inputClassName="editor" value={content} onChange={(e) => setContent(e.target.value)} />
      <ErrorNote error={create.error} />
      <div className="actions">
        <Button variant="primary" type="submit" pending={create.isPending}>
          Create page
        </Button>
      </div>
    </form>
  );
}

function PageView({ id }: { id: number }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [mode, setMode] = useState<"view" | "edit" | "history">("view");
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const key = draftKey(user?.id, "wiki", id);
  const [draft, setDraftState] = useState("");
  // Every keystroke goes to localStorage (07 §6.6), with the version the editor was opened on, so a reload or
  // an expired session restores the edit and its conflict check.
  const setDraft = (text: string) => {
    setDraftState(text);
    writeDraft(key, text);
  };
  const editor = useRef<HTMLTextAreaElement>(null);
  const protect = () => {
    const el = editor.current;
    if (!el) return;
    const { text, cursor } = wrapInKeep(draft, el.selectionStart, el.selectionEnd);
    setDraft(text);
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(cursor, cursor);
    });
  };
  const [message, setMessage] = useState("");
  const [diffSha, setDiffSha] = useState<string | null>(null);
  const [baseSha, setBaseSha] = useState("");

  const page = useQuery({ queryKey: ["wiki", id], queryFn: () => api<Page>(`/api/wiki/pages/${id}`) });
  const history = useQuery({
    queryKey: ["wiki", id, "history"],
    queryFn: () => api<Commit[]>(`/api/wiki/pages/${id}/history`),
    enabled: mode === "history",
  });
  const diff = useQuery({
    queryKey: ["wiki", id, "diff", diffSha],
    queryFn: () => api<{ diff: string }>(`/api/wiki/pages/${id}/commits/${diffSha}`),
    enabled: !!diffSha,
  });

  function startEditing(p: Page) {
    const saved = readDraft(key);
    const savedBase = readDraft(`${key}:base`);
    setDraftState(saved ?? p.content);
    setBaseSha(saved !== null && savedBase !== null ? savedBase : (p.head_sha ?? ""));
    if (saved === null) writeDraft(`${key}:base`, p.head_sha ?? "");
    setNotice(null);
    setMode("edit");
    if (saved !== null && saved !== p.content) toast({ title: "Restored your unsaved text" });
  }
  function stopEditing() {
    writeDraft(key, "");
    writeDraft(`${key}:base`, "");
    setMode("view");
  }

  useEffect(() => {
    setMode("view");
    setDiffSha(null);
  }, [id]);

  // Back from an expired session (or a crash) with unsaved text: reopen the editor with it.
  const resumed = useRef<number | null>(null);
  useEffect(() => {
    if (!page.data || resumed.current === id || !page.data.can_edit) return;
    resumed.current = id;
    if (readDraft(key) !== null) startEditing(page.data);
    // only once per page, when it first loads
  }, [page.data, id]);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["wiki"] });
    qc.invalidateQueries({ queryKey: ["wiki-pages"] });
  };

  const save = useMutation({
    mutationFn: () =>
      api<Page>(`/api/wiki/pages/${id}`, {
        method: "PUT",
        // The version the editor was opened on; the server refuses the save if the page changed since (409).
        json: { content: draft, message: message || undefined, base_sha: baseSha },
      }),
    onSuccess: () => {
      stopEditing();
      setMessage("");
      refresh();
      toast({ title: "Saved", tone: "success" });
    },
  });
  const conflict = save.error instanceof ApiError && save.error.code === "wiki_conflict" ? save.error : null;
  const [notice, setNotice] = useState<string | null>(null);

  async function resolveConflict(keepCopy: boolean) {
    if (keepCopy) {
      try {
        await navigator.clipboard.writeText(draft);
        setNotice("Your text is on the clipboard. This is the current version of the page.");
      } catch {
        setNotice("Couldn't copy to the clipboard. Select and copy your version below, then reload.");
        return;
      }
    } else {
      setNotice(null);
    }
    save.reset();
    stopEditing();
    refresh();
  }
  const publish = useMutation({
    mutationFn: (v: Visibility) => api<Page>(`/api/wiki/pages/${id}/visibility`, { method: "POST", json: { visibility: v } }),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: () => api(`/api/wiki/pages/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      setConfirmDelete(false);
      refresh();
      navigate("/wiki");
      toast({ title: "Page deleted" });
    },
  });
  usePageTitle({ label: "Wiki", to: "/wiki" }, page.data?.title);

  if (page.error)
    return (
      <div className="page">
        <ErrorNote error={page.error} />
      </div>
    );
  const p = page.data;
  if (!p)
    return (
      <div className="page">
        <SkeletonRows rows={5} />
      </div>
    );

  return (
    <div className="page">
      <PageHeader title={p.title}>
        <VisibilityBadge visibility={p.visibility} />
        {p.can_edit && mode === "view" && (
          <Button
            icon={<Pencil size={16} aria-hidden />}
            onClick={() => startEditing(p)}
          >
            Edit
          </Button>
        )}
        <Button icon={<History size={16} aria-hidden />} onClick={() => setMode(mode === "history" ? "view" : "history")}>
          {mode === "history" ? "Back to page" : "History"}
        </Button>
        {(p.mine || (isAdmin && p.visibility === "public")) && (
          <VisibilityControl
            visibility={p.visibility}
            item="page"
            name={p.path}
            mine={p.mine}
            onChange={(v) => publish.mutateAsync(v)}
          />
        )}
        {p.can_edit && (
          <Button variant="danger" icon={<Trash2 size={16} aria-hidden />} onClick={() => setConfirmDelete(true)}>
            Delete
          </Button>
        )}
      </PageHeader>
      <div className="muted small mono">
        {p.path} · updated {formatDate(p.updated_at)}
      </div>
      <ErrorNote error={(conflict ? null : save.error) || publish.error || remove.error || history.error || diff.error} />
      {notice && <Banner tone="info">{notice}</Banner>}
      {conflict && (
        <Card className="conflict" role="alert" aria-label="Edit conflict">
          <strong>This page changed since you opened it.</strong>
          <p className="muted small">Someone (or the librarian) saved a newer version. Your edits were not saved.</p>
          <div className="conflict-columns">
            <div>
              <div className="list-label">Current version</div>
              <pre className="excerpt">{String((conflict.body as { current_content?: string } | null)?.current_content ?? "")}</pre>
            </div>
            <div>
              <div className="list-label">Your version</div>
              <pre className="excerpt">{draft}</pre>
            </div>
          </div>
          <div className="actions">
            <Button variant="primary" onClick={() => resolveConflict(true)}>
              Copy my text and reload
            </Button>
            <Button onClick={() => resolveConflict(false)}>Discard my edits</Button>
          </div>
        </Card>
      )}
      {mode === "view" && <Markdown>{p.content}</Markdown>}
      {mode === "edit" && (
        <>
          <TextArea
            ref={editor}
            label="Page content"
            hideLabel
            hint="Text inside a Keep block is never changed by the librarian."
            inputClassName="editor"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div>
            <Button variant="link" onClick={protect}>
              Keep block
            </Button>
          </div>
          <TextField
            label="Change summary"
            hideLabel
            placeholder="What changed (optional)"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
          />
          <div className="actions">
            <Button variant="primary" onClick={() => save.mutate()} pending={save.isPending}>
              Save
            </Button>
            <Button onClick={stopEditing}>Cancel</Button>
          </div>
        </>
      )}
      {mode === "history" && (
        <div className="history">
          <ul className="commits">
            {history.data?.map((c) => (
              <li key={c.sha}>
                <Button variant="link" aria-pressed={diffSha === c.sha} onClick={() => setDiffSha(c.sha)}>
                  {c.message}
                </Button>
                <span className="muted small">
                  {c.author} · {formatDate(c.date)}
                </span>
              </li>
            ))}
          </ul>
          {diff.data && <DiffView text={diff.data.diff} />}
        </div>
      )}
      <UnsavedGuard when={mode === "edit" && draft !== p.content && !save.isPending} what="your edits to this page" />
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete ${p.path}?`}
        body={
          p.visibility === "public"
            ? "The page is removed for you and your teammates. This can't be undone here."
            : "The page is removed from your wiki. This can't be undone here."
        }
        confirmLabel="Delete page"
        danger
        pending={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
    </div>
  );
}

export default function WikiPage() {
  const { pageId } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const pages = useQuery({ queryKey: ["wiki-pages"], queryFn: () => api<PageSummary[]>("/api/wiki/pages") });

  useEffect(() => {
    setCreating(false);
  }, [pageId]);

  return (
    <div className="split">
      <nav className="split-list" aria-label="Wiki pages">
        <Button variant="primary" icon={<Plus size={16} aria-hidden />} onClick={() => setCreating(true)}>
          New page
        </Button>
        <Link
          to="/wiki/changes"
          className={`list-item ${!pageId && !creating ? "active" : ""}`}
          onClick={() => setCreating(false)}
        >
          Librarian changes
        </Link>
        <ErrorNote error={pages.error} />
        {pages.isLoading && <SkeletonRows />}
        {pages.data && <PageTree pages={pages.data} current={pageId ? Number(pageId) : undefined} />}
      </nav>
      <section className="split-main">
        {creating ? (
          <NewPage
            onCreated={(p) => {
              qc.invalidateQueries({ queryKey: ["wiki-pages"] });
              setCreating(false);
              navigate(`/wiki/${p.id}`);
            }}
          />
        ) : pageId ? (
          <PageView id={Number(pageId)} />
        ) : (
          <LibrarianPanel />
        )}
      </section>
    </div>
  );
}
