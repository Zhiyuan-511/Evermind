// v5.8.6: sign with hardened runtime + entitlements so the Python backend
// subprocess inherits the Electron parent's TCC grants. Prior script used
// plain `codesign -s -` without --options=runtime, which made TCC treat
// every rebuild as a new app and re-prompt for file access every run.
//
// Preference order for signing identity:
//   1. env EVERMIND_CODESIGN_IDENTITY (e.g. "Evermind Local Dev") — produces
//      a stable designated requirement so TCC persists grants across rebuilds.
//   2. "-" (ad-hoc) — still works for local dev, but TCC may prompt on each
//      rebuild. Hardened runtime + entitlements + usage-description keys
//      reduce the scope of prompts.
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const electronDir = path.resolve(__dirname, '..');
const distApp = path.join(electronDir, 'dist', 'mac-arm64', 'Evermind.app');
const entitlements = path.join(electronDir, 'build', 'entitlements.mac.plist');

function fail(message) {
  console.error(`[ad_hoc_sign_app] ${message}`);
  process.exit(1);
}

if (!fs.existsSync(distApp)) {
  fail(`App bundle not found: ${distApp}`);
}

if (!fs.existsSync(entitlements)) {
  fail(`Entitlements file not found: ${entitlements}`);
}

try {
  execFileSync('xattr', ['-cr', distApp], { stdio: 'inherit' });
} catch (error) {
  console.warn(`[ad_hoc_sign_app] xattr cleanup skipped: ${error.message}`);
}

const identity = process.env.EVERMIND_CODESIGN_IDENTITY || '-';
const label = identity === '-' ? 'ad-hoc' : identity;
console.log(`[ad_hoc_sign_app] signing with identity=${label}, hardened runtime + entitlements`);

try {
  execFileSync(
    'codesign',
    [
      '--force',
      '--deep',
      '--timestamp=none',
      '--options=runtime',
      '--entitlements', entitlements,
      '-s', identity,
      distApp,
    ],
    { stdio: 'inherit' }
  );
} catch (error) {
  fail(`codesign failed: ${error.message}`);
}

// v5.8.6: verify the signature sticks + TCC-friendly flags are set.
try {
  execFileSync(
    'codesign',
    ['--verify', '--deep', '--strict', '--verbose=2', distApp],
    { stdio: 'inherit' }
  );
} catch (error) {
  console.warn(`[ad_hoc_sign_app] verify warning: ${error.message}`);
}

console.log(`[ad_hoc_sign_app] signed ${distApp}`);
if (identity === '-') {
  console.log(
    '[ad_hoc_sign_app] TIP: set EVERMIND_CODESIGN_IDENTITY="Evermind Local Dev" '
    + '(a self-signed cert from Keychain Access) to get a stable designated '
    + 'requirement — TCC will then persist grants across rebuilds, eliminating '
    + 'the per-run permission prompts.'
  );
}
