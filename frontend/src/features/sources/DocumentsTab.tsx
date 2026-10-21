import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileUp, RefreshCw, Trash2 } from "lucide-react";
import { api, type DocumentItem, type Visibility } from "../../api";
import {
  Button,
  ConfirmDialog,
  DataTable,
  EmptyState,
  ErrorNote,
  FileButton,
  VisibilityBadge,
  VisibilityControl,
  formatDate,
  type Column,
} from "../../components/ui";

const ACCEPT = ".pdf,.md,.markdown,.txt,.rst,.log,.yaml,.yml,.json,.csv";

/** Sources → Documents (07 §4.6). */
export default function DocumentsTab() {
  const qc = useQueryClient();
  const replaceInput = useRef<HTMLInputElement>(null);
  const [replacing, setReplacing] = useState<number | null>(null);
  const [deleting, setDeleting] = useState<DocumentItem | null>(null);
  const docs = useQuery({ queryKey: ["documents"], queryFn: () => api<DocumentItem[]>("/api/knowledge/documents") });
  const refresh = () => qc.invalidateQueries({ queryKey: ["documents"] });

  const upload = useMutation({
    mutationFn: async (files: FileList) => {
      for (const f of Array.from(files)) {
        const form = new FormData();
        form.append("file", f);
        await api("/api/knowledge/documents", { method: "POST", form });
      }
    },
    onSettled: refresh,
  });

  // Replace keeps the document's id and sharing; a new upload with the same name is a separate document.
  const replace = useMutation({
    mutationFn: ({ id, file }: { id: number; file: File }) => {
      const form = new FormData();
      form.append("file", file);
      return api(`/api/knowledge/documents/${id}/file`, { method: "PUT", form });
    },
    onSettled: () => {
      setReplacing(null);
      refresh();
      if (replaceInput.current) replaceInput.current.value = "";
    },
  });

  const visibility = useMutation({
    mutationFn: ({ id, v }: { id: number; v: Visibility }) =>
      api(`/api/knowledge/documents/${id}/visibility`, { method: "POST", json: { visibility: v } }),
    onSuccess: refresh,
  });

  const remove = useMutation({
    mutationFn: (id: number) => api(`/api/knowledge/documents/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      setDeleting(null);
      refresh();
    },
  });

  const columns: Column<DocumentItem>[] = [
    { key: "title", header: "Title", sortValue: (d) => d.title, cell: (d) => d.title },
    { key: "visibility", header: "Visibility", cell: (d) => <VisibilityBadge visibility={d.visibility} /> },
    { key: "chunks", header: "Chunks", numeric: true, sortValue: (d) => d.chunks, cell: (d) => d.chunks },
    { key: "updated", header: "Updated", sortValue: (d) => d.updated_at, cell: (d) => formatDate(d.updated_at) },
    {
      key: "actions",
      header: <span className="visually-hidden">Actions</span>,
      cell: (d) =>
        d.mine && (
          <div className="row-actions">
            <VisibilityControl size="sm" visibility={d.visibility} onChange={(v) => visibility.mutate({ id: d.id, v })} />
            <Button
              size="sm"
              icon={<RefreshCw size={14} aria-hidden />}
              onClick={() => {
                setReplacing(d.id);
                replaceInput.current?.click();
              }}
            >
              Replace file
            </Button>
            <Button size="sm" variant="danger" icon={<Trash2 size={14} aria-hidden />} onClick={() => setDeleting(d)}>
              Delete
            </Button>
          </div>
        ),
    },
  ];

  return (
    <>
      <div className="row">
        <p className="muted lede grow">
          PDFs, Markdown and text files. New uploads are private; share one with the team when it is useful to
          others.
        </p>
        <FileButton label="Upload files" accept={ACCEPT} multiple onFiles={(f) => upload.mutate(f)} icon={<FileUp size={16} aria-hidden />} />
      </div>
      <input
        ref={replaceInput}
        type="file"
        className="visually-hidden"
        aria-label="Replacement file"
        tabIndex={-1}
        accept={ACCEPT}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file && replacing !== null) replace.mutate({ id: replacing, file });
        }}
      />
      {(upload.isPending || replace.isPending) && (
        <div className="muted" role="status">
          Uploading and indexing…
        </div>
      )}
      <ErrorNote error={upload.error || replace.error || visibility.error || remove.error || docs.error} />
      <DataTable
        label="Documents"
        columns={columns}
        rows={docs.data}
        rowKey={(d) => d.id}
        rowLabel={(d) => d.title}
        loading={docs.isLoading}
        empty={<EmptyState>No documents yet. Upload a runbook, design doc or postmortem.</EmptyState>}
      />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={`Delete ${deleting?.title ?? ""}?`}
        body="Deletes the document and its search index. This can't be undone."
        confirmLabel="Delete document"
        danger
        pending={remove.isPending}
        onConfirm={() => deleting && remove.mutate(deleting.id)}
      />
    </>
  );
}
