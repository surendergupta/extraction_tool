// Mirrors sense_tool/app/schemas.py and app/enums.py. Kept in sync by hand -
// this is a separate app with no shared package, per the backend's scope.

export type DocumentStatus = "QUEUED" | "PROCESSING" | "DONE" | "FAILED";

export type ExportFormat = "pdf" | "docx" | "xlsx";

export interface DocumentIntakeResponse {
  id: string;
  status: DocumentStatus;
  source: string;
}

export interface DocumentOut {
  id: string;
  source: string;
  status: DocumentStatus;
  raw_file_path: string;
  extracted_text: string | null;
  structured_data: Record<string, unknown> | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface DocumentSummary {
  id: string;
  source: string;
  status: DocumentStatus;
  created_at: string;
  updated_at: string;
  rank: number | null;
  snippet: string | null;
}

export interface SearchResponse {
  query: string;
  count: number;
  results: DocumentSummary[];
}

/** Shape of a FastAPI HTTPException error body: {"detail": "..."} or
 * (for 422 validation errors) {"detail": [{...}, ...]}. */
export interface ApiErrorBody {
  detail?: string | Array<{ msg?: string; [key: string]: unknown }>;
}
