import type { NextConfig } from "next";
import path from "path";

const nextConfig: NextConfig = {
  output: "standalone",
  // Pin the tracing root to this frontend package. Without this, Next sees
  // the parent-level package-lock.json and wraps the standalone bundle in
  // an extra frontend/ subdir, which breaks the Evermind.app sync script
  // (it looks for .next/standalone/server.js, not .next/standalone/frontend/server.js).
  outputFileTracingRoot: path.join(__dirname),
};

export default nextConfig;
