import { NextRequest, NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend";
import { backendUnreachable } from "@/lib/proxy";

// Proxies to POST {backend}/documents/intake. Same-origin from the
// browser's point of view - see README for why this app proxies instead of
// calling the FastAPI backend directly from client code (no CORS
// middleware there, and this app can't add one).
export async function POST(request: NextRequest) {
  const backendUrl = getBackendUrl();

  let formData: FormData;
  try {
    formData = await request.formData();
  } catch {
    return NextResponse.json({ detail: "Invalid form data" }, { status: 400 });
  }

  let backendResponse: Response;
  try {
    // Re-post the FormData as-is; fetch sets a fresh multipart
    // Content-Type/boundary for it automatically - don't set one manually.
    backendResponse = await fetch(`${backendUrl}/documents/intake`, {
      method: "POST",
      body: formData,
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
