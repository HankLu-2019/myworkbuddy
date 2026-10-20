import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type Hit } from "../api";
import { Empty, ErrorNote, PageHeader, SOURCE_LABELS, VisibilityBadge } from "../components/ui";
import { citationHref } from "./ChatPage";

const SCOPES = [
  ["all", "Everything"],
  ["wiki", "Wiki"],
  ["sessions", "Past sessions"],
  ["code", "Code"],
  ["documents", "Documents"],
] as const;

export default function SearchPage() {
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<string>("all");

  const results = useQuery({
    queryKey: ["search", query, scope],
    queryFn: () => api<Hit[]>(`/api/knowledge/search?${new URLSearchParams({ q: query, scope, limit: "20" })}`),
    enabled: !!query,
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    setQuery(input.trim());
  }

  return (
    <div className="page">
      <PageHeader title="Search" />
      <p className="muted lede">
        Combines keyword (BM25) and semantic search over your private knowledge and everything your team has
        published.
      </p>
      <form className="search-bar" onSubmit={submit}>
        <input
          aria-label="Search query"
          placeholder="e.g. OOMKilled spark executor, SPIN-4412, why we chose pgvector"
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
        <select aria-label="Scope" value={scope} onChange={(e) => setScope(e.target.value)}>
          {SCOPES.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
        <button className="primary" type="submit">
          Search
        </button>
      </form>
      <ErrorNote error={results.error} />
      {results.isFetching && <div className="muted">Searching…</div>}
      {results.data && !results.data.length && <Empty>No matches. Try different words or a wider scope.</Empty>}
      <ol className="hits">
        {results.data?.map((h) => (
          <li key={`${h.document_id}-${h.excerpt.slice(0, 20)}`} className="hit card">
            <div className="hit-head">
              <Link to={citationHref({ ...h, n: 0 })}>{h.title}</Link>
              <span className="tag">{SOURCE_LABELS[h.source_type] ?? h.source_type}</span>
              <VisibilityBadge visibility={h.visibility} />
              <span className="score mono">{h.score.toFixed(4)}</span>
            </div>
            <pre className="excerpt">{h.excerpt}</pre>
          </li>
        ))}
      </ol>
    </div>
  );
}
