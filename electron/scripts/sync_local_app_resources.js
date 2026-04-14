const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const rootDir = path.resolve(__dirname, '..', '..');
const electronDir = path.join(rootDir, 'electron');
const localApp = path.join(rootDir, 'Evermind.app');
const resourcesDir = path.join(localApp, 'Contents', 'Resources');

const backendSrc = path.join(rootDir, 'backend');
const backendDest = path.join(resourcesDir, 'backend');
const frontendBuildRoot = path.join(rootDir, 'frontend', '.next');
const frontendStandaloneSrc = path.join(frontendBuildRoot, 'standalone');
const frontendBundleSrc = path.join(electronDir, '.packaged', 'frontend-standalone');
const frontendBundleDest = path.join(resourcesDir, 'frontend-standalone');

const passthroughFiles = [
  ['OPENCLAW_GUIDE.md', 'OPENCLAW_GUIDE.md'],
  ['evermind_godmode_final.html', 'evermind_godmode_final.html'],
];

const SKIP_SEGMENTS = new Set([
  '__pycache__',
  '.pytest_cache',
  '.mypy_cache',
  '.ruff_cache',
  '.DS_Store',
]);

function fail(message) {
  console.error(`[sync_local_app_resources] ${message}`);
  process.exit(1);
}

function readBuildId(buildIdPath) {
  try {
    return fs.readFileSync(buildIdPath, 'utf8').trim();
  } catch (_error) {
    return '';
  }
}

function ensurePreparedFrontendBundle() {
  const standaloneServer = path.join(frontendStandaloneSrc, 'server.js');
  const packagedServer = path.join(frontendBundleSrc, 'server.js');
  const sourceBuildId = readBuildId(path.join(frontendBuildRoot, 'BUILD_ID'));
  const packagedBuildId = readBuildId(path.join(frontendBundleSrc, '.next', 'BUILD_ID'));

  if (!fs.existsSync(standaloneServer) && !fs.existsSync(packagedServer)) {
    fail(
      `frontend bundle missing: neither ${standaloneServer} nor ${packagedServer} exists. `
      + 'Run `npm --prefix frontend run build` first.'
    );
  }

  if (!fs.existsSync(standaloneServer)) {
    console.warn(
      `[sync_local_app_resources] frontend standalone build missing, reusing packaged bundle build=${packagedBuildId || 'unknown'}`
    );
    return;
  }

  const shouldRefreshPackaged =
    !fs.existsSync(packagedServer)
    || (sourceBuildId && sourceBuildId !== packagedBuildId);

  if (!shouldRefreshPackaged) {
    console.log(
      `[sync_local_app_resources] frontend bundle already fresh (build=${sourceBuildId || packagedBuildId || 'unknown'})`
    );
    return;
  }

  execFileSync('node', [path.join(__dirname, 'prepare_frontend_bundle.js')], {
    cwd: electronDir,
    stdio: 'inherit',
  });
}

function shouldCopy(srcPath) {
  const normalized = String(srcPath || '');
  return !normalized.split(path.sep).some((segment) => SKIP_SEGMENTS.has(segment));
}

function copyTree(src, dest, { optional = false } = {}) {
  if (!fs.existsSync(src)) {
    if (optional) {
      console.warn(`[sync_local_app_resources] skipped missing optional path: ${src}`);
      return;
    }
    fail(`missing source path: ${src}`);
  }
  fs.rmSync(dest, { recursive: true, force: true });
  fs.cpSync(src, dest, {
    recursive: true,
    force: true,
    filter: shouldCopy,
  });
  console.log(`[sync_local_app_resources] synced ${path.relative(rootDir, dest)}`);
}

if (!fs.existsSync(localApp)) {
  fail(`local app bundle missing: ${localApp}`);
}

fs.mkdirSync(resourcesDir, { recursive: true });
ensurePreparedFrontendBundle();

copyTree(backendSrc, backendDest);
copyTree(frontendBundleSrc, frontendBundleDest);

for (const [srcRel, destRel] of passthroughFiles) {
  const src = path.join(rootDir, srcRel);
  const dest = path.join(resourcesDir, destRel);
  if (!fs.existsSync(src)) continue;
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.copyFileSync(src, dest);
  console.log(`[sync_local_app_resources] synced ${destRel}`);
}

console.log(`[sync_local_app_resources] local app resources are in sync: ${localApp}`);

// ── Post-sync: send SIGHUP to trigger hot-reload in running backend ──
// The Python sidecar registers a SIGHUP handler that reloads key modules
// (workflow_templates, task_classifier, html_postprocess, etc.) so file
// changes take effect without restarting the app.
const stateDir = path.join(require('os').homedir(), '.evermind');
const lockFile = path.join(stateDir, 'backend.lock');

let sighupSent = false;
try {
  // Strategy 1: Read PID from backend.lock file (JSON format: {"pid": ..., ...})
  if (fs.existsSync(lockFile)) {
    const lockContent = fs.readFileSync(lockFile, 'utf8').trim();
    let pid = 0;
    try {
      const lockData = JSON.parse(lockContent);
      pid = parseInt(lockData.pid, 10) || 0;
    } catch (_) {
      pid = parseInt(lockContent, 10) || 0;
    }
    if (pid > 0) {
      try {
        process.kill(pid, 'SIGHUP');
        console.log(`[sync_local_app_resources] SIGHUP sent to backend PID ${pid} — modules will hot-reload`);
        sighupSent = true;
      } catch (e) {
        // PID stale or process gone — fall through to pgrep
      }
    }
  }
  // Strategy 2: pgrep for the server.py process
  if (!sighupSent) {
    try {
      const pids = execFileSync('pgrep', ['-f', 'python.*server\\.py'], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
      }).trim();
      if (pids) {
        const firstPid = pids.split('\n')[0].trim();
        execFileSync('kill', ['-HUP', firstPid], { stdio: 'ignore' });
        console.log(`[sync_local_app_resources] SIGHUP sent to backend PID ${firstPid} (via pgrep) — modules will hot-reload`);
        sighupSent = true;
      }
    } catch (_) {
      // No running backend found — that's fine
    }
  }
  if (!sighupSent) {
    console.log('[sync_local_app_resources] no running backend found — restart the app to load new code');
  }
} catch (e) {
  console.warn(`[sync_local_app_resources] SIGHUP attempt failed: ${e.message}`);
}
