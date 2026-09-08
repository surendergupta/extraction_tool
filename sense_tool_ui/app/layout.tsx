import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Sense_tool UI",
  description: "Internal testing UI for the Sense_tool document pipeline",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>
        <nav className="nav">
          <Link href="/" className="nav-brand">
            Sense_tool
          </Link>
          <Link href="/" className="nav-link">
            Upload
          </Link>
          <Link href="/documents" className="nav-link">
            Documents
          </Link>
          <Link href="/search" className="nav-link">
            Search
          </Link>
        </nav>
        {children}
      </body>
    </html>
  );
}
