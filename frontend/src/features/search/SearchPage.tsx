import { useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { api, type Hit } from "../../api";
import { usePageTitle } from "../../app/pageTitle";
import { Button, Card, EmptyState, ErrorNote, PageHeader, SOURCE_LABELS, Select, SkeletonRows, TextField, VisibilityBadge } from "../../components/ui";
import { citationHref } from "../chat/ChatPage";

const SCOPES = [
  ["all", "Everything"],
  ["wiki", "Wiki"],
  ["sessions", "Past sessions"],
  ["code", "Code"],
  ["documents", "Documents"],
] as const;

/** Search (07 §4.4): query and scope live in the URL, so results can be linked and the top bar can open them. */
export default function SearchPage() {
  const [params, setParams] = useSearchParams();
  const query = params.get("q") ?? "";
  const scope = params.get("scope") ?? "all";
  const [input, setInput] = useState(query);
  usePageTitle({ label: "Search", to: "/search" }, query && `“${query}”`);
  useEffect(() => setInput(query), [query]);

  const results = useQuery({
    queryKey: ["search", query, scope],
    queryFn: () => api<Hit[]>(`/api/knowledge/search?${new URLSearchParams({ q: query, scope, limit: "20" })}`),
    enabled: !!query,
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    const q = input.trim();
    setParams(q ? { q, scope } : { scope });
  }

  return (
    <div className="page">
      <PageHeader title="Search" />
      <p className="muted lede">
        Combines keyword (BM25) and semantic search over your private knowledge and everything your team has
        published.
      </p>
      <form className="search-bar" role="search" onSubmit={submit}>
        <TextField
          label="Search query"
          hideLabel
          placeholder="e.g. OOMKilled spark executor, SPIN-4412, why we chose pgvector"
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
        <Select
          label="Scope"
          hideLabel
          value={scope}
          options={SCOPES}
          onChange={(e) => setParams(query ? { q: query, scope: e.target.value } : { scope: e.target.value })}
        />
        <Button variant="primary" type="submit" icon={<Search size={16} aria-hidden />}>
          Search
        </Button>
      </form>
      <ErrorNote error={results.error} />
      {results.isFetching && !results.data && <SkeletonRows />}
      {results.isFetching && results.data && <div className="muted" role="status">Searching…</div>}
      {results.data && !results.data.length && <EmptyState>No matches. Try different words or a wider scope.</EmptyState>}
      <ol className="hits" aria-label="Results">
        {results.data?.map((h) => (
          <li key={`${h.document_id}-${h.excerpt.slice(0, 20)}`}>
            <Card>
              <div className="hit-head">
                <Link to={citationHref({ ...h, n: 0 })}>{h.title}</Link>
                <span className="badge badge-neutral">{SOURCE_LABELS[h.source_type] ?? h.source_type}</span>
                <VisibilityBadge visibility={h.visibility} />
                <span className="score mono" aria-label={`score ${h.score.toFixed(4)}`}>
                  {h.score.toFixed(4)}
                </span>
              </div>
              <pre className="excerpt">{h.excerpt}</pre>
            </Card>
          </li>
        ))}
      </ol>
    </div>
  );
}
