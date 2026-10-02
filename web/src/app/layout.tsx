import type { Metadata } from "next";
import Link from "next/link";
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
            <Link href="/upload">Upload</Link>
            <Link href="/evals">Evals and cost</Link>
          </nav>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
