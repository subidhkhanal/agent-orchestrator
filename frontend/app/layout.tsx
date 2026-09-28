import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "agent-orchestrator",
  description: "Supervisor-worker multi-agent orchestration with human approval",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
