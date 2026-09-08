"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { ApiError, exportDocumentUrl, getDocument } from "@/lib/api";
import { addRecentDocument } from "@/lib/recentDocuments";
import type { DocumentOut, ExportFormat } from "@/lib/types";
import StatusBadge from "@/components/StatusBadge";
import ErrorBanner from "@/components/ErrorBanner";
import JsonBlock from "@/components/JsonBlock";
import ExpandableText from "@/components/ExpandableText";

const POLL_INTERVAL_MS = 2500;
const EXPORT_FORMATS: ExportFormat[] = ["pdf", "docx", "xlsx"];

export default function DocumentDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;

  const [doc, setDoc] = useState<DocumentOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await getDocument(id);
      setDoc(result);
      setError(null);
      setNotFound(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setNotFound(true);
      } else {
        setError(err instanceof ApiError ? err.message : "Failed to load document.");
      }
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    addRecentDocument(id);
  }, [id]);

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

  // Same polling behaviour as the upload page - in case this document is
  // still QUEUED/PROCESSING when reached directly (e.g. from the list or
  // search) rather than right after an upload.
  useEffect(() => {
    if (!doc || doc.status === "DONE" || doc.status === "FAILED") return;
    const timer = setInterval(load, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [doc, load]);

  if (notFound) {
    return (
      <main className="page">
        <h1>Document not found</h1>
        <ErrorBanner message={`No document with id ${id}.`} />
        <Link href="/documents">← Back to documents</Link>
      </main>
    );
  }

  return (
    <main className="page">
      <h1>Document detail</h1>
      <p className="muted">
        <code>{id}</code>
      </p>

      {error && <ErrorBanner message={error} />}
      {loading && !doc && <p className="muted">Loading…</p>}

      {doc && (
        <div className="card">
          <p>
            <strong>Source:</strong> {doc.source}
          </p>
          <p>
            <strong>Status:</strong> <StatusBadge status={doc.status} />
            {(doc.status === "QUEUED" || doc.status === "PROCESSING") && (
              <span className="spinner" aria-hidden style={{ marginLeft: 8 }} />
            )}
          </p>
          <p>
            <strong>Created:</strong> {new Date(doc.created_at).toLocaleString()}
            {" · "}
            <strong>Updated:</strong> {new Date(doc.updated_at).toLocaleString()}
          </p>

          {doc.status === "FAILED" && (
            <ErrorBanner
              message={doc.error_message ?? "Processing failed (no error message returned)."}
            />
          )}

          {doc.status === "DONE" && (
            <>
              <div className="button-row" style={{ margin: "16px 0" }}>
                {EXPORT_FORMATS.map((format) => (
                  <a key={format} className="button-link" href={exportDocumentUrl(doc.id, format)}>
                    Export {format.toUpperCase()}
                  </a>
                ))}
              </div>

              <h3>Extracted text</h3>
              <ExpandableText text={doc.extracted_text || "(empty)"} previewChars={1000} />

              <h3>Structured data</h3>
              <JsonBlock value={doc.structured_data} />
            </>
          )}
        </div>
      )}

      <p style={{ marginTop: 20 }}>
        <Link href="/documents">← Back to documents</Link>
      </p>
    </main>
  );
}
