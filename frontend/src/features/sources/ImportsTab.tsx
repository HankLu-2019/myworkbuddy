import { Link } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { api, type ImportResult } from "../../api";
import { DataTable, ErrorNote, FileButton, StatusPill, type Column } from "../../components/ui";

const columns: Column<ImportResult>[] = [
  { key: "file", header: "File", cell: (r) => <span className="mono small">{r.filename}</span> },
  {
    key: "result",
    header: "Result",
    cell: (r) => (
      <>
        <StatusPill status={r.status} />
        {r.error && <div className="error-text small">{r.error}</div>}
      </>
    ),
  },
  { key: "session", header: "Session", cell: (r) => (r.session_id ? <Link to={`/chat/${r.session_id}`}>{r.title}</Link> : "—") },
];

/** Sources → Claude Code sessions (07 §4.6). */
export default function ImportsTab() {
  const upload = useMutation({
    mutationFn: (files: FileList) => {
      const form = new FormData();
      Array.from(files).forEach((f) => form.append("files", f));
      return api<ImportResult[]>("/api/imports/claude-code", { method: "POST", form });
    },
  });

  return (
    <>
      <p className="muted lede">
        Your Claude Code history already records a lot of your work. Upload the session files from{" "}
        <code>~/.claude/projects/&lt;project&gt;/*.jsonl</code>. Each becomes a private session here and gets a summary
        in your memory. Re-uploading a file only updates it if the session grew.
      </p>
      <div>
        <FileButton label="Choose .jsonl files" accept=".jsonl" multiple onFiles={(f) => upload.mutate(f)} icon={<Upload size={16} aria-hidden />} />
      </div>
      {upload.isPending && <div className="muted" role="status">Importing…</div>}
      <ErrorNote error={upload.error} />
      {upload.data && <DataTable label="Import results" columns={columns} rows={upload.data} rowKey={(r) => r.filename} />}
    </>
  );
}
