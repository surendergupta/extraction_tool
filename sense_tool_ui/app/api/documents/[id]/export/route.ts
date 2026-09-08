import { NextRequest, NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend";
import { backendUnreachable } from "@/lib/proxy";

// Proxies to GET {backend}/documents/{id}/export?format=pdf|docx|xlsx and
// streams the binary body straight through, preserving Content-Type and
// Content-Disposition so the browser triggers a normal file download.
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const backendUrl = getBackendUrl();
  const search = request.nextUrl.search;

  let backendResponse: Response;
  try {
    backendResponse = await fetch(
      `${backendUrl}/documents/${encodeURIComponent(id)}/export${search}`,
    );
  } catch (err) {
    return backendUnreachable(backendUrl, err);
  }

  if (!backendResponse.ok) {
    // Error responses are JSON ({"detail": "..."}) - pass through as text.
    const body = await backendResponse.text();
    return new NextResponse(body, {
      status: backendResponse.status,
      headers: { "Content-Type": backendResponse.headers.get("content-type") ?? "application/json" },
    });
  }

  const buffer = await backendResponse.arrayBuffer();
  const headers = new Headers();
  const contentType = backendResponse.headers.get("content-type");
  const contentDisposition = backendResponse.headers.get("content-disposition");
  if (contentType) headers.set("Content-Type", contentType);
  if (contentDisposition) headers.set("Content-Disposition", contentDisposition);

  return new NextResponse(buffer, { status: backendResponse.status, headers });
}
