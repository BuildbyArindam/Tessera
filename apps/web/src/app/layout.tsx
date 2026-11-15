import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Sidebar } from "@/components/Sidebar";
import "./globals.css";

export const metadata: Metadata = {
  title: "Tessera · treasury dashboard",
  description: "Policy-bounded treasury agent: balances, forecasts and autonomy level.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <a className="skip-link" href="#main">
          Skip to content
        </a>
        <div className="shell">
          <Sidebar />
          <main id="main" className="main" tabIndex={-1}>
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}
