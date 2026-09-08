// Client-side helpers. These all call this Next.js app's OWN API routes
// (app/api/documents/**), not the FastAPI backend directly - see
// app/api/documents/**/route.ts and the README for why (the backend has no
// CORS middleware, and this app isn't allowed to modify the backend to add
// one, so this app's server-side routes act as a same-origin proxy).

import type { DocumentIntakeResponse, DocumentOut, SearchResponse } from "./types";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function parseErrorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail
        .map((e) => (typeof e === "object" && e && "msg" in e ? String(e.msg) : JSON.stringify(e)))
        .join("; ");
    }
  } catch {
    // response wasn't JSON - fall through to the generic message below
  }
  return `Request failed with status ${response.status}`;
}

async function request<T>(input: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(input, init);
  } catch (err) {
    throw new ApiError(0, `Network error: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as T;
}

export async function intakeDocument(
  source: string,
  file: File,
): Promise<DocumentIntakeResponse> {
  const formData = new FormData();
  formData.append("source", source);
  formData.append("file", file);
  return request<DocumentIntakeResponse>("/api/documents/intake", {
    method: "POST",
    body: formData,
  });
}

export async function getDocument(id: string): Promise<DocumentOut> {
  return request<DocumentOut>(`/api/documents/${encodeURIComponent(id)}`, {
    cache: "no-store",
  });
}

export async function searchDocuments(
  query: string,
  opts?: { limit?: number; offset?: number },
): Promise<SearchResponse> {
  const params = new URLSearchParams({ q: query });
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  if (opts?.offset != null) params.set("offset", String(opts.offset));
  return request<SearchResponse>(`/api/documents/search?${params.toString()}`, {
    cache: "no-store",
  });
}

/** URL for an export download <a href>. No fetch needed - the browser
 * follows Content-Disposition: attachment from the (proxied) response. */
export function exportDocumentUrl(id: string, format: "pdf" | "docx" | "xlsx"): string {
  return `/api/documents/${encodeURIComponent(id)}/export?format=${format}`;
}
