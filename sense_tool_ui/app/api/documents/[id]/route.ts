import { NextRequest, NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend";
import { backendUnreachable } from "@/lib/proxy";

// Proxies to GET {backend}/documents/{id}
export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const backendUrl = getBackendUrl();

  let backendResponse: Response;
  try {
    backendResponse = await fetch(`${backendUrl}/documents/${encodeURIComponent(id)}`, {
      cache: "no-store",
    });
  } catch (err) {
    return backendUnreachable(backendUrl, err);
  }

  const body = await backendResponse.text();
  return new NextResponse(body, {
    status: backendResponse.status,
    headers: { "Content-Type": backendResponse.headers.get("content-type") ?? "application/json" },
  });
}
