import { redirect } from "next/navigation";

// The upload page lives at "/" - this just covers the "/upload" path from
// the spec so either URL works.
export default function UploadRedirect() {
  redirect("/");
}
