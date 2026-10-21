import { useMemo, useState, type ReactNode } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import { Button } from "./Button";
import { SkeletonRows } from "./Feedback";

export function Card({ children, className, as: As = "div", ...rest }: { children: ReactNode; className?: string; as?: "div" | "section" | "form"; [k: string]: unknown }) {
  const Tag = As as "div";
  return (
    <Tag className={["card", className].filter(Boolean).join(" ")} {...rest}>
      {children}
    </Tag>
  );
}

export type Column<T> = {
  key: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  /** Value for sorting; the column is sortable when given. */
  sortValue?: (row: T) => string | number;
  numeric?: boolean;
  className?: string;
};

/** The one table: sortable headers, a loading state, an empty row and "Load more" (07 §8). */
export function DataTable<T>({
  label,
  columns,
  rows,
  rowKey,
  loading,
  empty,
  onLoadMore,
  loadingMore,
  rowLabel,
}: {
  label: string;
  columns: Column<T>[];
  rows: T[] | undefined;
  rowKey: (row: T) => string | number;
  loading?: boolean;
  empty?: ReactNode;
  onLoadMore?: () => void;
  loadingMore?: boolean;
  /** Accessible name for each row (e.g. the item's title), so tests and screen readers can find it. */
  rowLabel?: (row: T) => string;
}) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);
  const sorted = useMemo(() => {
    if (!rows || !sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col?.sortValue) return rows;
    const value = col.sortValue;
    return [...rows].sort((a, b) => (value(a) < value(b) ? -sort.dir : value(a) > value(b) ? sort.dir : 0));
  }, [rows, sort, columns]);
  if (loading && !rows) return <SkeletonRows />;
  if (rows && !rows.length && empty) return <>{empty}</>;
  return (
    <div className="table-wrap">
      <table aria-label={label}>
        <thead>
          <tr>
            {columns.map((c) => {
              const active = sort?.key === c.key;
              return (
                <th
                  key={c.key}
                  className={c.numeric ? "num" : undefined}
                  aria-sort={active ? (sort!.dir === 1 ? "ascending" : "descending") : undefined}
                  scope="col"
                >
                  {c.sortValue ? (
                    <button
                      type="button"
                      className="sort"
                      onClick={() => setSort(active && sort!.dir === 1 ? { key: c.key, dir: -1 } : { key: c.key, dir: 1 })}
                    >
                      {c.header}
                      {active && (sort!.dir === 1 ? <ArrowUp size={12} aria-hidden /> : <ArrowDown size={12} aria-hidden />)}
                    </button>
                  ) : (
                    c.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted?.map((row) => (
            <tr key={rowKey(row)} aria-label={rowLabel?.(row)}>
              {columns.map((c) => (
                <td key={c.key} className={[c.numeric && "num", c.className].filter(Boolean).join(" ") || undefined}>
                  {c.cell(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {onLoadMore && (
        <div className="table-foot">
          <Button onClick={onLoadMore} pending={loadingMore}>
            Load more
          </Button>
        </div>
      )}
    </div>
  );
}

export function KeyValue({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([k, v], i) => (
        <div key={i} style={{ display: "contents" }}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function CodeBlock({ children, label }: { children: string; label?: string }) {
  return (
    <pre className="code-block" aria-label={label} tabIndex={0}>
      {children}
    </pre>
  );
}

/** Unified diff with added and removed lines marked (also by +/- text, not colour alone). */
export function DiffView({ text, label = "Changes" }: { text: string; label?: string }) {
  return (
    <pre className="diff" aria-label={label} tabIndex={0}>
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
