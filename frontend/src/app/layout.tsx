import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Evermind — AI Workflow Orchestrator",
  description: "Multi-agent AI collaboration platform. Design, execute, and automate workflows with 100+ AI models.",
  keywords: ["AI", "workflow", "agent", "orchestrator", "GPT", "Claude", "Gemini"],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
      </head>
      <body className="antialiased">
        {children}
      </body>
    </html>
  );
}
