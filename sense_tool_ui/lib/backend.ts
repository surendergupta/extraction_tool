// Server-side only (used from app/api/**/route.ts). NEXT_PUBLIC_API_URL is
// read here even though the "public" prefix is meant for client bundles -
// Next.js still makes it available server-side, and the task specifically
// asked for that env var name, so it's reused here rather than adding a
// second, non-public one for the same value.
export function getBackendUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
}
