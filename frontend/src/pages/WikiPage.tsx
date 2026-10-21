import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Commit, type Page, type PageSummary, type Visibility } from "../api";
import { Empty, ErrorNote, Markdown, PageHeader, VisibilityBadge, formatDate } from "../components/ui";

type LibrarianChange = { sha: string; date: string; message: string; diff: string };

function PageTree({ pages, current }: { pages: PageSummary[]; current?: number }) {
  const groups: [string, PageSummary[]][] = [
    ["My pages", pages.filter((p) => p.visibility === "private")],
    ["Team wiki", pages.filter((p) => p.visibility === "public")],
  ];
  return (
    <>
      {groups.map(([label, items]) => (
        <div key={label}>
          <div className="list-label">{label}</div>
          {!items.length && <div className="muted small pad">None yet</div>}
          {items.map((p) => (
            <Link key={p.id} to={`/wiki/${p.id}`} className={`list-item ${p.id === current ? "active" : ""}`}>
              <span className="mono small truncate">{p.path}</span>
            </Link>
          ))}
        </div>
      ))}
    </>
  );
}

function Diff({ text }: { text: string }) {
  return (
    <pre className="diff">
      {text.split("\n").map((line, i) => (
        <div
          key={i}
          className={line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : ""}
        >
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

function LibrarianPanel() {
  const changes = useQuery({
    queryKey: ["librarian"],
    queryFn: () => api<LibrarianChange[]>("/api/wiki/librarian/changes?days=7"),
  });
  const run = useMutation({ mutationFn: () => api<{ queued: boolean }>("/api/wiki/librarian/run", { method: "POST" }) });
  return (
    <div className="page">
      <PageHeader title="What the librarian changed">
        <button onClick={() => run.mutate()} disabled={run.isPending}>
          {run.isSuccess ? "Queued" : "Update from recent sessions now"}
        </button>
      </PageHeader>
      <p className="muted lede">
        Each night the librarian folds your session summaries into your private pages (how you work, decisions,
        project cards). Every change is a git commit, so you can review it here and edit anything it got wrong.
      </p>
      <ErrorNote error={changes.error || run.error} />
      {changes.data && !changes.data.length && <Empty>No librarian changes in the last 7 days.</Empty>}
      {changes.data?.map((c) => (
        <details key={c.sha} className="card change">
          <summary>
            <span>{c.message}</span> <span className="muted small">{formatDate(c.date)}</span>
          </summary>
          <Diff text={c.diff} />
        </details>
      ))}
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
  return (
    <div className="page">
      <PageHeader title="New page" />
      <p className="muted lede">New pages are private. Publish a page when you want the team to see it.</p>
      <label htmlFor="path">Path</label>
      <input id="path" className="mono" value={path} onChange={(e) => setPath(e.target.value)} placeholder="runbooks/jenkins-oom.md" />
      <label htmlFor="content">Content (Markdown)</label>
      <textarea id="content" className="editor" value={content} onChange={(e) => setContent(e.target.value)} />
      <ErrorNote error={create.error} />
      <div className="actions">
        <button className="primary" onClick={() => create.mutate()} disabled={create.isPending}>
          Create page
        </button>
      </div>
    </div>
  );
}

function PageView({ id }: { id: number }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [mode, setMode] = useState<"view" | "edit" | "history">("view");
  const [draft, setDraft] = useState("");
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

  useEffect(() => {
    setMode("view");
    setDiffSha(null);
  }, [id]);

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
      setMode("view");
      setMessage("");
      refresh();
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
    setMode("view");
    refresh();
  }
  const publish = useMutation({
    mutationFn: (v: Visibility) => api<Page>(`/api/wiki/pages/${id}/visibility`, { method: "POST", json: { visibility: v } }),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: () => api(`/api/wiki/pages/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      refresh();
      navigate("/wiki");
    },
  });

  if (page.error) return <ErrorNote error={page.error} />;
  const p = page.data;
  if (!p) return <div className="muted">Loading…</div>;

  return (
    <div className="page">
      <PageHeader title={p.title}>
        <VisibilityBadge visibility={p.visibility} />
        {p.can_edit && mode === "view" && (
          <button
            onClick={() => {
              setDraft(p.content);
              setBaseSha(p.head_sha ?? "");
              setNotice(null);
              setMode("edit");
            }}
          >
            Edit
          </button>
        )}
        <button onClick={() => setMode(mode === "history" ? "view" : "history")}>
          {mode === "history" ? "Back to page" : "History"}
        </button>
        {p.mine && (
          <button onClick={() => publish.mutate(p.visibility === "public" ? "private" : "public")}>
            {p.visibility === "public" ? "Unpublish" : "Publish to team"}
          </button>
        )}
        {p.can_edit && (
          <button className="danger" onClick={() => window.confirm(`Delete ${p.path}?`) && remove.mutate()}>
            Delete
          </button>
        )}
      </PageHeader>
      <div className="muted small mono">
        {p.path} · updated {formatDate(p.updated_at)}
      </div>
      <ErrorNote error={(conflict ? null : save.error) || publish.error || remove.error || history.error || diff.error} />
      {notice && <div className="notice">{notice}</div>}
      {conflict && (
        <div className="conflict card" role="alertdialog" aria-label="Edit conflict">
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
            <button className="primary" onClick={() => resolveConflict(true)}>
              Copy my text and reload
            </button>
            <button onClick={() => resolveConflict(false)}>Discard my edits</button>
          </div>
        </div>
      )}
      {mode === "view" && <Markdown>{p.content}</Markdown>}
      {mode === "edit" && (
        <>
          <textarea aria-label="Page content" className="editor" value={draft} onChange={(e) => setDraft(e.target.value)} />
          <input aria-label="Change summary" placeholder="What changed (optional)" value={message} onChange={(e) => setMessage(e.target.value)} />
          <div className="actions">
            <button className="primary" onClick={() => save.mutate()} disabled={save.isPending}>
              Save
            </button>
            <button onClick={() => setMode("view")}>Cancel</button>
          </div>
        </>
      )}
      {mode === "history" && (
        <div className="history">
          <ul className="commits">
            {history.data?.map((c) => (
              <li key={c.sha}>
                <button className={`link ${diffSha === c.sha ? "active" : ""}`} onClick={() => setDiffSha(c.sha)}>
                  {c.message}
                </button>
                <span className="muted small">
                  {c.author} · {formatDate(c.date)}
                </span>
              </li>
            ))}
          </ul>
          {diff.data && <Diff text={diff.data.diff} />}
        </div>
      )}
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
      <section className="split-list">
        <button className="primary block" onClick={() => setCreating(true)}>
          New page
        </button>
        <Link to="/wiki" className={`list-item ${!pageId && !creating ? "active" : ""}`} onClick={() => setCreating(false)}>
          Librarian changes
        </Link>
        {pages.data && <PageTree pages={pages.data} current={pageId ? Number(pageId) : undefined} />}
      </section>
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
