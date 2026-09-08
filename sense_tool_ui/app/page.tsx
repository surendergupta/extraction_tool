"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import Link from "next/link";
import { ApiError, getDocument, intakeDocument } from "@/lib/api";
import { addRecentDocument } from "@/lib/recentDocuments";
import type { DocumentIntakeResponse, DocumentOut } from "@/lib/types";
import StatusBadge from "@/components/StatusBadge";
import ErrorBanner from "@/components/ErrorBanner";
import JsonBlock from "@/components/JsonBlock";
import ExpandableText from "@/components/ExpandableText";

const POLL_INTERVAL_MS = 2500;

export default function UploadPage() {
  const [source, setSource] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [intakeResult, setIntakeResult] = useState<DocumentIntakeResponse | null>(null);
  const [document, setDocument] = useState<DocumentOut | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
    };
  }, []);

  function startPolling(id: string) {
    if (pollTimer.current) clearInterval(pollTimer.current);

    const poll = async () => {
      try {
        const doc = await getDocument(id);
        setDocument(doc);
        setPollError(null);
        if (doc.status === "DONE" || doc.status === "FAILED") {
          if (pollTimer.current) clearInterval(pollTimer.current);
        }
      } catch (err) {
        setPollError(err instanceof ApiError ? err.message : "Failed to check document status.");
      }
    };

    poll();
    pollTimer.current = setInterval(poll, POLL_INTERVAL_MS);
  }

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!file) {
      setSubmitError("Choose a file first.");
      return;
    }

    setSubmitting(true);
    setSubmitError(null);
    setIntakeResult(null);
    setDocument(null);
    setPollError(null);
    if (pollTimer.current) clearInterval(pollTimer.current);

    try {
      const result = await intakeDocument(source.trim() || "unknown", file);
      setIntakeResult(result);
      addRecentDocument(result.id);
      startPolling(result.id);
    } catch (err) {
      setSubmitError(err instanceof ApiError ? err.message : "Upload failed unexpectedly.");
    } finally {
      setSubmitting(false);
    }
  }

  const currentStatus = document?.status ?? intakeResult?.status;
  const isPolling = !!intakeResult && (currentStatus === "QUEUED" || currentStatus === "PROCESSING");

  return (
    <main className="page">
      <h1>Upload a document</h1>
      <p className="muted">
        Sends the file to <code>POST /documents/intake</code>, then polls{" "}
        <code>GET /documents/&#123;id&#125;</code> every {POLL_INTERVAL_MS / 1000}s until it
        reaches DONE or FAILED.
      </p>

      <form onSubmit={handleSubmit} className="card" style={{ marginBottom: 20 }}>
        <div className="field">
          <label htmlFor="source">Source (optional — e.g. hospital/clinic name)</label>
          <input
            id="source"
            type="text"
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder="unknown"
          />
        </div>
        <div className="field">
          <label htmlFor="file">File</label>
          <input id="file" type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </div>
        <button type="submit" disabled={submitting}>
          {submitting ? "Uploading…" : "Upload"}
        </button>
      </form>

      {submitError && <ErrorBanner message={submitError} />}

      {intakeResult && (
        <section className="card">
          <h2 style={{ marginTop: 0 }}>
            Document <code>{intakeResult.id}</code>
          </h2>
          <p>
            <Link href={`/documents/${intakeResult.id}`}>View full detail page →</Link>
          </p>

          {pollError && <ErrorBanner message={pollError} />}

          <p>
            {isPolling && <span className="spinner" aria-hidden />}
            Status: {currentStatus && <StatusBadge status={currentStatus} />}
          </p>

          {document?.status === "FAILED" && (
            <ErrorBanner
              message={document.error_message ?? "Processing failed (no error message returned)."}
            />
          )}

          {document?.status === "DONE" && (
            <>
              <h3>Extracted text</h3>
              <ExpandableText text={document.extracted_text || "(empty)"} />

              <h3>Structured data</h3>
              <JsonBlock value={document.structured_data} />
            </>
          )}
        </section>
      )}
    </main>
  );
}
