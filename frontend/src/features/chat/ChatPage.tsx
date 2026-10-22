import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Plus, RotateCcw, Send, Trash2 } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  streamPost,
  type ChatMessage,
  type Citation,
  type Project,
  type ProposalCard as ProposalCardData,
  type Session,
} from "../../api";
import { ProposalCard } from "./ProposalCard";
import { useAuth } from "../../auth";
import { usePageTitle } from "../../app/pageTitle";
import { draftKey, useDraft } from "../../app/drafts";
import { CitationDrawer, citationHref } from "./CitationPreview";
import {
  Banner,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorNote,
  Markdown,
  SOURCE_LABELS,
  Select,
  TextArea,
  VisibilityBadge,
  VisibilityControl,
  useToast,
} from "../../components/ui";

export { citationHref } from "./CitationPreview";

const DESKTOP = "(min-width: 768px)";

/** Citation chips: on desktop they open the source in a drawer beside the chat; on phones they navigate. */
function Citations({ items, onOpen }: { items: Citation[]; onOpen: (c: Citation) => void }) {
  if (!items.length) return null;
  return (
    <div className="citations">
      {items.map((c) => (
        <Link
          key={c.n}
          to={citationHref(c)}
          className="citation"
          title={c.source_ref}
          onClick={(e) => {
            if (window.matchMedia?.(DESKTOP).matches && !e.metaKey && !e.ctrlKey) {
              e.preventDefault();
              onOpen(c);
            }
          }}
        >
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

type Pending = {
  user: string;
  text: string;
  tools: { name: string; args: Record<string, unknown> }[];
  proposals?: ProposalCardData[];
  error?: string;
};

/** First suggested prompt when a chat starts from a run (07 §5.7). */
export const INVESTIGATE_PROMPT = "What's the likely cause and what should I check?";

/** First suggested prompt when a chat starts from a plan revision ("Discuss in chat", 03 §9). */
export const DISCUSS_PROMPT = "What are the weak spots in this plan?";

type ContextRef = { type: "run" | "plan_revision"; id: string };

/** `?context=run:<id>` or `?context=plan_revision:<id>` → the refs to attach to a new session. */
export function contextFromParam(value: string | null): ContextRef[] {
  const m = value?.match(/^(run|plan_revision):([0-9a-f-]{36})$/i);
  return m ? [{ type: m[1] as ContextRef["type"], id: m[2] }] : [];
}

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
  const [params, setParams] = useSearchParams();
  const { user } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const qc = useQueryClient();
  const [cited, setCited] = useState<Citation | null>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const {
    value: draft,
    setValue: setDraft,
    clear: clearDraft,
    restored,
    acknowledge,
  } = useDraft(sessionId ? draftKey(user?.id, "chat", sessionId) : null);
  // Once per session, even when StrictMode runs effects twice.
  const announced = useRef<string | null>(null);
  useEffect(() => {
    if (restored && sessionId && announced.current !== sessionId) {
      announced.current = sessionId;
      toast({ title: "Restored your unsaved text" });
      acknowledge();
    }
  }, [restored, sessionId, toast, acknowledge]);

  const location = useLocation();
  const prefill = (location.state as { prefill?: string } | null)?.prefill;
  useEffect(() => {
    if (prefill && sessionId && !draft) setDraft(prefill);
  }, [prefill, sessionId]);

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
  // A new or reopened session puts the cursor in the composer (07 §5.2), once the composer exists.
  useEffect(() => {
    if (sessionId && isOwner) composer.current?.focus();
  }, [sessionId, isOwner]);
  usePageTitle({ label: "Chat", to: "/chat" }, sessionId ? session.data?.title : undefined);

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
    mutationFn: (context: ContextRef[] = []) =>
      api<Session>("/api/chat/sessions", {
        method: "POST",
        json: context.length ? { context, title: context[0].type === "run" ? "Investigate a run" : "Discuss a plan" } : {},
      }),
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: ["sessions"] });
      // A chat started from a run or a plan suggests its first question (07 §5.7, 03 §9).
      const first = s.context?.[0]?.type;
      const prefill = first === "run" ? INVESTIGATE_PROMPT : first === "plan_revision" ? DISCUSS_PROMPT : undefined;
      navigate(`/chat/${s.id}`, { state: prefill ? { prefill } : undefined });
    },
  });

  const patch = useMutation({
    mutationFn: (body: Partial<Pick<Session, "title" | "visibility" | "project_id" | "context">>) =>
      api<Session>(`/api/chat/sessions/${sessionId}`, { method: "PATCH", json: body }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["session", sessionId] });
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
  });

  const summarize = useMutation({
    mutationFn: () => api(`/api/chat/sessions/${sessionId}/close`, { method: "POST" }),
    onSuccess: () => toast({ title: "Summary queued", tone: "success" }),
  });

  // `c` and the "New chat" links open /chat?new=1 (07 §5); create once, even under StrictMode's double effects.
  const creating = useRef(false);
  useEffect(() => {
    const context = contextFromParam(params.get("context"));
    if ((params.get("new") !== "1" && !context.length) || creating.current) return;
    creating.current = true;
    setParams({}, { replace: true });
    create.mutate(context, { onSettled: () => (creating.current = false) });
  }, [params]);

  const remove = useMutation({
    mutationFn: () => api(`/api/chat/sessions/${sessionId}`, { method: "DELETE" }),
    onSuccess: () => {
      setConfirmDelete(false);
      qc.invalidateQueries({ queryKey: ["sessions"] });
      navigate("/chat");
      toast({ title: "Chat deleted" });
    },
  });

  async function send(e?: FormEvent, resend?: string) {
    e?.preventDefault();
    const content = (resend ?? draft).trim();
    if (!content || !sessionId || streaming) return;
    if (resend === undefined) clearDraft();
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
          else if (event === "proposal") state.proposals = [...(state.proposals ?? []), data];
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

  const visibility = session.data?.visibility ?? "private";
  const projectOptions: [string, string][] = [
    ["", "No project in focus"],
    ...(projects.data ?? []).map((p) => [String(p.id), p.name] as [string, string]),
  ];

  return (
    <div className="split">
      <nav className="split-list" aria-label="Chat sessions">
        <Button variant="primary" icon={<Plus size={16} aria-hidden />} onClick={() => create.mutate([])} pending={create.isPending}>
          New chat
        </Button>
        <ErrorNote error={create.error} />
        <div className="list-label" id="own-sessions">Your sessions</div>
        <ul className="stack" aria-labelledby="own-sessions">
          {sessions.data?.map((s) => (
            <li key={s.id}>
              <Link
                to={`/chat/${s.id}`}
                className={`list-item ${s.id === sessionId ? "active" : ""}`}
                aria-current={s.id === sessionId ? "page" : undefined}
              >
                <span className="truncate grow">{s.title}</span>
                {s.source === "claude-code" && <span className="badge badge-neutral" title="Imported from Claude Code">CC</span>}
                {s.visibility === "public" && <VisibilityBadge visibility="public" />}
              </Link>
            </li>
          ))}
        </ul>
        {sessions.data && !sessions.data.length && <p className="muted small">No chats yet.</p>}
        {!!shared.data?.length && (
          <>
            <div className="list-label" id="shared-sessions">Shared by teammates</div>
            <ul className="stack" aria-labelledby="shared-sessions">
              {shared.data.map((s) => (
                <li key={s.id}>
                  <Link
                    to={`/chat/${s.id}`}
                    className={`list-item ${s.id === sessionId ? "active" : ""}`}
                    aria-current={s.id === sessionId ? "page" : undefined}
                  >
                    <span className="truncate">{s.title}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </>
        )}
      </nav>

      <section className="chat-main" aria-label="Conversation">
        {!sessionId ? (
          <div className="chat-empty">
            <EmptyState
              action={
                <Button variant="primary" onClick={() => create.mutate([])} pending={create.isPending}>
                  Start a chat
                </Button>
              }
            >
              <h2>Ask about your work</h2>
              <p className="muted">
                Answers draw on your private knowledge and anything your team has published. Start a new chat, or pick
                a past session.
              </p>
            </EmptyState>
          </div>
        ) : (
          <>
            <div className="chat-bar">
              <h1 className="chat-title truncate">{session.data?.title}</h1>
              {session.data && <VisibilityBadge visibility={visibility} />}
              {isOwner && (
                <div className="chat-tools">
                  <Select
                    label="Project in focus"
                    hideLabel
                    value={session.data?.project_id ? String(session.data.project_id) : ""}
                    options={projectOptions}
                    onChange={(e) => patch.mutate({ project_id: e.target.value ? Number(e.target.value) : null })}
                  />
                  <VisibilityControl
                    visibility={visibility}
                    item="chat"
                    name={session.data?.title ?? "this chat"}
                    size="sm"
                    onChange={(v) => patch.mutateAsync({ visibility: v })}
                  />
                  <Button size="sm" onClick={() => summarize.mutate()} pending={summarize.isPending}>
                    {summarize.isSuccess ? "Summary queued" : "Save to memory"}
                  </Button>
                  <Button size="sm" variant="danger" icon={<Trash2 size={14} aria-hidden />} onClick={() => setConfirmDelete(true)}>
                    Delete
                  </Button>
                </div>
              )}
            </div>
            {!!session.data?.context?.length && (
              <div className="row context-chips" aria-label="Attached to this chat">
                <span className="muted small">Attached:</span>
                {session.data.context.map((ref) => (
                  <span key={ref.id} className="chip">
                    <Link to={ref.href ?? (ref.type === "run" ? `/runs/${ref.id}` : "/work")}>
                      {ref.label ?? (ref.type === "run" ? "Run result" : "Plan revision")}
                    </Link>
                    {isOwner && (
                      <button
                        type="button"
                        className="chip-remove"
                        aria-label={`Remove ${ref.type === "run" ? "the run" : "the plan"} from this chat`}
                        onClick={() => patch.mutate({ context: session.data!.context!.filter((r) => r.id !== ref.id) })}
                      >
                        ×
                      </button>
                    )}
                  </span>
                ))}
              </div>
            )}
            {session.data?.source === "run" && (
              <Banner tone="info">
                This is the transcript of a scheduled run. To ask about it, open the run and choose Investigate in chat.
              </Banner>
            )}
            <ErrorNote error={patch.error ?? summarize.error} />
            <div className="messages" aria-live="polite" aria-busy={streaming}>
              {messages.data?.map((m) => (
                <article
                  key={m.id}
                  aria-label={m.role === "user" ? "You" : "Assistant"}
                  className={`msg msg-${m.role}${m.status && m.status !== "complete" ? ` msg-${m.status}` : ""}`}
                >
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
                  {m.role === "assistant" && <Citations items={m.citations} onOpen={setCited} />}
                  {m.proposals?.map((p) => <ProposalCard key={p.proposal_id} card={p} />)}
                  {m.role === "assistant" && (m.cost_usd ?? 0) > 0 && (
                    <div className="muted small" aria-label="Cost of this reply">
                      ${(m.cost_usd ?? 0) < 0.01 ? "<0.01" : (m.cost_usd ?? 0).toFixed(2)}
                    </div>
                  )}
                  {m.role === "assistant" && m.status !== "complete" && m.status !== undefined && isOwner && (
                    <Button
                      size="sm"
                      icon={<RotateCcw size={14} aria-hidden />}
                      disabled={streaming}
                      onClick={() => void send(undefined, previousUserText(messages.data ?? [], m.id))}
                    >
                      Retry
                    </Button>
                  )}
                </article>
              ))}
              {pending && (
                <>
                  {pending.user && (
                    <article className="msg msg-user" aria-label="You">
                      <Markdown>{pending.user}</Markdown>
                    </article>
                  )}
                  <article className="msg msg-assistant" aria-label="Assistant">
                    <ToolActivity tools={pending.tools} />
                    {pending.text ? (
                      <Markdown>{pending.text}</Markdown>
                    ) : (
                      !pending.error && <div className="typing" role="status">Thinking…</div>
                    )}
                    {pending.proposals?.map((p) => <ProposalCard key={p.proposal_id} card={p} />)}
                    <ErrorNote error={pending.error} />
                  </article>
                </>
              )}
              <div ref={bottom} />
            </div>
            {isOwner && session.data?.source !== "run" && (
              <form className="composer" onSubmit={send}>
                <TextArea
                  ref={composer}
                  label="Message"
                  hideLabel
                  placeholder="Ask about a project, a past decision, an error… (Enter to send, Shift+Enter for a new line)"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={onKey}
                  rows={3}
                />
                <Button variant="primary" type="submit" icon={<Send size={16} aria-hidden />} disabled={!draft.trim() || streaming}>
                  Send
                </Button>
              </form>
            )}
            <CitationDrawer citation={cited} onClose={() => setCited(null)} />
            <ConfirmDialog
              open={confirmDelete}
              onOpenChange={setConfirmDelete}
              title="Delete this chat?"
              body="The chat and its saved summary are removed. This can't be undone."
              confirmLabel="Delete chat"
              danger
              pending={remove.isPending}
              onConfirm={() => remove.mutate()}
            />
          </>
        )}
      </section>
    </div>
  );
}
