const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const electronDir = path.resolve(__dirname, '..');
const distApp = path.join(electronDir, 'dist', 'mac-arm64', 'Evermind.app');
const desktopApp = path.join(os.homedir(), 'Desktop', 'Evermind.app');
const expectedFrontendServer = path.join(distApp, 'Contents', 'Resources', 'frontend-standalone', 'server.js');

function fail(message) {
  console.error(`[sync_desktop_app] ${message}`);
  process.exit(1);
}

if (!fs.existsSync(distApp)) {
  fail(`Packaged app missing: ${distApp}`);
}

if (!fs.existsSync(expectedFrontendServer)) {
  fail(`Packaged frontend bundle missing: ${expectedFrontendServer}`);
}

const stagingRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'evermind-sync-'));
const stagingApp = path.join(stagingRoot, 'Evermind.app');

try {
  execFileSync('ditto', [distApp, stagingApp], { stdio: 'inherit' });
  execFileSync('xattr', ['-cr', stagingApp], { stdio: 'inherit' });
  execFileSync('codesign', ['--force', '--deep', '-s', '-', stagingApp], { stdio: 'inherit' });

  fs.rmSync(desktopApp, { recursive: true, force: true });
  execFileSync('ditto', [stagingApp, desktopApp], { stdio: 'inherit' });
  execFileSync('xattr', ['-cr', desktopApp], { stdio: 'inherit' });
  execFileSync('codesign', ['--force', '--deep', '-s', '-', desktopApp], { stdio: 'inherit' });
} catch (error) {
  fail(error.message);
} finally {
  fs.rmSync(stagingRoot, { recursive: true, force: true });
}

console.log(`[sync_desktop_app] synced ${desktopApp}`);
