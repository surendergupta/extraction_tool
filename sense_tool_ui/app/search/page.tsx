"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { ApiError, searchDocuments } from "@/lib/api";
import type { SearchResponse } from "@/lib/types";
import StatusBadge from "@/components/StatusBadge";
import ErrorBanner from "@/components/ErrorBanner";
import { renderHighlightedSnippet } from "@/lib/highlight";

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<SearchResponse | null>(null);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const q = query.trim();
    if (!q) {
      setError("Enter a search term.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const response = await searchDocuments(q);
      setResults(response);
    } catch (err) {
      setResults(null);
      setError(err instanceof ApiError ? err.message : "Search failed unexpectedly.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="page">
      <h1>Search documents</h1>
      <p className="muted">
        Full-text search over <code>extracted_text</code> via{" "}
        <code>GET /documents/search?q=...</code>.
      </p>

      <form onSubmit={handleSubmit} className="card" style={{ marginBottom: 20 }}>
        <div className="field">
          <label htmlFor="q">Query</label>
          <input
            id="q"
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="e.g. bronchitis"
          />
        </div>
        <button type="submit" disabled={loading}>
          {loading ? "Searching…" : "Search"}
        </button>
      </form>

      {error && <ErrorBanner message={error} />}

      {results && (
        <>
          <p className="muted">
            {results.count} result{results.count === 1 ? "" : "s"} for &quot;{results.query}&quot;
          </p>
          {results.results.length === 0 && <p className="muted">No matches.</p>}
          {results.results.map((r) => (
            <Link
              key={r.id}
              href={`/documents/${r.id}`}
              style={{ display: "block", textDecoration: "none", color: "inherit" }}
            >
              <div className="card" style={{ marginBottom: 12, cursor: "pointer" }}>
                <p style={{ margin: "0 0 6px" }}>
                  <strong>{r.source}</strong> <StatusBadge status={r.status} />{" "}
                  <span className="muted" style={{ fontSize: 12 }}>
                    {new Date(r.created_at).toLocaleString()}
                    {r.rank != null && ` · rank ${r.rank.toFixed(3)}`}
                  </span>
                </p>
                {r.snippet && <p className="snippet">{renderHighlightedSnippet(r.snippet)}</p>}
                <p className="muted" style={{ fontSize: 12, margin: 0 }}>
                  <code>{r.id}</code>
                </p>
              </div>
            </Link>
          ))}
        </>
      )}
    </main>
  );
}
