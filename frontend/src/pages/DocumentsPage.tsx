import { useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type DocumentItem, type Visibility } from "../api";
import { Empty, ErrorNote, PageHeader, VisibilityBadge, formatDate } from "../components/ui";

export default function DocumentsPage() {
  const qc = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
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
    onSettled: () => {
      refresh();
      if (fileInput.current) fileInput.current.value = "";
    },
  });

  const visibility = useMutation({
    mutationFn: ({ id, v }: { id: number; v: Visibility }) =>
      api(`/api/knowledge/documents/${id}/visibility`, { method: "POST", json: { visibility: v } }),
    onSuccess: refresh,
  });

  const remove = useMutation({
    mutationFn: (id: number) => api(`/api/knowledge/documents/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
  });

  return (
    <div className="page">
      <PageHeader title="Documents">
        <label className="button primary">
          Upload files
          <input
            ref={fileInput}
            type="file"
            multiple
            hidden
            accept=".pdf,.md,.markdown,.txt,.rst,.log,.yaml,.yml,.json,.csv"
            onChange={(e) => e.target.files?.length && upload.mutate(e.target.files)}
          />
        </label>
      </PageHeader>
      <p className="muted lede">
        PDFs, Markdown and text files. New uploads are private; share one with the team when it is useful to
        others.
      </p>
      {upload.isPending && <div className="muted">Uploading and indexing…</div>}
      <ErrorNote error={upload.error || visibility.error || remove.error || docs.error} />
      {docs.data && !docs.data.length && <Empty>No documents yet. Upload a runbook, design doc or postmortem.</Empty>}
      {!!docs.data?.length && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Title</th>
                <th>Visibility</th>
                <th>Chunks</th>
                <th>Updated</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {docs.data.map((d) => (
                <tr key={d.id}>
                  <td>{d.title}</td>
                  <td>
                    <VisibilityBadge visibility={d.visibility} />
                  </td>
                  <td className="num">{d.chunks}</td>
                  <td>{formatDate(d.updated_at)}</td>
                  <td className="row-actions">
                    {d.mine && (
                      <>
                        <button
                          onClick={() =>
                            visibility.mutate({ id: d.id, v: d.visibility === "public" ? "private" : "public" })
                          }
                        >
                          {d.visibility === "public" ? "Make private" : "Share with team"}
                        </button>
                        <button
                          className="danger"
                          onClick={() => window.confirm(`Delete ${d.title}?`) && remove.mutate(d.id)}
                        >
                          Delete
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
