import { Link } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { api, type ImportResult } from "../api";
import { ErrorNote, PageHeader } from "../components/ui";

export default function ImportsPage() {
  const upload = useMutation({
    mutationFn: (files: FileList) => {
      const form = new FormData();
      Array.from(files).forEach((f) => form.append("files", f));
      return api<ImportResult[]>("/api/imports/claude-code", { method: "POST", form });
    },
  });

  return (
    <div className="page">
      <PageHeader title="Imports" />
      <h2>Claude Code sessions</h2>
      <p className="muted lede">
        Your Claude Code history already records a lot of your work. Upload the session files from{" "}
        <code>~/.claude/projects/&lt;project&gt;/*.jsonl</code>. Each becomes a private session here and gets a summary
        in your memory. Re-uploading a file only updates it if the session grew.
      </p>
      <label className="button primary">
        Choose .jsonl files
        <input type="file" accept=".jsonl" multiple hidden onChange={(e) => e.target.files?.length && upload.mutate(e.target.files)} />
      </label>
      {upload.isPending && <div className="muted">Importing…</div>}
      <ErrorNote error={upload.error} />
      {upload.data && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>File</th>
                <th>Result</th>
                <th>Session</th>
              </tr>
            </thead>
            <tbody>
              {upload.data.map((r) => (
                <tr key={r.filename}>
                  <td className="mono small">{r.filename}</td>
                  <td>
                    <span className={`pill pill-${r.status}`}>{r.status}</span>
                    {r.error && <div className="error-text small">{r.error}</div>}
                  </td>
                  <td>{r.session_id ? <Link to={`/chat/${r.session_id}`}>{r.title}</Link> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
