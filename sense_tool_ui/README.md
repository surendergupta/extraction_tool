# sense_tool_ui

A minimal internal testing UI for [Sense_tool](../README.md) — upload
documents, watch them move through the pipeline, browse what you've
uploaded, and search. Not a polished product; there's no design system here
beyond basic usability.

This is a separate, independent Next.js app. It does not modify anything in
`../app/` or `../ocr_service/` — it only talks to the backend over its
existing REST endpoints.

## Setup

Requires the Sense_tool backend running somewhere reachable (see
[../README.md](../README.md) — `docker compose up` from the repo root is
the easiest way, or run it locally).

```bash
cp .env.local.example .env.local
# edit .env.local if the backend isn't at http://localhost:8000

npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

## Configuration

- `NEXT_PUBLIC_API_URL` — base URL of the Sense_tool FastAPI backend.
  Defaults to `http://localhost:8000` if unset.

## Pages

- **`/` (`/upload` redirects here)** — upload a file + optional source,
  then polls the document's status every 2.5s until `DONE`/`FAILED`.
- **`/documents`** — see "The document list" below.
- **`/documents/[id]`** — full detail: status, extracted text (expandable),
  structured data (pretty-printed JSON), and PDF/DOCX/XLSX export buttons.
- **`/search`** — full-text search, with the backend's `<b>`-highlighted
  snippets rendered as bold text.

## Why this app proxies instead of calling the backend directly

The FastAPI backend has no CORS middleware configured, and this app isn't
allowed to modify backend code to add one. So instead of the browser
calling `NEXT_PUBLIC_API_URL` directly (which the browser would block on
CORS), all four backend calls go through this app's own same-origin API
routes (`app/api/documents/**/route.ts`), which do the actual
server-to-server fetch to the backend — CORS is a browser-enforced
mechanism, so it doesn't apply there. `NEXT_PUBLIC_API_URL` is still the
single place that configures where the backend is; it's just read
server-side by those routes rather than from client-side `fetch` calls.

## The document list (`/documents`) has a real limitation

The backend has **no "list all documents" endpoint** — only
`POST /documents/intake`, `GET /documents/{id}`, `GET /documents/search`,
and `GET /documents/{id}/export` (see
`../app/routers/documents.py`). This app can't add one without
modifying the backend, which the brief for this app rules out.

So `/documents` instead shows documents **this browser has uploaded or
viewed**, tracked in `localStorage` (see `lib/recentDocuments.ts`) — every
successful upload, and every document detail page you visit (including via
Search), gets added. It's a real, useful "recent documents" view for
testing, but it is not a global list, has no pagination, and won't show
documents uploaded through some other client (`curl`, another browser,
etc.). The page says this explicitly. If the backend later grows a real
list endpoint, swap `lib/recentDocuments.ts`'s role in that page for a
direct fetch.

## Snippet rendering

Search snippets come back from the backend with literal `<b>`/`</b>`
markup (`ts_headline`'s default). Per the brief, that markup is rendered
(bolded), not stripped or shown as escaped text — but *not* via
`dangerouslySetInnerHTML`: `lib/highlight.tsx` parses just the `<b>`
vocabulary itself into real React elements, since the rest of that string
is un-escaped OCR'd document text and shouldn't be trusted as HTML.

## Error handling

Every page handles: the backend being unreachable (network error →
readable message, not a blank screen or console-only failure), 404s
(document detail page shows a clear "not found" state), and the backend's
own 4xx/5xx error bodies (surfaced via their `detail` field). See
`lib/api.ts`'s `ApiError`.

## Stack

Next.js (App Router) + TypeScript, plain `fetch`, plain CSS
(`app/globals.css`) — no state library, no Tailwind; this is small enough
not to need either.
