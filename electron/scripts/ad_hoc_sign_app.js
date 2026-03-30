const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const electronDir = path.resolve(__dirname, '..');
const distApp = path.join(electronDir, 'dist', 'mac-arm64', 'Evermind.app');

function fail(message) {
  console.error(`[ad_hoc_sign_app] ${message}`);
  process.exit(1);
}

if (!fs.existsSync(distApp)) {
  fail(`App bundle not found: ${distApp}`);
}

try {
  execFileSync('xattr', ['-cr', distApp], { stdio: 'inherit' });
} catch (error) {
  console.warn(`[ad_hoc_sign_app] xattr cleanup skipped: ${error.message}`);
}

try {
  execFileSync('codesign', ['--force', '--deep', '-s', '-', distApp], { stdio: 'inherit' });
} catch (error) {
  fail(`codesign failed: ${error.message}`);
}

console.log(`[ad_hoc_sign_app] signed ${distApp}`);
