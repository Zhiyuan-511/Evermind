import type { Metadata } from 'next';
import Script from 'next/script';
import { Inter, JetBrains_Mono } from 'next/font/google';
import './globals.css';

const inter = Inter({
  subsets: ['latin'],
  variable: '--font-sans',
  display: 'swap',
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ['latin'],
  variable: '--font-mono',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'Evermind — AI Workflow Orchestrator',
  description: 'Multi-agent AI collaboration platform. Design, execute, and automate workflows with 100+ AI models.',
  keywords: ['AI', 'workflow', 'agent', 'orchestrator', 'GPT', 'Claude', 'Gemini'],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <Script id="evermind-theme-init" strategy="beforeInteractive">
          {`
            try {
              const savedTheme = localStorage.getItem('evermind-theme');
              document.documentElement.dataset.theme = savedTheme || 'dark';
            } catch {
              document.documentElement.dataset.theme = 'dark';
            }
          `}
        </Script>
      </head>
      <body className={`${inter.variable} ${jetbrainsMono.variable} antialiased`}>
        {children}
      </body>
    </html>
  );
}
