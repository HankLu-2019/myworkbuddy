import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, streamPost, type ChatMessage, type Citation, type Project, type Session } from "../api";
import { useAuth } from "../auth";
import { ErrorNote, Markdown, SOURCE_LABELS, VisibilityBadge } from "../components/ui";

export function citationHref(c: Citation): string {
  if (c.source_ref.startsWith("wiki:")) return `/wiki/${c.source_ref.slice(5)}`;
  if (c.source_ref.startsWith("session:")) return `/chat/${c.source_ref.slice(8)}`;
  if (c.source_type === "upload") return "/documents";
  return "/projects";
}

function Citations({ items }: { items: Citation[] }) {
  if (!items.length) return null;
  return (
    <div className="citations">
      {items.map((c) => (
        <Link key={c.n} to={citationHref(c)} className="citation" title={c.source_ref}>
          <span className="cite-n">{c.n}</span>
          {c.title}
          <span className="cite-type">{SOURCE_LABELS[c.source_type] ?? c.source_type}</span>
        </Link>
      ))}
    </div>
  );
}

function ToolActivity({ tools }: { tools: { name: string; args: Record<string, unknown> }[] }) {
  if (!tools.length) return null;
  return (
    <div className="tool-activity">
      {tools.map((t, i) => (
        <div key={i}>
          <span className="mono">{t.name}</span> {String(t.args.query ?? t.args.session_id ?? "")}
        </div>
      ))}
    </div>
  );
}

type Pending = { user: string; text: string; tools: { name: string; args: Record<string, unknown> }[]; error?: string };

/** The user message a reply answered, for Retry. */
export function previousUserText(messages: ChatMessage[], replyId: number): string {
  const idx = messages.findIndex((m) => m.id === replyId);
  for (let i = idx - 1; i >= 0; i--) if (messages[i].role === "user") return messages[i].content;
  return "";
}

/** What to tell the user for each error code from the chat stream (design 01 §H13, 07-ux §6.8). */
export function chatErrorText(code: string | undefined, message: string | undefined): string {
  switch (code) {
    case "budget_exceeded":
      return message || "You've reached your monthly AI budget. An admin can raise it.";
    case "session_busy":
      return "A reply is still being written in another tab.";
    case "llm_unavailable":
      return message || "The AI model isn't answering right now. Try again in a minute.";
    default:
      return message || "Something went wrong. Try again.";
  }
}

