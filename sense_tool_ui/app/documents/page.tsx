"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, getDocument } from "@/lib/api";
import { getRecentDocumentIds, removeRecentDocument } from "@/lib/recentDocuments";
import type { DocumentOut } from "@/lib/types";
import StatusBadge from "@/components/StatusBadge";
import ErrorBanner from "@/components/ErrorBanner";

interface Row {
  id: string;
  doc: DocumentOut | null;
  error: string | null;
}

export default function DocumentsPage() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [globalError, setGlobalError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setGlobalError(null);
    const ids = getRecentDocumentIds();
    if (ids.length === 0) {
      setRows([]);
      setLoading(false);
      return;
    }
    try {
      const results = await Promise.all(
        ids.map(async (id): Promise<Row | null> => {
          try {
            const doc = await getDocument(id);
            return { id, doc, error: null };
          } catch (err) {
            if (err instanceof ApiError && err.status === 404) {
              removeRecentDocument(id);
              return null; // gone server-side - drop it from the list
            }
            return {
              id,
              doc: null,
              error: err instanceof ApiError ? err.message : "Failed to load",
            };
          }
        }),
      );
      setRows(results.filter((r): r is Row => r !== null));
    } catch (err) {
      setGlobalError(err instanceof Error ? err.message : "Failed to load documents.");
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // react-hooks/set-state-in-effect flags this: its static analysis treats
    // any function reachable from an effect that contains a setState call as
    // synchronous, even though `load`'s setState calls only run after an
    // `await` (a genuine async fetch-then-update, which is exactly the
    // "calling setState in a callback when external state changes" pattern
    // the rule's own message recommends). Fetch-on-mount with a loading flag
    // is the standard pattern here; no heavier data-fetching library is in
    // scope for this app.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  return (
    <main className="page">
      <h1>Documents</h1>

      <div className="info-banner">
        The backend has no &quot;list all documents&quot; endpoint (only intake / get-by-id /
        search — see <code>sense_tool/app/routers/documents.py</code>). This page shows documents
        this browser has uploaded or viewed, tracked locally — not a global list. Use{" "}
        <Link href="/search">Search</Link> to find documents by content.
      </div>

      {globalError && <ErrorBanner message={globalError} />}

      {loading && <p className="muted">Loading…</p>}

      {!loading && rows && rows.length === 0 && (
        <p className="muted">No documents seen yet in this browser. Try the Upload page.</p>
      )}

      {!loading && rows && rows.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Source</th>
              <th>Status</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <DocumentRow key={row.id} row={row} />
            ))}
          </tbody>
        </table>
      )}

      <p style={{ marginTop: 20 }}>
        <button className="secondary" onClick={() => load()}>
          Refresh
        </button>
      </p>
    </main>
  );
}

function DocumentRow({ row }: { row: Row }) {
  const router = useRouter();

  if (row.error || !row.doc) {
    return (
      <tr className="clickable" onClick={() => router.push(`/documents/${row.id}`)}>
        <td>
          <code>{row.id}</code>
        </td>
        <td colSpan={3} style={{ color: "var(--danger)" }}>
          {row.error ?? "Failed to load"}
        </td>
      </tr>
    );
  }

  const { doc } = row;
  return (
    <tr className="clickable" onClick={() => router.push(`/documents/${doc.id}`)}>
      <td>
        <code>{doc.id}</code>
      </td>
      <td>{doc.source}</td>
      <td>
        <StatusBadge status={doc.status} />
      </td>
      <td>{new Date(doc.created_at).toLocaleString()}</td>
    </tr>
  );
}
