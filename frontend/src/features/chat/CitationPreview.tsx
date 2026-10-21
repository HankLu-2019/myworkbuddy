import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { api, type Citation, type Page } from "../../api";
import { Drawer, ErrorNote, KeyValue, Markdown, SOURCE_LABELS, SkeletonRows, VisibilityBadge } from "../../components/ui";

export function citationHref(c: Citation): string {
  if (c.source_ref.startsWith("wiki:")) return `/wiki/${c.source_ref.slice(5)}`;
  if (c.source_ref.startsWith("session:")) return `/chat/${c.source_ref.slice(8)}`;
  if (c.source_type === "upload") return "/sources/documents";
  const project = (c.meta as { project_id?: number }).project_id;
  return project ? `/sources/repos/${project}` : "/sources/repos";
}

function WikiPreview({ id }: { id: string }) {
  const page = useQuery({ queryKey: ["wiki", Number(id)], queryFn: () => api<Page>(`/api/wiki/pages/${id}`) });
  if (page.error) return <ErrorNote error={page.error} />;
  if (!page.data) return <SkeletonRows rows={4} />;
  return (
    <div className="stack">
      <div className="row">
        <span className="mono small grow">{page.data.path}</span>
        <VisibilityBadge visibility={page.data.visibility} />
      </div>
      <Markdown>{page.data.content}</Markdown>
    </div>
  );
}

/** The cited source beside the conversation (07 §5.2), with "Open full page" to leave the chat. */
export function CitationDrawer({ citation, onClose }: { citation: Citation | null; onClose: () => void }) {
  const meta = (citation?.meta ?? {}) as Record<string, unknown>;
  return (
    <Drawer open={!!citation} onOpenChange={(o) => !o && onClose()} title={citation ? `[${citation.n}] ${citation.title}` : "Source"}>
      {citation && (
        <div className="stack cite-drawer">
          <Link to={citationHref(citation)} onClick={onClose}>
            <ExternalLink size={14} aria-hidden /> Open full page
          </Link>
          {citation.source_ref.startsWith("wiki:") ? (
            <WikiPreview id={citation.source_ref.slice(5)} />
          ) : (
            <KeyValue
              items={[
                ["Type", SOURCE_LABELS[citation.source_type] ?? citation.source_type],
                ["Visibility", <VisibilityBadge visibility={citation.visibility} />],
                ...(typeof meta.path === "string" ? [["Path", <span className="mono small">{meta.path}</span>] as [string, ReactNode]] : []),
                ["Reference", <span className="mono small">{citation.source_ref}</span>],
              ]}
            />
          )}
        </div>
      )}
    </Drawer>
  );
}