export default function ChatPage() {
  const { sessionId } = useParams();
  const { user } = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  const [streaming, setStreaming] = useState(false);
  const stream = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const sessions = useQuery({ queryKey: ["sessions"], queryFn: () => api<Session[]>("/api/chat/sessions") });
  const shared = useQuery({ queryKey: ["sessions", "shared"], queryFn: () => api<Session[]>("/api/chat/shared") });
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/api/projects") });
  const session = useQuery({
    queryKey: ["session", sessionId],
    queryFn: () => api<Session>(`/api/chat/sessions/${sessionId}`),
    enabled: !!sessionId,
  });
  const messages = useQuery({
    queryKey: ["messages", sessionId],
    queryFn: () => api<ChatMessage[]>(`/api/chat/sessions/${sessionId}/messages`),
    enabled: !!sessionId,
  });

  const isOwner = session.data?.owner_id === user?.id;

  // Braces matter: newer browsers return a Promise from scrollIntoView, and an effect must return a cleanup function
  // or nothing. Returning the Promise crashed React's commit phase and blanked the page mid-reply.
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages.data, pending]);

  // Switching sessions drops any in-flight reply and stale error from the previous one.
  useEffect(() => {
    setPending(null);
    return () => stream.current?.abort();
  }, [sessionId]);

  const create = useMutation({
    mutationFn: () => api<Session>("/api/chat/sessions", { method: "POST", json: {} }),
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: ["sessions"] });
      navigate(`/chat/${s.id}`);
    },
  });

  const patch = useMutation({
    mutationFn: (body: Partial<Pick<Session, "title" | "visibility" | "project_id">>) =>
      api<Session>(`/api/chat/sessions/${sessionId}`, { method: "PATCH", json: body }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["session", sessionId] });
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
  });

  const summarize = useMutation({
    mutationFn: () => api(`/api/chat/sessions/${sessionId}/close`, { method: "POST" }),
  });

  const remove = useMutation({
    mutationFn: () => api(`/api/chat/sessions/${sessionId}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sessions"] });
      navigate("/chat");
    },
  });

  async function send(e?: FormEvent, resend?: string) {
    e?.preventDefault();
    const content = (resend ?? draft).trim();
    if (!content || !sessionId || streaming) return;
    if (resend === undefined) setDraft("");
    const controller = new AbortController();
    stream.current = controller;
    const state: Pending = { user: content, text: "", tools: [] };
    let savedId: number | null = null;
    setPending({ ...state });
    setStreaming(true);
    try {
      await streamPost(
        `/api/chat/sessions/${sessionId}/messages`,
        { content },
        ({ event, data }) => {
          if (event === "delta") state.text += data.text;
          else if (event === "tool_call") state.tools = [...state.tools, data];
          else if (event === "error") state.error = chatErrorText(data.code, data.message);
          else if (event === "done") savedId = data.message_id ?? null;
          setPending({ ...state });
        },
        controller.signal,
      );
    } catch (err) {
      if (controller.signal.aborted) return;
      state.error = err instanceof ApiError ? chatErrorText(err.code, err.message) : String(err);
    } finally {
      setStreaming(false);
    }
    await qc.invalidateQueries({ queryKey: ["messages", sessionId] });
    qc.invalidateQueries({ queryKey: ["sessions"] });
    qc.invalidateQueries({ queryKey: ["session", sessionId] });
    // The saved messages now include this turn, including a partial or failed reply (which explains itself).
    // Keep an error note only when nothing was saved, e.g. the chat was busy.
    setPending(state.error && savedId === null ? { user: "", text: "", tools: [], error: state.error } : null);
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  return (
    <div className="chat">
      <section className="chat-list">
        <button className="primary block" onClick={() => create.mutate()} disabled={create.isPending}>
          New chat
        </button>
        <div className="list-label">Your sessions</div>
        {sessions.data?.map((s) => (
          <Link key={s.id} to={`/chat/${s.id}`} className={`list-item ${s.id === sessionId ? "active" : ""}`}>
            <span className="truncate">{s.title}</span>
            {s.source === "claude-code" && <span className="tag">CC</span>}
            {s.visibility === "public" && <span className="tag tag-team">Team</span>}
          </Link>
        ))}
        {!!shared.data?.length && <div className="list-label">Shared by teammates</div>}
        {shared.data?.map((s) => (
          <Link key={s.id} to={`/chat/${s.id}`} className={`list-item ${s.id === sessionId ? "active" : ""}`}>
            <span className="truncate">{s.title}</span>
          </Link>
        ))}
      </section>

      <section className="chat-main">
        {!sessionId ? (
          <div className="chat-empty">
            <h2>Ask about your work</h2>
            <p className="muted">
              Answers draw on your private knowledge and anything your team has published. Start a new chat, or pick
              a past session on the left.
            </p>
          </div>
        ) : (
          <>
            <div className="chat-bar">
              <div className="chat-title truncate">{session.data?.title}</div>
              {session.data && <VisibilityBadge visibility={session.data.visibility} />}
              {isOwner && (
                <div className="chat-tools">
                  <select
                    aria-label="Project in focus"
                    value={session.data?.project_id ?? ""}
                    onChange={(e) => patch.mutate({ project_id: e.target.value ? Number(e.target.value) : null })}
                  >
                    <option value="">No project in focus</option>
                    {projects.data?.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                  <button
                    onClick={() =>
                      patch.mutate({ visibility: session.data?.visibility === "public" ? "private" : "public" })
                    }
                  >
                    {session.data?.visibility === "public" ? "Make private" : "Share with team"}
                  </button>
                  <button onClick={() => summarize.mutate()} disabled={summarize.isPending}>
                    {summarize.isSuccess ? "Summary queued" : "Save to memory"}
                  </button>
                  <button
                    className="danger"
                    onClick={() => window.confirm("Delete this session and its summary?") && remove.mutate()}
                  >
                    Delete
                  </button>
                </div>
              )}
            </div>
            <div className="messages">
              {messages.data?.map((m) => (
                <div key={m.id} className={`msg msg-${m.role}${m.status && m.status !== "complete" ? ` msg-${m.status}` : ""}`}>
                  {m.role === "assistant" && <ToolActivity tools={m.tools} />}
                  {m.status === "failed" ? (
                    <div className="error-text" role="note">
                      {chatErrorText(m.error_code ?? undefined, m.content)}
                    </div>
                  ) : (
                    <Markdown>{m.content}</Markdown>
                  )}
                  {m.status === "partial" && (
                    <div className="muted small" role="note">
                      Incomplete — the model call failed.
                    </div>
                  )}
                  {m.role === "assistant" && <Citations items={m.citations} />}
                  {m.role === "assistant" && (m.cost_usd ?? 0) > 0 && (
                    <div className="muted small" aria-label="Cost of this reply">
                      ${(m.cost_usd ?? 0) < 0.01 ? "<0.01" : (m.cost_usd ?? 0).toFixed(2)}
                    </div>
                  )}
                  {m.role === "assistant" && m.status !== "complete" && m.status !== undefined && isOwner && (
                    <button disabled={streaming} onClick={() => void send(undefined, previousUserText(messages.data ?? [], m.id))}>
                      Retry
                    </button>
                  )}
                </div>
              ))}
              {pending && (
                <>
                  {pending.user && (
                    <div className="msg msg-user">
                      <Markdown>{pending.user}</Markdown>
                    </div>
                  )}
                  <div className="msg msg-assistant">
                    <ToolActivity tools={pending.tools} />
                    {pending.text ? <Markdown>{pending.text}</Markdown> : !pending.error && <div className="typing">Thinking…</div>}
                    <ErrorNote error={pending.error} />
                  </div>
                </>
              )}
              <div ref={bottom} />
            </div>
            {isOwner && (
              <form className="composer" onSubmit={send}>
                <textarea
                  aria-label="Message"
                  placeholder="Ask about a project, a past decision, an error… (Enter to send, Shift+Enter for a new line)"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={onKey}
                  rows={3}
                />
                <button className="primary" type="submit" disabled={!draft.trim() || streaming}>
                  Send
                </button>
              </form>
            )}
          </>
        )}
      </section>
    </div>
  );
}
