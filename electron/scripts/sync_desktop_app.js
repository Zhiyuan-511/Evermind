const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const electronDir = path.resolve(__dirname, '..');
const rootDir = path.resolve(electronDir, '..');
const distApp = path.join(electronDir, 'dist', 'mac-arm64', 'Evermind.app');
const localApp = path.join(rootDir, 'Evermind.app');
const desktopApp = path.join(os.homedir(), 'Desktop', 'Evermind.app');
const RESOURCE_SYNC_ENTRIES = [
  'backend',
  'frontend-standalone',
  'OPENCLAW_GUIDE.md',
  'evermind_godmode_final.html',
];
const CRITICAL_BACKEND_FILES = [
  'ai_bridge.py',
  'html_postprocess.py',
  'orchestrator.py',
  'plugins/implementations.py',
  'preview_validation.py',
  'proxy_relay.py',
  'release_doctor.py',
  'repo_map.py',
  'scripts/desktop_run_goal_monitor.py',
  'scripts/release_doctor.py',
  'server.py',
  'task_classifier.py',
  'runtime_vendor/three/three.min.js',
  'runtime_vendor/phaser/phaser.min.js',
  'runtime_vendor/howler/howler.min.js',
  'workflow_templates.py',
];
const CRITICAL_FRONTEND_FILES = [
  'server.js',
  '.next/BUILD_ID',
  '.next/required-server-files.json',
];

function fail(message) {
  console.error(`[sync_desktop_app] ${message}`);
  process.exit(1);
}

function appHasRuntimeBundle(appPath) {
  return fs.existsSync(path.join(appPath, 'Contents', 'Resources', 'frontend-standalone', 'server.js'));
}

function appFreshness(appPath) {
  const freshnessTargets = [
    path.join(appPath, 'Contents', 'Resources', 'backend', 'orchestrator.py'),
    path.join(appPath, 'Contents', 'Resources', 'frontend-standalone', 'server.js'),
  ];
  let freshest = 0;
  for (const target of freshnessTargets) {
    try {
      freshest = Math.max(freshest, fs.statSync(target).mtimeMs || 0);
    } catch (_error) {
      // Ignore missing freshness targets here; caller validates runtime bundle.
    }
  }
  return freshest;
}

function resolveSourceApp() {
  const candidates = [localApp, distApp].filter((appPath) => appHasRuntimeBundle(appPath));
  if (!candidates.length) {
    fail(
      `No usable source app found. Checked local=${localApp} and dist=${distApp}; `
      + 'run `npm --prefix electron run sync:local-app` or rebuild the desktop package first.'
    );
  }
  candidates.sort((left, right) => appFreshness(right) - appFreshness(left));
  return candidates[0];
}

function overlayResources(sourceApp, targetApp) {
  const sourceResources = path.join(sourceApp, 'Contents', 'Resources');
  const targetResources = path.join(targetApp, 'Contents', 'Resources');

  for (const relPath of RESOURCE_SYNC_ENTRIES) {
    const sourcePath = path.join(sourceResources, relPath);
    const targetPath = path.join(targetResources, relPath);
    if (!fs.existsSync(sourcePath)) continue;
    fs.rmSync(targetPath, { recursive: true, force: true });
    fs.cpSync(sourcePath, targetPath, { recursive: true, force: true });
  }
}

function verifyBackendMirror(sourceApp, targetApp) {
  const sourceBackend = path.join(sourceApp, 'Contents', 'Resources', 'backend');
  const targetBackend = path.join(targetApp, 'Contents', 'Resources', 'backend');
  const mismatches = [];

  for (const relPath of CRITICAL_BACKEND_FILES) {
    const sourcePath = path.join(sourceBackend, relPath);
    const targetPath = path.join(targetBackend, relPath);
    if (!fs.existsSync(sourcePath) || !fs.existsSync(targetPath)) {
      mismatches.push(`${relPath} (missing)`);
      continue;
    }
    if (!fs.readFileSync(sourcePath).equals(fs.readFileSync(targetPath))) {
      mismatches.push(relPath);
    }
  }

  if (mismatches.length) {
    fail(`desktop app drifted after sync. Mismatched critical files: ${mismatches.join(', ')}`);
  }
}

function verifyFrontendMirror(sourceApp, targetApp) {
  const sourceFrontend = path.join(sourceApp, 'Contents', 'Resources', 'frontend-standalone');
  const targetFrontend = path.join(targetApp, 'Contents', 'Resources', 'frontend-standalone');
  const mismatches = [];

  for (const relPath of CRITICAL_FRONTEND_FILES) {
    const sourcePath = path.join(sourceFrontend, relPath);
    const targetPath = path.join(targetFrontend, relPath);
    if (!fs.existsSync(sourcePath) || !fs.existsSync(targetPath)) {
      mismatches.push(`${relPath} (missing)`);
      continue;
    }
    if (!fs.readFileSync(sourcePath).equals(fs.readFileSync(targetPath))) {
      mismatches.push(relPath);
    }
  }

  if (mismatches.length) {
    fail(`desktop frontend drifted after sync. Mismatched critical files: ${mismatches.join(', ')}`);
  }
}

if (!fs.existsSync(distApp)) {
  fail(`Packaged app missing: ${distApp}`);
}

const sourceApp = resolveSourceApp();

const stagingRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'evermind-sync-'));
const stagingApp = path.join(stagingRoot, 'Evermind.app');

// v5.8.6: sign with hardened runtime + entitlements so the Python subprocess
// inherits TCC grants (see electron/build/entitlements.mac.plist). Plain
// ad-hoc without these caused re-prompts every new pipeline run.
const entitlements = path.join(__dirname, '..', 'build', 'entitlements.mac.plist');
const signIdentity = process.env.EVERMIND_CODESIGN_IDENTITY || '-';
const signArgs = (target) => [
  '--force',
  '--deep',
  '--timestamp=none',
  '--options=runtime',
  '--entitlements', entitlements,
  '-s', signIdentity,
  target,
];

try {
  console.log(`[sync_desktop_app] using base ${distApp}`);
  console.log(`[sync_desktop_app] overlaying resources from ${sourceApp}`);
  console.log(`[sync_desktop_app] signing with identity=${signIdentity === '-' ? 'ad-hoc' : signIdentity}`);
  execFileSync('ditto', [distApp, stagingApp], { stdio: 'inherit' });
  overlayResources(sourceApp, stagingApp);
  execFileSync('xattr', ['-cr', stagingApp], { stdio: 'inherit' });
  execFileSync('codesign', signArgs(stagingApp), { stdio: 'inherit' });

  fs.rmSync(desktopApp, { recursive: true, force: true });
  execFileSync('ditto', [stagingApp, desktopApp], { stdio: 'inherit' });
  execFileSync('xattr', ['-cr', desktopApp], { stdio: 'inherit' });
  execFileSync('codesign', signArgs(desktopApp), { stdio: 'inherit' });
  verifyBackendMirror(sourceApp, desktopApp);
  verifyFrontendMirror(sourceApp, desktopApp);
} catch (error) {
  fail(error.message);
} finally {
  fs.rmSync(stagingRoot, { recursive: true, force: true });
}

console.log(`[sync_desktop_app] synced ${desktopApp}`);
