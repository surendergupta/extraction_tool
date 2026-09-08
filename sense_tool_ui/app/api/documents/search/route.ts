import { NextRequest, NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend";
import { backendUnreachable } from "@/lib/proxy";

// Proxies to GET {backend}/documents/search?q=...&limit=...&offset=...
export async function GET(request: NextRequest) {
  const backendUrl = getBackendUrl();
  const search = request.nextUrl.search; // includes the leading "?"

  let backendResponse: Response;
  try {
    backendResponse = await fetch(`${backendUrl}/documents/search${search}`, {
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
