// The backend has no "list all documents" endpoint (only intake / get-by-id
// / search / export - see sense_tool/app/routers/documents.py) and this app
// isn't allowed to add one. So the /documents page shows documents this
// browser has uploaded or opened, tracked here in localStorage, rather than
// a true global list. See the README and the banner on /documents.

const STORAGE_KEY = "sense_tool_recent_documents";
const MAX_ENTRIES = 50;

function isBrowser(): boolean {
  return typeof window !== "undefined";
}

export function getRecentDocumentIds(): string[] {
  if (!isBrowser()) return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((id): id is string => typeof id === "string");
  } catch {
    return [];
  }
}

export function addRecentDocument(id: string): void {
  if (!isBrowser()) return;
  try {
    const existing = getRecentDocumentIds().filter((existingId) => existingId !== id);
    const next = [id, ...existing].slice(0, MAX_ENTRIES);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // localStorage unavailable (private mode, quota, etc.) - not fatal
  }
}

export function removeRecentDocument(id: string): void {
  if (!isBrowser()) return;
  try {
    const next = getRecentDocumentIds().filter((existingId) => existingId !== id);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // ignore
  }
}
