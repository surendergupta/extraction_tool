import { NextResponse } from "next/server";

/** Consistent 502 body/shape for "couldn't reach the FastAPI backend at all"
 * (as opposed to the backend responding with its own 4xx/5xx, which gets
 * passed through as-is). */
export function backendUnreachable(backendUrl: string, err: unknown): NextResponse {
  const message = err instanceof Error ? err.message : String(err);
  return NextResponse.json(
    { detail: `Could not reach Sense_tool backend at ${backendUrl}: ${message}` },
    { status: 502 },
  );
}
