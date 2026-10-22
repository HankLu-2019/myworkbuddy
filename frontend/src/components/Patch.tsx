import { useEffect, useState } from "react";

/** One file of a unified git diff. */
export interface PatchFile {
  path: string;
  lines: string[]; // hunk lines, including @@ headers
  binary: boolean;
}

export function parsePatch(patch: string): PatchFile[] {
  const files: PatchFile[] = [];
  let cur: PatchFile | null = null;
  for (const line of patch.split("\n")) {
    const m = line.match(/^diff --git a\/(.+?) b\/(.+)$/);
    if (m) {
      cur = { path: m[2], lines: [], binary: false };
      files.push(cur);
      continue;
    }
    if (!cur) continue;
    if (line.startsWith("Binary files") || line.startsWith("GIT binary patch")) cur.binary = true;
    else if (line.startsWith("@@") || (cur.lines.length && /^[ +\-\\]/.test(line))) cur.lines.push(line);
  }
  return files;
}

type Row = { left: string | null; right: string | null; kind: "same" | "del" | "add" | "change" | "hunk"; text?: string };

/** Pair removed and added runs side by side, like a review tool's split view. */
export function splitRows(lines: string[]): Row[] {
  const rows: Row[] = [];
  let dels: string[] = [];
  let adds: string[] = [];
  const flush = () => {
    const n = Math.max(dels.length, adds.length);
    for (let i = 0; i < n; i++) {
      const left = dels[i] ?? null;
      const right = adds[i] ?? null;
      rows.push({ left, right, kind: left !== null && right !== null ? "change" : left !== null ? "del" : "add" });
    }
    dels = [];
    adds = [];
  };
  for (const line of lines) {
    if (line.startsWith("@@")) {
      flush();
      rows.push({ left: null, right: null, kind: "hunk", text: line });
    } else if (line.startsWith("-")) dels.push(line.slice(1));
    else if (line.startsWith("+")) adds.push(line.slice(1));
    else if (line.startsWith("\\")) continue;
    else {
      flush();
      rows.push({ left: line.slice(1), right: line.slice(1), kind: "same" });
    }
  }
  flush();
  return rows;
}

function useWide(): boolean {
  const query = "(min-width: 768px)";
  const [wide, setWide] = useState(() => typeof window !== "undefined" && !!window.matchMedia?.(query).matches);
  useEffect(() => {
    const mq = window.matchMedia?.(query);
    if (!mq) return;
    const on = () => setWide(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return wide;
}

/** A patch as one block per file, split (side by side) or unified; phones get unified (07 §7). The +/- marks and
    the column headers carry the meaning, not colour alone. */
export function PatchView({ patch, label = "Changes" }: { patch: string; label?: string }) {
  const wide = useWide();
  const [mode, setMode] = useState<"split" | "unified" | null>(null);
  const view = mode ?? (wide ? "split" : "unified");
  const files = parsePatch(patch);
  if (!files.length) return <p className="muted">No changes.</p>;
  return (
    <section className="patch stack" aria-label={label}>
      <div className="row" role="group" aria-label="Diff layout">
        <button type="button" className={`btn btn-sm ${view === "split" ? "btn-primary" : "btn-secondary"}`} aria-pressed={view === "split"} onClick={() => setMode("split")}>
          <span className="btn-label">Side by side</span>
        </button>
        <button type="button" className={`btn btn-sm ${view === "unified" ? "btn-primary" : "btn-secondary"}`} aria-pressed={view === "unified"} onClick={() => setMode("unified")}>
          <span className="btn-label">Unified</span>
        </button>
        {!wide && <span className="muted small">Easier to review on a larger screen.</span>}
      </div>
      {files.map((f) => (
        <details key={f.path} open className="patch-file">
          <summary className="mono">{f.path}</summary>
          {f.binary ? (
            <p className="muted small">Binary file; not shown.</p>
          ) : view === "unified" ? (
            <pre className="diff" tabIndex={0} aria-label={`Changes in ${f.path}`}>
              {f.lines.map((line, i) => (
                <div key={i} className={line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : ""}>
                  {line || " "}
                </div>
              ))}
            </pre>
          ) : (
            <div className="table-wrap">
              <table className="diff-split" aria-label={`Changes in ${f.path}, side by side`}>
                <thead>
                  <tr>
                    <th scope="col">Before</th>
                    <th scope="col">After</th>
                  </tr>
                </thead>
                <tbody>
                  {splitRows(f.lines).map((r, i) =>
                    r.kind === "hunk" ? (
                      <tr key={i} className="hunk">
                        <td colSpan={2}>{r.text}</td>
                      </tr>
                    ) : (
                      <tr key={i}>
                        <td className={r.left === null ? "empty" : r.kind === "same" ? "" : "del"}>{r.left === null ? "" : `${r.kind === "same" ? " " : "-"} ${r.left}`}</td>
                        <td className={r.right === null ? "empty" : r.kind === "same" ? "" : "add"}>{r.right === null ? "" : `${r.kind === "same" ? " " : "+"} ${r.right}`}</td>
                      </tr>
                    ),
                  )}
                </tbody>
              </table>
            </div>
          )}
        </details>
      ))}
    </section>
  );
}
