const fs = require('fs');
const path = require('path');

const electronDir = path.resolve(__dirname, '..');
const repoRoot = path.resolve(electronDir, '..');
const frontendRoot = path.join(repoRoot, 'frontend');
let standaloneRoot = path.join(frontendRoot, '.next', 'standalone');
let standaloneServer = path.join(standaloneRoot, 'server.js');

// Next.js 16 + Turbopack may place the standalone output in a subdirectory
// named after the project when it detects multiple lockfiles / workspace root.
if (!fs.existsSync(standaloneServer)) {
    // Search for server.js in immediate subdirectories
    const candidates = fs.existsSync(standaloneRoot)
        ? fs.readdirSync(standaloneRoot, { withFileTypes: true })
            .filter(e => e.isDirectory())
            .map(e => path.join(standaloneRoot, e.name))
        : [];
    for (const subdir of candidates) {
        const candidate = path.join(subdir, 'server.js');
        if (fs.existsSync(candidate)) {
            console.log(`[prepare_frontend_bundle] Found standalone in subdirectory: ${subdir}`);
            standaloneRoot = subdir;
            standaloneServer = candidate;
            break;
        }
    }
}

const staticRoot = path.join(frontendRoot, '.next', 'static');
const publicRoot = path.join(frontendRoot, 'public');
const bundleRoot = path.join(electronDir, '.packaged', 'frontend-standalone');

function fail(message) {
  console.error(`[prepare_frontend_bundle] ${message}`);
  process.exit(1);
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function removeDir(target) {
  fs.rmSync(target, { recursive: true, force: true });
}

function copyDirContents(fromDir, toDir) {
  ensureDir(toDir);
  for (const entry of fs.readdirSync(fromDir, { withFileTypes: true })) {
    const src = path.join(fromDir, entry.name);
    const dst = path.join(toDir, entry.name);
    if (entry.isDirectory()) {
      copyDir(src, dst);
    } else if (entry.isSymbolicLink()) {
      const real = fs.realpathSync(src);
      const stat = fs.statSync(real);
      if (stat.isDirectory()) {
        copyDir(real, dst);
      } else {
        ensureDir(path.dirname(dst));
        fs.copyFileSync(real, dst);
      }
    } else {
      ensureDir(path.dirname(dst));
      fs.copyFileSync(src, dst);
    }
  }
}

function copyDir(fromDir, toDir) {
  ensureDir(toDir);
  copyDirContents(fromDir, toDir);
}

if (!fs.existsSync(standaloneServer)) {
  fail(
    `Missing Next standalone server at ${standaloneServer}. ` +
    `Run "cd ${frontendRoot} && npm run build" first.`,
  );
}

removeDir(bundleRoot);
ensureDir(bundleRoot);

copyDirContents(standaloneRoot, bundleRoot);

if (fs.existsSync(staticRoot)) {
  copyDir(staticRoot, path.join(bundleRoot, '.next', 'static'));
}

if (fs.existsSync(publicRoot)) {
  copyDir(publicRoot, path.join(bundleRoot, 'public'));
}

const bundledServer = path.join(bundleRoot, 'server.js');
if (!fs.existsSync(bundledServer)) {
  fail(`Bundled standalone server missing after copy: ${bundledServer}`);
}

console.log(`[prepare_frontend_bundle] frontend bundle ready at ${bundleRoot}`);
