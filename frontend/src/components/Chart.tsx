import { useState, type ReactNode } from "react";
import { Button } from "./Button";
import { DataTable } from "./Data";

/** One data point of a single-series chart: what it is, its value, and how the value reads. */
export type ChartRow = { key: string; label: string; value: number; display: string };

/** A headline number (08 §F9): label, value, and an optional line of context. */
export function StatTile({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="card stat-tile" role="group" aria-label={label}>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {hint ? <span className="stat-hint">{hint}</span> : null}
    </div>
  );
}

function ChartFrame({
  title,
  rows,
  valueHeader,
  empty,
  children,
}: {
  title: string;
  rows: ChartRow[];
  valueHeader: string;
  empty: ReactNode;
  children: ReactNode;
}) {
  const [table, setTable] = useState(false);
  return (
    <section className="card" aria-label={title}>
      <div className="chart-head">
        <h3>{title}</h3>
        {rows.length > 0 && (
          <Button size="sm" variant="ghost" aria-pressed={table} onClick={() => setTable((t) => !t)}>
            {table ? "Show chart" : "Show table"}
          </Button>
        )}
      </div>
      {rows.length === 0 ? (
        <p className="muted">{empty}</p>
      ) : table ? (
        <DataTable
          label={title}
          rows={rows}
          rowKey={(r) => r.key}
          columns={[
            { key: "label", header: "", cell: (r) => r.label },
            { key: "value", header: valueHeader, cell: (r) => r.display, numeric: true, sortValue: (r) => r.value },
          ]}
        />
      ) : (
        children
      )}
    </section>
  );
}

const share = (v: number, max: number) => (max > 0 ? Math.max(0, v) / max : 0);

/** Horizontal bars for categories, from one baseline, each value at its tip; a table view for every chart. */
export function BarChart({ title, rows, valueHeader, empty = "Nothing yet." }: { title: string; rows: ChartRow[]; valueHeader: string; empty?: ReactNode }) {
  const max = Math.max(0, ...rows.map((r) => r.value));
  return (
    <ChartFrame title={title} rows={rows} valueHeader={valueHeader} empty={empty}>
      <dl className="bars" aria-label={title}>
        {rows.map((r) => (
          <div key={r.key} style={{ display: "contents" }} title={`${r.label}: ${r.display}`}>
            <dt>{r.label}</dt>
            <dd>
              <span className="bar" style={{ width: `${share(r.value, max) * 100}%` }} aria-hidden />
              <span className="bar-value">{r.display}</span>
            </dd>
          </div>
        ))}
      </dl>
    </ChartFrame>
  );
}

/** Columns over time (weeks), oldest first; every cap is labelled when there are few, else the latest and the peak. */
export function ColumnChart({ title, rows, valueHeader, empty = "Nothing yet." }: { title: string; rows: ChartRow[]; valueHeader: string; empty?: ReactNode }) {
  const max = Math.max(0, ...rows.map((r) => r.value));
  const peak = rows.findIndex((r) => r.value === max);
  const capped = (i: number) => rows.length <= 8 || i === rows.length - 1 || i === peak;
  return (
    <ChartFrame title={title} rows={rows} valueHeader={valueHeader} empty={empty}>
      <div className="columns" role="img" aria-label={`${title}: ${rows.map((r) => `${r.label} ${r.display}`).join(", ")}`}>
        {rows.map((r, i) => (
          <div key={r.key} className="column" title={`${r.label}: ${r.display}`}>
            {capped(i) && <span className="column-cap">{r.display}</span>}
            <span className="column-bar" style={{ height: `${share(r.value, max) * 85}%` }} />
          </div>
        ))}
      </div>
      <div className="column-labels" aria-hidden>
        {rows.map((r) => (
          <span key={r.key}>{r.label}</span>
        ))}
      </div>
    </ChartFrame>
  );
}
