from pathlib import Path



ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "backend"
LOCAL_APP_BACKEND_DIR = ROOT_DIR / "Evermind.app" / "Contents" / "Resources" / "backend"
PREPARED_FRONTEND_DIR = ROOT_DIR / "electron" / ".packaged" / "frontend-standalone"
LOCAL_APP_FRONTEND_DIR = ROOT_DIR / "Evermind.app" / "Contents" / "Resources" / "frontend-standalone"

CRITICAL_BACKEND_MIRRORS = [
    "ai_bridge.py",
    "html_postprocess.py",
    "orchestrator.py",
    "plugins/implementations.py",
    "preview_validation.py",
    "proxy_relay.py",
    "release_doctor.py",
    "repo_map.py",
    "scripts/desktop_run_goal_monitor.py",
    "scripts/release_doctor.py",
    "server.py",
    "task_classifier.py",
    "runtime_vendor/three/three.min.js",
    "runtime_vendor/phaser/phaser.min.js",
    "runtime_vendor/howler/howler.min.js",
    "workflow_templates.py",
]

CRITICAL_FRONTEND_MIRRORS = [
    "server.js",
    ".next/BUILD_ID",
    ".next/required-server-files.json",
]


def test_local_app_backend_mirror_matches_source():
    assert LOCAL_APP_BACKEND_DIR.exists(), (
        f"Local app backend mirror missing: {LOCAL_APP_BACKEND_DIR}"
    )

    mismatches = []
    missing = []

    for rel_path in CRITICAL_BACKEND_MIRRORS:
        source_path = BACKEND_DIR / rel_path
        mirror_path = LOCAL_APP_BACKEND_DIR / rel_path
        if not mirror_path.exists():
            missing.append(rel_path)
            continue
        if source_path.read_bytes() != mirror_path.read_bytes():
            mismatches.append(rel_path)

    assert not missing and not mismatches, (
        "Local Evermind.app backend resources drifted from source. "
        "Run `npm --prefix electron run sync:local-app`.\n"
        f"Missing: {missing}\n"
        f"Mismatched: {mismatches}"
    )


def test_local_app_frontend_bundle_matches_prepared_bundle():
    assert PREPARED_FRONTEND_DIR.exists(), (
        f"Prepared frontend bundle missing: {PREPARED_FRONTEND_DIR}"
    )
    assert LOCAL_APP_FRONTEND_DIR.exists(), (
        f"Local app frontend bundle missing: {LOCAL_APP_FRONTEND_DIR}"
    )

    mismatches = []
    missing = []

    for rel_path in CRITICAL_FRONTEND_MIRRORS:
        source_path = PREPARED_FRONTEND_DIR / rel_path
        mirror_path = LOCAL_APP_FRONTEND_DIR / rel_path
        if not mirror_path.exists():
            missing.append(rel_path)
            continue
        if source_path.read_bytes() != mirror_path.read_bytes():
            mismatches.append(rel_path)

    assert not missing and not mismatches, (
        "Local Evermind.app frontend resources drifted from the prepared bundle. "
        "Run `npm --prefix electron run sync:local-app`.\n"
        f"Missing: {missing}\n"
        f"Mismatched: {mismatches}"
    )
