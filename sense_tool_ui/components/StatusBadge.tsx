import type { DocumentStatus } from "@/lib/types";

const CLASS_BY_STATUS: Record<DocumentStatus, string> = {
  QUEUED: "badge badge-queued",
  PROCESSING: "badge badge-processing",
  DONE: "badge badge-done",
  FAILED: "badge badge-failed",
};

export default function StatusBadge({ status }: { status: DocumentStatus }) {
  return <span className={CLASS_BY_STATUS[status]}>{status}</span>;
}
