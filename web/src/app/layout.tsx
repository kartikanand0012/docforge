import type { Metadata } from "next";
import Link from "next/link";
import SignOut from "@/components/SignOut";
import "./globals.css";

export const metadata: Metadata = {
  title: "DocForge review",
  description: "Review extracted documents against their source, correct them and sign.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>
        <header className="topbar">
          <Link href="/" className="brand">
            DocForge
          </Link>
          <nav aria-label="Main">
            <Link href="/">Review queue</Link>
            <Link href="/documents">Documents</Link>
            <Link href="/upload">Upload</Link>
            <Link href="/search">Search</Link>
            <Link href="/chat">Chat</Link>
            <Link href="/collections">Knowledge bases</Link>
            <Link href="/questions">Unanswered</Link>
            <Link href="/agents">AI agents</Link>
            <Link href="/evals">Evals and cost</Link>
            {/* A file download through the session proxy, not a page. */}
            <a href="/api/v1/exports/documents.csv" download>
              Export signed records (CSV)
            </a>
          </nav>
          <SignOut />
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
