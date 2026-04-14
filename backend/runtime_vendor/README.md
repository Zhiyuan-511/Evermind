Bundled browser runtimes used by Evermind-generated exports.

- `three/three.min.js`: Three.js 0.152.2 UMD build for global `THREE` browser usage
- `three/three.module.js`: Three.js 0.152.2 module build for `import * as THREE ...`
- `phaser/phaser.min.js`: Phaser 3.80.1 browser build
- `howler/howler.min.js`: Howler 2.2.4 browser build

These files are copied into generated output folders under `./_evermind_runtime/`
so exported projects can run without remote engine CDNs.
