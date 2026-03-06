import Link from 'next/link';

export default function Home() {
  return (
    <div className="h-screen flex items-center justify-center" style={{ background: 'radial-gradient(ellipse at center, var(--bg2) 0%, var(--bg1) 70%)' }}>
      <div className="text-center animate-fade-in">
        <div className="text-6xl mb-6">🧠</div>
        <h1 className="text-3xl font-bold mb-2 bg-gradient-to-r from-blue-400 via-purple-400 to-pink-400 bg-clip-text text-transparent">
          Evermind
        </h1>
        <p className="text-[var(--text2)] text-sm mb-8 max-w-sm mx-auto">
          Multi-agent AI workflow platform. Design, execute, and automate with 100+ models.
        </p>

        <Link
          href="/editor"
          className="btn btn-primary text-sm px-8 py-3 rounded-xl inline-flex items-center gap-2"
        >
          🚀 Open Editor
        </Link>

        <div className="mt-12 grid grid-cols-3 gap-6 text-center max-w-md mx-auto">
          <div>
            <div className="text-2xl mb-1">🤖</div>
            <div className="text-[10px] text-[var(--text3)]">100+ AI Models</div>
          </div>
          <div>
            <div className="text-2xl mb-1">🔄</div>
            <div className="text-[10px] text-[var(--text3)]">Auto Retry</div>
          </div>
          <div>
            <div className="text-2xl mb-1">🖥️</div>
            <div className="text-[10px] text-[var(--text3)]">Computer Use</div>
          </div>
        </div>
      </div>
    </div>
  );
}
