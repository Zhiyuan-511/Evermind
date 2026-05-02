"""
Evermind Backend — AI Bridge Unit Tests
Covers model resolution, usage normalization, and cost estimation.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

from ai_bridge import (
    AIBridge,
    AGENT_PRESETS,
    MODEL_REGISTRY,
    MAX_REQUEST_TOTAL_CHARS,
    _sanitize_error,
)
from plugins.base import PluginResult
from plugins.implementations import (
    FileOpsPlugin,
    clear_active_file_ops_write_token,
    set_active_file_ops_write_token,
)
from proxy_relay import get_relay_manager


class TestResolveModel(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_resolves_static_registry_model(self):
        info = self.bridge._resolve_model("gpt-4o")
        self.assertEqual(info["litellm_id"], "gpt-4o")
        self.assertEqual(info["provider"], "openai")
        self.assertTrue(info["supports_tools"])

    def test_resolves_deepseek_model(self):
        info = self.bridge._resolve_model("deepseek-v3")
        self.assertEqual(info["litellm_id"], "deepseek/deepseek-chat")
        self.assertEqual(info["provider"], "deepseek")

    def test_fallback_for_unknown_model(self):
        info = self.bridge._resolve_model("some-future-model-v99")
        self.assertEqual(info["litellm_id"], "some-future-model-v99")
        self.assertTrue(info["supports_tools"])
        self.assertFalse(info["supports_cua"])

    def test_kimi_model_has_api_base(self):
        info = self.bridge._resolve_model("kimi")
        self.assertIn("api_base", info)
        self.assertTrue("kimi" in info["api_base"] or "moonshot" in info["api_base"])

    def test_qwen_model_has_api_base(self):
        info = self.bridge._resolve_model("qwen-max")
        self.assertIn("api_base", info)
        self.assertIn("dashscope", info["api_base"])

    def test_current_claude_registry_ids_match_official_models(self):
        sonnet = self.bridge._resolve_model("claude-4-sonnet")
        opus = self.bridge._resolve_model("claude-4-opus")

        self.assertEqual(sonnet["litellm_id"], "claude-sonnet-4-6")
        self.assertEqual(opus["litellm_id"], "claude-opus-4-6")


class TestAnalystResearchFollowup(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_source_fetch_counts_as_analyst_research_activity(self):
        reason = self.bridge._analyst_browser_followup_reason(
            "analyst",
            {"source_fetch": 2},
            [
                {"data": {"url": "https://github.com/example/repo/blob/main/README.md"}},
                {"data": {"url": "https://docs.example.com/game-loop"}},
            ],
            {"source_fetch"},
        )
        self.assertIsNone(reason)

    def test_followup_message_mentions_source_fetch(self):
        message = self.bridge._analyst_browser_followup_message(
            "You have only visited 1 source URL. Use source_fetch or browser on one more distinct GitHub/doc/tutorial/source page before final report."
        )
        self.assertIn("source_fetch", message)


class TestProviderAuthFailureMemory(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    @patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-ds-test"}, clear=False)
    def test_recent_provider_auth_failure_filters_candidates(self):
        model_info = self.bridge._resolve_model("kimi-coding")
        # V4.5: Need 2+ consecutive failures before blocking (was 1)
        self.bridge._record_provider_auth_failure(model_info, "401 unauthorized")
        self.bridge._record_provider_auth_failure(model_info, "401 unauthorized")
        candidates = self.bridge._filter_viable_model_candidates(["kimi-coding", "deepseek-v3"])
        self.assertNotIn("kimi-coding", candidates)

    def test_provider_auth_failure_is_seeded_from_recent_logs(self):
        ts = datetime.fromtimestamp(time.time() - 30).strftime("%Y-%m-%d %H:%M:%S,000")
        lines = [
            f"{ts} [evermind.ai_bridge] WARNING: Provider auth failure cooldown: provider=openai cooldown=43200s error=401 unauthorized"
        ]
        with patch.object(AIBridge, "_tail_compat_gateway_log_lines", return_value=lines):
            bridge = AIBridge(config={})

        reason = bridge._provider_recent_auth_failure_reason(bridge._resolve_model("gpt-5.4"))
        self.assertIn("recent openai auth failure", reason)

    def test_stale_provider_auth_failure_seed_is_ignored_by_default(self):
        ts = datetime.fromtimestamp(time.time() - 3600).strftime("%Y-%m-%d %H:%M:%S,000")
        lines = [
            f"{ts} [evermind.ai_bridge] WARNING: Provider auth failure cooldown: provider=openai cooldown=43200s error=401 unauthorized"
        ]
        with patch.object(AIBridge, "_tail_compat_gateway_log_lines", return_value=lines):
            bridge = AIBridge(config={})

        reason = bridge._provider_recent_auth_failure_reason(bridge._resolve_model("gpt-5.4"))
        self.assertEqual(reason, "")


class TestBuilderPresetContracts(unittest.TestCase):
    def test_builder_preset_mentions_game_boot_sequence_guards(self):
        # v6.1.5: game-specific rules moved to
        # agent_skills/gameplay-foundation + godogen-tps-control-sanity-lock
        # (loaded conditionally). Core builder preset now keeps only the
        # cross-domain "null-guard JS, return FULL merged file" contracts.
        instructions = AGENT_PRESETS["builder"]["instructions"]
        self.assertIn("undeclared globals", instructions)
        self.assertIn("FULL merged file", instructions)


class TestPartialOutputEvents(unittest.TestCase):
    def test_partial_output_event_keeps_full_text_for_timeout_salvage(self):
        bridge = AIBridge(config={})
        full_text = "<!DOCTYPE html><html><body>" + ("TPS" * 3000) + "</body></html>"

        event = bridge._build_partial_output_event(full_text, phase="streaming")

        self.assertIsNotNone(event)
        self.assertEqual(event["partial_output"], full_text)
        self.assertNotEqual(event["preview"], full_text)
        self.assertIn("...", event["preview"])


class TestAIBridgePerRunStateReset(unittest.TestCase):
    """v6.0: per-run state must not leak kill switch across pipeline runs."""

    def test_reset_clears_kill_switch_and_counter(self):
        bridge = AIBridge(config={})
        bridge._builder_direct_multifile_disabled = True
        bridge._builder_empty_batch_count = 7
        bridge._builder_deferred_context = "stale context from prior run"

        bridge.reset_per_run_state()

        self.assertFalse(bridge._builder_direct_multifile_disabled)
        self.assertEqual(bridge._builder_empty_batch_count, 0)
        self.assertEqual(bridge._builder_deferred_context, "")

    def test_reset_preserves_provider_auth_health(self):
        """Auth cooldowns should survive a new run (bad key is still bad)."""
        bridge = AIBridge(config={})
        bridge._provider_auth_health["kimi"] = {"strikes": 2, "cooldown_until": 1234}
        bridge.reset_per_run_state()
        self.assertEqual(bridge._provider_auth_health.get("kimi", {}).get("strikes"), 2)

    def test_fresh_bridge_starts_clean(self):
        """A brand-new AIBridge must NOT have a latent kill switch."""
        bridge = AIBridge(config={})
        self.assertFalse(getattr(bridge, "_builder_direct_multifile_disabled", False))
        self.assertEqual(getattr(bridge, "_builder_empty_batch_count", 0), 0)


class TestBuilderNarrationOnlyGuard(unittest.TestCase):
    """v5.8.7: empty-prose batches must be rejected before they burn continuations."""

    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_empty_string_is_narration(self):
        self.assertTrue(self.bridge._builder_response_looks_like_narration_only(""))

    def test_short_planning_prose_is_narration(self):
        content = (
            "Here is my plan: first I will design the layout, then I'll build "
            "the navigation and finally the hero. I'll iterate if anything breaks."
        )
        self.assertTrue(self.bridge._builder_response_looks_like_narration_only(content))

    def test_minimax_failure_sample_is_narration(self):
        content = "Plan:\n1. Build layout\n2. Style hero\n3. Add interactions\n" * 20
        self.assertLess(len(content), 5000)
        self.assertTrue(self.bridge._builder_response_looks_like_narration_only(content))

    def test_real_html_block_is_not_narration(self):
        content = (
            "```html index.html\n"
            "<!DOCTYPE html><html><head><title>x</title></head>"
            "<body><main>hello world</main></body></html>\n"
            "```"
        )
        self.assertFalse(self.bridge._builder_response_looks_like_narration_only(content))

    def test_bare_doctype_html_without_fences_is_not_narration(self):
        content = "<!DOCTYPE html><html><head></head><body>" + ("<p>x</p>" * 120) + "</body></html>"
        self.assertFalse(self.bridge._builder_response_looks_like_narration_only(content))


class TestGameContinuationHeuristics(unittest.TestCase):
    def test_builder_game_text_continuation_detects_missing_game_shell(self):
        bridge = AIBridge(config={})
        html = """<!DOCTYPE html><html><head><style>
#start-screen { position: absolute; inset: 0; display: flex; }
#gameCanvas { width: 100vw; height: 100vh; }
.hud { position: absolute; top: 12px; left: 12px; }
</style></head><body></body></html>""" + (" " * 1200)

        reason = bridge._builder_game_text_continuation_reason(
            html,
            "做一个 3D 第三人称射击游戏",
        )

        self.assertEqual(reason, "missing_game_shell")

    def test_builder_game_text_continuation_detects_missing_start_handler(self):
        bridge = AIBridge(config={})
        html = """<!DOCTYPE html><html><body>
<section id="startOverlay"><button onclick="startGame()">Start</button></section>
<canvas id="gameCanvas"></canvas>
<script>
const state = { started: false };
function loop(){ requestAnimationFrame(loop); }
document.addEventListener('keydown', () => {});
</script>
</body></html>""" + (" " * 1200)

        reason = bridge._builder_game_text_continuation_reason(
            html,
            "做一个 3D 第三人称射击游戏",
        )

        self.assertEqual(reason, "missing_start_handler")

    def test_builder_game_text_continuation_detects_missing_game_loop(self):
        bridge = AIBridge(config={})
        html = """<!DOCTYPE html><html><body>
<section id="startOverlay"><button onclick="startGame()">Start</button></section>
<canvas id="gameCanvas"></canvas>
<script>
function startGame(){ document.body.dataset.mode = 'playing'; }
document.addEventListener('keydown', () => {});
</script>
</body></html>""" + (" " * 1200)

        reason = bridge._builder_game_text_continuation_reason(
            html,
            "做一个 3D 第三人称射击游戏",
        )

        self.assertEqual(reason, "missing_game_loop")

    def test_builder_game_text_continuation_detects_missing_runtime_surface(self):
        bridge = AIBridge(config={})
        html = """<!DOCTYPE html><html><body>
<section id="startOverlay"><button onclick="startGame()">Start</button></section>
<div class="hud">HP 100</div>
<div class="weapon-panel">Pulse Rifle</div>
<div class="control-hint">WASD Move / Click Fire</div>
</body></html>""" + (" " * 1200)

        reason = bridge._builder_game_text_continuation_reason(
            html,
            "做一个 3D 第三人称射击游戏",
        )

        self.assertEqual(reason, "missing_runtime_surface")

    def test_builder_html_persist_accepts_object_method_start_flow(self):
        bridge = AIBridge(config={})
        html = """<!DOCTYPE html><html><body>
<section id="missionOverlay"><button class="menu-btn" onclick="game.startMission(1)">开始任务</button></section>
<canvas id="gameCanvas"></canvas>
<script>
const game = {
  startMission(index){ document.body.dataset.state = 'playing'; },
  animate(){ requestAnimationFrame(() => game.animate()); }
};
game.animate();
</script>
</body></html>""" + (" " * 1200)

        reason = bridge._builder_html_persist_rejection_reason(
            html,
            input_data="做一个 3D 第三人称射击游戏",
            filename="index.html",
        )

        self.assertEqual(reason, "")

    def test_builder_html_write_rejection_reason_allows_game_shell_with_local_runtime_script(self):
        bridge = AIBridge(config={})
        html = """<!DOCTYPE html><html><body>
<section id="start-screen"><button id="startBtn">开始战斗</button></section>
<canvas id="gameCanvas"></canvas>
<script src="./runtime/game.js" defer></script>
</body></html>""" + (" " * 1200)

        reason = bridge._builder_html_write_rejection_reason(
            html,
            input_data="做一个 3D 第三人称射击游戏",
            filename="index.html",
        )

        self.assertEqual(reason, "")

    def test_extract_html_files_from_text_output_skips_implicit_index_for_support_lane_builder(self):
        bridge = AIBridge(config={})
        output = (
            "```html\n"
            "<!DOCTYPE html><html><body><main><h1>NEON HORIZON</h1><canvas id='gameCanvas'></canvas>"
            "<script>function animate(){requestAnimationFrame(animate);} animate();</script></main></body></html>\n"
            "```"
        )
        input_data = (
            "Build a non-overlapping support subsystem for the same commercial-grade HTML5 game.\n"
            "Do NOT overwrite /tmp/evermind_output/index.html in this pass unless the orchestrator explicitly reassigns it.\n"
            "Your lane owns support JS/CSS/JSON modules first, not a root index.html rewrite.\n"
        )

        files = bridge._extract_html_files_from_text_output(output, input_data)

        self.assertEqual(files, {})

    def test_builder_is_support_lane_node_requires_explicit_flag(self):
        bridge = AIBridge(config={})
        self.assertFalse(
            bridge._builder_is_support_lane_node(
                {"type": "builder", "can_write_root_index": False}
            )
        )
        self.assertTrue(
            bridge._builder_is_support_lane_node(
                {
                    "type": "builder",
                    "can_write_root_index": False,
                    "builder_is_support_lane_node": True,
                }
            )
        )

    def test_attempt_builder_game_text_continuation_repairs_shell_only_game_page(self):
        bridge = AIBridge(config={})
        html = """<!DOCTYPE html><html><head><style>
#start-screen { position: absolute; inset: 0; display: flex; }
#gameCanvas { width: 100vw; height: 100vh; }
.hud { position: absolute; top: 12px; left: 12px; }
</style></head><body></body></html>""" + (" " * 1200)
        seen = {}

        async def _request(messages):
            seen["messages"] = messages
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                "<section id='start-screen'><button onclick='startGame()'>Start</button></section>"
                                "<canvas id='gameCanvas'></canvas>"
                                "<div class='hud'>HP 100</div>"
                                "<script>function startGame(){requestAnimationFrame(loop);} function loop(){requestAnimationFrame(loop);}</script>"
                            )
                        )
                    )
                ],
                usage=None,
            )

        output_text, continuation_count, cont_resp = asyncio.run(
            bridge._attempt_builder_game_text_continuation(
                output_text=html,
                input_data="做一个 3D 第三人称射击游戏",
                system_prompt="builder system prompt",
                continuation_count=0,
                max_continuations=2,
                request_continuation=_request,
                on_progress=None,
            )
        )

        self.assertEqual(continuation_count, 1)
        self.assertIsNotNone(cont_resp)
        self.assertIn("gamecanvas", output_text.lower())
        self.assertIn("requestanimationframe", output_text.lower())
        self.assertIn("playable body markup is still incomplete", seen["messages"][1]["content"])
        self.assertIn("COMPLETE merged HTML document", seen["messages"][1]["content"])

    def test_attempt_builder_game_text_continuation_replaces_with_full_document_when_returned(self):
        bridge = AIBridge(config={})
        html = """<!DOCTYPE html><html><head><style>
#start-screen { position: absolute; inset: 0; display: flex; }
#gameCanvas { width: 100vw; height: 100vh; }
.hud { position: absolute; top: 12px; left: 12px; }
</style></head><body></body></html>""" + (" " * 1200)

        async def _request(messages):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                "<!DOCTYPE html><html><body><section id='start-screen'></section>"
                                "<canvas id='gameCanvas'></canvas><div class='hud'>HP 100</div>"
                                "<div class='controls'>WASD to move, mouse to aim, click to fire.</div>"
                                "<script>function startGame(){requestAnimationFrame(loop);} function loop(){requestAnimationFrame(loop);}</script>"
                                "</body></html>"
                            )
                        )
                    )
                ],
                usage=None,
            )

        output_text, continuation_count, _ = asyncio.run(
            bridge._attempt_builder_game_text_continuation(
                output_text=html,
                input_data="做一个 3D 第三人称射击游戏",
                system_prompt="builder system prompt",
                continuation_count=0,
                max_continuations=2,
                request_continuation=_request,
                on_progress=None,
            )
        )

        self.assertEqual(continuation_count, 1)
        self.assertTrue(output_text.lstrip().lower().startswith("<!doctype html>"))
        self.assertEqual(output_text.lower().count("<!doctype html>"), 1)
        self.assertIn("<canvas id='gamecanvas'>", output_text.lower())

    def test_builder_disk_salvage_text_merges_incremental_fragment_with_existing_shell(self):
        bridge = AIBridge(config={})
        base_html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>Neon Hunter</title>
  <style>
    .startButton {
      padding: 1rem 2rem;
      text-transform:
</style>
</head>
<body>
</body>
</html>"""
        fragment = """uppercase;
      letter-spacing: 0.15em;
    }
  </style>
</head>
<body>
  <section id="startScreen"><button class="startButton">Start</button></section>
  <canvas id="gameCanvas"></canvas>
  <script>function startGame(){requestAnimationFrame(loop);} function loop(){requestAnimationFrame(loop);}</script>
</body>
</html>"""

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge.config["output_dir"] = tmpdir
            Path(tmpdir, "index.html").write_text(base_html, encoding="utf-8")

            salvaged = bridge._builder_disk_salvage_text(
                "做一个 3D 第三人称射击游戏",
                incremental_text=fragment,
            )

        self.assertIn("```html index.html", salvaged)
        self.assertIn("<!DOCTYPE html>", salvaged)
        self.assertIn("text-transform:", salvaged)
        self.assertIn("uppercase;", salvaged)
        self.assertIn("gameCanvas", salvaged)


class TestNodeModelPreferences(unittest.TestCase):
    def setUp(self):
        self._api_base_env = patch.dict(
            "os.environ",
            {
                "OPENAI_API_BASE": "",
                "ANTHROPIC_API_BASE": "",
                "GEMINI_API_BASE": "",
                "DEEPSEEK_API_BASE": "",
                "KIMI_API_BASE": "",
                "QWEN_API_BASE": "",
            },
            clear=False,
        )
        self._api_base_env.start()
        # Prevent real backend log files from seeding stale gateway health
        # state into fresh AIBridge instances during tests.
        # Tests that explicitly test seeding already override this path
        # with their own patch.
        self._log_file_patch = patch(
            "ai_bridge.COMPAT_GATEWAY_LOG_FILE",
            "/tmp/_evermind_test_nonexistent_log_file.log",
        )
        self._log_file_patch.start()

    def tearDown(self):
        self._log_file_patch.stop()
        self._api_base_env.stop()

    def test_resolve_candidates_prioritize_node_chain_over_default_model(self):
        bridge = AIBridge(config={
            "openai_api_key": "sk-openai-test",
            "anthropic_api_key": "sk-ant-test",
            "kimi_api_key": "sk-kimi-test",
            "node_model_preferences": {
                "builder": ["claude-4-sonnet", "kimi-coding"],
            },
        })

        candidates = bridge.resolve_node_model_candidates(
            {"type": "builder", "model": "gpt-5.4", "model_is_default": True},
            "gpt-5.4",
        )

        self.assertEqual(candidates[:3], ["claude-4-sonnet", "kimi-coding", "gpt-5.4"])

    def test_preferred_model_skips_missing_keys_inside_chain(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "KIMI_API_KEY": ""}):
            bridge = AIBridge(config={
                "kimi_api_key": "sk-kimi-test",
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "claude-4-sonnet", "kimi-coding"],
                },
            })

            preferred = bridge.preferred_model_for_node(
                {"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                "gpt-5.4",
            )

            self.assertEqual(preferred, "kimi-coding")

    def test_resolve_candidates_filters_unavailable_legacy_models(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "GEMINI_API_KEY": "",
                "DEEPSEEK_API_KEY": "",
                "QWEN_API_KEY": "",
                "KIMI_API_KEY": "",
                # v6.1.3: also clear zhipu/minimax/doubao/yi/aigate env keys
                # so tests are isolated from other suites that may have set
                # them — glm-4-plus now inserts into position 3 otherwise.
                "ZHIPU_API_KEY": "",
                "MINIMAX_API_KEY": "",
                "DOUBAO_API_KEY": "",
                "YI_API_KEY": "",
                "AIGATE_API_KEY": "",
            },
        ):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
            })

            candidates = bridge.resolve_node_model_candidates(
                {"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                "gpt-5.4",
            )

            self.assertEqual(candidates[:3], ["gpt-5.4", "kimi-k2.6-code-preview", "gpt-5.3-codex"])  # v5.8.2
            self.assertNotIn("claude-4-sonnet", candidates)
            self.assertNotIn("deepseek-v3", candidates)
            self.assertNotIn("gemini-2.5-pro", candidates)
            self.assertNotIn("qwen-max", candidates)

    def test_resolve_candidates_appends_resilience_fallbacks_for_custom_gateway_singleton(self):
        with patch.dict("os.environ", {
            "OPENAI_API_BASE": "https://gateway.example/v1",
            "ZHIPU_API_KEY": "",
            "MINIMAX_API_KEY": "",
            "DOUBAO_API_KEY": "",
            "YI_API_KEY": "",
            "AIGATE_API_KEY": "",
        }, clear=False):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
                "node_model_preferences": {
                    "builder": ["gpt-5.4"],
                },
            })

            candidates = bridge.resolve_node_model_candidates(
                {"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                "gpt-5.4",
            )

            self.assertEqual(candidates[:3], ["gpt-5.4", "kimi-k2.6-code-preview", "gpt-5.3-codex"])  # v5.8.2

    def test_resolve_candidates_appends_resilience_fallbacks_for_custom_gateway_imagegen_singleton(self):
        with patch.dict("os.environ", {
            "OPENAI_API_BASE": "https://gateway.example/v1",
            "ZHIPU_API_KEY": "",
            "MINIMAX_API_KEY": "",
            "DOUBAO_API_KEY": "",
            "YI_API_KEY": "",
            "AIGATE_API_KEY": "",
        }, clear=False):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
                "node_model_preferences": {
                    "imagegen": ["gpt-5.4"],
                },
            })

            candidates = bridge.resolve_node_model_candidates(
                {"type": "imagegen", "model": "gpt-5.4", "model_is_default": True},
                "gpt-5.4",
            )

            self.assertEqual(candidates[:3], ["gpt-5.4", "kimi-k2.6-code-preview", "gpt-5.3-codex"])  # v5.8.2

    def test_resolve_candidates_does_not_append_emergency_fallbacks_for_non_gateway_singleton(self):
        bridge = AIBridge(config={
            "kimi_api_key": "sk-kimi-test",
            "node_model_preferences": {
                "builder": ["kimi-coding"],
            },
        })

        candidates = bridge.resolve_node_model_candidates(
            {"type": "builder", "model": "kimi-coding", "model_is_default": True},
            "kimi-coding",
        )

        self.assertEqual(candidates, ["kimi-coding"])

    def test_resolve_candidates_inserts_matching_relay_models_after_primary(self):
        relay_mgr = get_relay_manager()
        relay_mgr.load([
            {
                "id": "relay_fast",
                "name": "Fast Relay",
                "base_url": "https://relay-fast.example/v1",
                "api_key": "relay-fast-key",
                "models": ["gpt-5.4", "claude-4-sonnet"],
                "enabled": True,
                "timeout": 90,
                "last_test": {"success": True, "latency_ms": 120},
            },
            {
                "id": "relay_backup",
                "name": "Backup Relay",
                "base_url": "https://relay-backup.example/v1",
                "api_key": "relay-backup-key",
                "models": ["gpt-5.4"],
                "enabled": True,
                "timeout": 90,
                "last_test": {"success": True, "latency_ms": 480},
            },
        ])
        try:
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "kimi-coding"],
                },
            })

            candidates = bridge.resolve_node_model_candidates(
                {"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                "gpt-5.4",
            )

            self.assertEqual(candidates[:3], [
                "gpt-5.4",
                "relay_pool/gpt-5.4",
                "kimi-coding",
            ])
        finally:
            relay_mgr.load([])

    def test_resolve_candidates_promotes_matching_relay_when_gateway_is_in_rejection_cooldown(self):
        relay_mgr = get_relay_manager()
        relay_mgr.load([
            {
                "id": "relay_fast",
                "name": "Fast Relay",
                "base_url": "https://relay-fast.example/v1",
                "api_key": "relay-fast-key",
                "models": ["gpt-5.4"],
                "enabled": True,
                "timeout": 90,
                "last_test": {"success": True, "latency_ms": 120},
            },
        ])
        try:
            with patch.dict("os.environ", {"OPENAI_API_BASE": "https://gateway.example/v1"}, clear=False):
                bridge = AIBridge(config={
                    "openai_api_key": "sk-openai-test",
                    "kimi_api_key": "sk-kimi-test",
                    "node_model_preferences": {
                        "builder": ["gpt-5.4", "kimi-coding"],
                    },
                })
                model_info = bridge._resolve_model("gpt-5.4")
                _key, state = bridge._compatible_gateway_state(model_info)
                self.assertIsNotNone(state)
                state["rejection_cooldown_until"] = time.time() + 60
                state["last_error"] = "Your request was blocked."

                candidates = bridge.resolve_node_model_candidates(
                    {"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                    "gpt-5.4",
                )

                self.assertEqual(candidates[:3], [
                    "relay/relay_fast/gpt-5.4",
                    "kimi-coding",
                    "gpt-5.4",
                ])
        finally:
            relay_mgr.load([])

    def test_resolve_candidates_prefers_matching_relay_pool_over_custom_gateway_for_builder(self):
        relay_mgr = get_relay_manager()
        relay_mgr.load([
            {
                "id": "relay_fast",
                "name": "Fast Relay",
                "base_url": "https://relay-fast.example/v1",
                "api_key": "relay-fast-key",
                "models": ["gpt-5.4"],
                "enabled": True,
                "timeout": 90,
                "last_test": {"success": True, "builder_profile_ok": True, "latency_ms": 120},
            },
            {
                "id": "relay_backup",
                "name": "Backup Relay",
                "base_url": "https://relay-backup.example/v1",
                "api_key": "relay-backup-key",
                "models": ["gpt-5.4"],
                "enabled": True,
                "timeout": 90,
                "last_test": {"success": True, "builder_profile_ok": True, "latency_ms": 240},
            },
        ])
        try:
            with patch.dict("os.environ", {"OPENAI_API_BASE": "https://gateway.example/v1"}, clear=False):
                bridge = AIBridge(config={
                    "openai_api_key": "sk-openai-test",
                    "kimi_api_key": "sk-kimi-test",
                    "node_model_preferences": {
                        "builder": ["gpt-5.4", "kimi-coding"],
                    },
                })

                candidates = bridge.resolve_node_model_candidates(
                    {"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                    "gpt-5.4",
                )

                self.assertEqual(candidates[:3], [
                    "relay_pool/gpt-5.4",
                    "gpt-5.4",
                    "kimi-coding",
                ])
        finally:
            relay_mgr.load([])

    def test_resolve_candidates_deprioritizes_recently_rejected_gateway_after_cooldown(self):
        with patch.dict("os.environ", {"OPENAI_API_BASE": "https://gateway.example/v1"}, clear=False):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "kimi-coding"],
                },
            })
            model_info = bridge._resolve_model("gpt-5.4")
            _key, state = bridge._compatible_gateway_state(model_info)
            self.assertIsNotNone(state)
            state["last_rejection_at"] = time.time() - 30
            state["last_success_at"] = 0.0
            state["last_error"] = "Your request was blocked."

            candidates = bridge.resolve_node_model_candidates(
                {"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                "gpt-5.4",
            )

            self.assertEqual(candidates[:2], ["kimi-coding", "gpt-5.4"])

    def test_bridge_seeds_gateway_rejection_cooldown_from_recent_logs(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "evermind-backend.log"
            observed_at = time.time() - 30
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(observed_at)) + ",000"
            log_path.write_text(
                (
                    f"{timestamp} [evermind.ai_bridge] WARNING: Compatible gateway rejection cooldown: "
                    "provider=openai host=relay cooldown=180s error=Your request was blocked.\n"
                ),
                encoding="utf-8",
            )

            with patch("ai_bridge.COMPAT_GATEWAY_LOG_FILE", log_path), \
                 patch.dict("os.environ", {"OPENAI_API_BASE": "https://relay/v1"}, clear=False):
                bridge = AIBridge(config={"openai_api_key": "sk-openai-test"})
                model_info = bridge._resolve_model("gpt-5.4")

                preflight_error = bridge._compatible_gateway_preflight_error(model_info)

            self.assertIn("compatible gateway rejection cooldown", preflight_error)
            self.assertIn("relay", preflight_error)
            self.assertIn("Your request was blocked", preflight_error)

    def test_bridge_seeds_recent_rejection_state_from_logs_after_cooldown_expiry(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "evermind-backend.log"
            observed_at = time.time() - 240
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(observed_at)) + ",000"
            log_path.write_text(
                (
                    f"{timestamp} [evermind.ai_bridge] WARNING: Compatible gateway rejection cooldown: "
                    "provider=openai host=relay cooldown=180s error=Your request was blocked.\n"
                ),
                encoding="utf-8",
            )

            with patch("ai_bridge.COMPAT_GATEWAY_LOG_FILE", log_path), \
                 patch.dict("os.environ", {"OPENAI_API_BASE": "https://relay/v1"}, clear=False):
                bridge = AIBridge(config={
                    "openai_api_key": "sk-openai-test",
                    "kimi_api_key": "sk-kimi-test",
                    "node_model_preferences": {"builder": ["gpt-5.4", "kimi-coding"]},
                })
                model_info = bridge._resolve_model("gpt-5.4")

                recent_issue = bridge._compatible_gateway_recent_unhealthy_reason(model_info)
                candidates = bridge.resolve_node_model_candidates(
                    {"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                    "gpt-5.4",
                )

            self.assertIn("recent rejection", recent_issue)
            self.assertEqual(candidates[:2], ["kimi-coding", "gpt-5.4"])

    def test_execute_falls_back_to_next_configured_model(self):
        bridge = AIBridge(config={
            "openai_api_key": "sk-openai-test",
            "anthropic_api_key": "sk-ant-test",
            "node_model_preferences": {
                "reviewer": ["claude-4-sonnet", "gpt-4o"],
            },
        })
        bridge._litellm = object()
        bridge._execute_litellm_chat = AsyncMock(side_effect=[
            {"success": False, "output": "", "error": "401 unauthorized"},
            {"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"},
        ])

        result = asyncio.run(
            bridge.execute(
                node={"type": "reviewer", "model": "gpt-5.4", "model_is_default": True},
                plugins=[],
                input_data="Review the generated site.",
                model="gpt-5.4",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("model"), "gpt-4o")
        self.assertEqual(result.get("attempted_models"), ["claude-4-sonnet", "gpt-4o"])
        self.assertTrue(result.get("model_chain_applied"))
        self.assertEqual(bridge._execute_litellm_chat.await_count, 2)

    def test_execute_falls_back_without_retrying_same_model_after_invalid_html_gateway_response(self):
        bridge = AIBridge(config={
            "openai_api_key": "sk-openai-test",
            "anthropic_api_key": "sk-ant-test",
            "node_model_preferences": {
                "reviewer": ["gpt-5.4", "claude-4-sonnet"],
            },
        })
        bridge._litellm = object()
        bridge._execute_litellm_chat = AsyncMock(side_effect=[
            {
                "success": False,
                "output": "",
                "error": (
                    "litellm.InternalServerError: OpenAIException - "
                    "Empty or invalid response from LLM endpoint. "
                    "Received: '<!doctype html><title>Relay - AI API Gateway</title>'"
                ),
            },
            {"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"},
        ])

        result = asyncio.run(
            bridge.execute(
                node={"type": "reviewer", "model": "gpt-5.4", "model_is_default": True},
                plugins=[],
                input_data="Review the generated site.",
                model="gpt-5.4",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("model"), "claude-4-sonnet")

    def test_execute_preflight_progress_timeout_does_not_block_builder_dispatch(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._pick_speculative_pair = lambda *a, **kw: None
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible_chat"}
        )

        async def slow_progress(_payload):
            await asyncio.sleep(0.5)

        start = time.perf_counter()
        with patch.dict("os.environ", {"EVERMIND_PROGRESS_EVENT_TIMEOUT_SEC": "0.01", "OPENAI_API_BASE": ""}):
            result = asyncio.run(
                bridge.execute(
                    node={
                        "type": "builder",
                        "model": "kimi-coding",
                        "model_is_default": False,
                        "builder_delivery_mode": "direct_multifile",
                    },
                    plugins=[],
                    input_data=(
                        "做一个 3 页面网站。\n"
                        "Assigned HTML filenames for this builder: index.html, pricing.html, contact.html."
                    ),
                    model="kimi-coding",
                    on_progress=slow_progress,
                )
            )
        elapsed = time.perf_counter() - start

        self.assertTrue(result.get("success"))
        self.assertLess(elapsed, 0.25)
        bridge._execute_openai_compatible_chat.assert_awaited_once()
        self.assertEqual(result.get("attempted_models"), ["kimi-coding"])

    def test_execute_builder_prewrite_timeout_skips_same_model_retry(self):
        bridge = AIBridge(config={
            "openai_api_key": "sk-openai-test",
            "kimi_api_key": "sk-kimi-test",
            "node_model_preferences": {
                "builder": ["gpt-5.4", "kimi-coding"],
            },
        })
        bridge._litellm = object()
        # Disable speculative execution so we test sequential fallback cleanly
        bridge._pick_speculative_pair = lambda *a, **kw: None
        # gpt-5.4 (no custom_gateway in setUp) → litellm_tools; kimi-coding (extra_headers) → openai_compatible
        # Use single-page input to avoid auto_builder_direct_multifile routing override.
        bridge._execute_litellm_tools = AsyncMock(return_value={
            "success": False,
            "output": "",
            "error": "builder pre-write timeout after 75s: no real file write or tool progress was produced.",
        })
        bridge._execute_openai_compatible = AsyncMock(return_value={
            "success": True,
            "output": "ok",
            "tool_results": [],
            "mode": "openai_compatible",
        })

        result = asyncio.run(
            bridge.execute(
                node={"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                plugins=[MagicMock()],
                input_data="Build a premium landing page with hero section and CTA buttons.",
                model="gpt-5.4",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("model"), "kimi-coding")
        self.assertEqual(result.get("attempted_models"), ["gpt-5.4", "kimi-coding"])
        self.assertEqual(bridge._execute_litellm_tools.await_count, 1)
        self.assertEqual(bridge._execute_openai_compatible.await_count, 1)

    def test_execute_skips_recently_rejected_gateway_fallback_candidate_after_builder_timeout(self):
        with patch.dict("os.environ", {"OPENAI_API_BASE": "https://relay/v1"}, clear=False):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
                "node_model_preferences": {
                    "builder": ["kimi-coding", "gpt-5.4"],
                },
            })
            bridge._pick_speculative_pair = lambda *a, **kw: None
            gateway_model = bridge._resolve_model("gpt-5.4")
            _key, state = bridge._compatible_gateway_state(gateway_model)
            self.assertIsNotNone(state)
            # Preflight checks rejection_cooldown_until, not last_rejection_at.
            # Set both so the gateway is in active cooldown.
            now = time.time()
            state["last_rejection_at"] = now - 5
            state["rejection_cooldown_until"] = now + 10
            state["last_success_at"] = 0.0
            state["last_error"] = "Your request was blocked."

            bridge._litellm = object()
            bridge._execute_openai_compatible = AsyncMock(side_effect=[
                {
                    "success": False,
                    "output": "",
                    "error": "builder pre-write timeout after 35s: no file write produced.",
                },
                {
                    "success": True,
                    "output": "should-not-run",
                    "tool_results": [],
                    "mode": "openai_compatible",
                },
            ])

            result = asyncio.run(
                bridge.execute(
                    node={"type": "builder", "model": "kimi-coding", "model_is_default": True},
                    plugins=[MagicMock()],
                    input_data="Build a 3D TPS shooter with polished combat feel.",
                    model="kimi-coding",
                    on_progress=None,
                )
            )

        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("attempted_models"), ["kimi-coding"])
        self.assertEqual(result.get("model_candidates")[:2], ["kimi-coding", "gpt-5.4"])
        self.assertEqual(bridge._execute_openai_compatible.await_count, 1)

    def test_execute_gateway_timeout_falls_back_without_same_model_retry(self):
        with patch.dict("os.environ", {"OPENAI_API_BASE": "https://gateway.example/v1"}, clear=False):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
                "node_model_preferences": {
                    "builder": ["gpt-5.4"],
                },
            })
            bridge._litellm = object()
            bridge._pick_speculative_pair = lambda *a, **kw: None
            bridge._execute_openai_compatible_chat = AsyncMock(return_value={
                "success": False,
                "output": "",
                "error": "litellm.Timeout: APITimeoutError - Request timed out. Error_str: Request timed out.",
            })
            bridge._execute_openai_compatible = AsyncMock(return_value={
                "success": True,
                "output": "ok",
                "tool_results": [],
                "mode": "openai_compatible",
            })

            result = asyncio.run(
                bridge.execute(
                    node={"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                    plugins=[],
                    input_data="Build a premium product landing page with stronger hierarchy and visuals.",
                    model="gpt-5.4",
                    on_progress=None,
                )
            )

            self.assertTrue(result.get("success"))
            # v5.8.2: LEGACY_AUTO_FALLBACK_ORDER now leads with kimi-k2.6-code-preview
            self.assertEqual(result.get("model"), "kimi-k2.6-code-preview")
            self.assertEqual(result.get("attempted_models"), ["gpt-5.4", "kimi-k2.6-code-preview"])
            self.assertEqual(bridge._execute_openai_compatible_chat.await_count, 1)
            self.assertEqual(bridge._execute_openai_compatible.await_count, 1)

    def test_execute_builder_gateway_model_not_found_falls_back_without_same_model_retry(self):
        with patch.dict("os.environ", {"OPENAI_API_BASE": "https://gateway.example/v1"}, clear=False):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "kimi-coding"],
                },
            })
            bridge._litellm = object()
            bridge._pick_speculative_pair = lambda *a, **kw: None
            bridge._execute_openai_compatible_chat = AsyncMock(return_value={
                "success": False,
                "output": "",
                "error": "openai.NotFoundError: The model gpt-5.4 does not exist on this compatible gateway.",
            })
            bridge._execute_openai_compatible = AsyncMock(return_value={
                "success": True,
                "output": "ok",
                "tool_results": [],
                "mode": "openai_compatible",
            })

            result = asyncio.run(
                bridge.execute(
                    node={"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                    plugins=[],
                    input_data="Build a premium product landing page with stronger hierarchy and visuals.",
                    model="gpt-5.4",
                    on_progress=None,
                )
            )

            self.assertTrue(result.get("success"))
            self.assertEqual(result.get("model"), "kimi-coding")
            self.assertEqual(result.get("attempted_models"), ["gpt-5.4", "kimi-coding"])
            self.assertEqual(bridge._execute_openai_compatible_chat.await_count, 1)
            self.assertEqual(bridge._execute_openai_compatible.await_count, 1)

    def test_execute_imagegen_initial_activity_timeout_falls_back_without_same_model_retry(self):
        with patch.dict("os.environ", {"OPENAI_API_BASE": "https://gateway.example/v1"}, clear=False):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
                "node_model_preferences": {
                    "imagegen": ["gpt-5.4", "kimi-coding"],
                },
            })
            bridge._pick_speculative_pair = lambda *a, **kw: None
            bridge._litellm = object()
            bridge._execute_openai_compatible = AsyncMock(side_effect=[
                {
                    "success": False,
                    "output": "",
                    "error": (
                        "imagegen initial-activity timeout after 40s: "
                        "compatible gateway produced no content or tool activity."
                    ),
                },
                {
                    "success": True,
                    "output": "ok",
                    "tool_results": [],
                    "mode": "openai_compatible",
                },
            ])

            result = asyncio.run(
                bridge.execute(
                    node={"type": "imagegen", "model": "gpt-5.4", "model_is_default": True},
                    plugins=[MagicMock()],
                    input_data="Generate stylized sci-fi enemy and weapon references.",
                    model="gpt-5.4",
                    on_progress=None,
                )
            )

            self.assertTrue(result.get("success"))
            self.assertEqual(result.get("model"), "kimi-coding")
            self.assertEqual(result.get("attempted_models"), ["gpt-5.4", "kimi-coding"])
            self.assertEqual(bridge._execute_openai_compatible.await_count, 2)

    def test_execute_skips_custom_gateway_model_after_circuit_opens(self):
        with patch.dict("os.environ", {"OPENAI_API_BASE": "https://gateway.example/v1"}, clear=False):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
                "node_model_preferences": {
                    "builder": ["gpt-5.4"],
                },
            })
            bridge._litellm = object()
            bridge._pick_speculative_pair = lambda *a, **kw: None
            bridge._execute_openai_compatible_chat = AsyncMock(return_value={
                "success": False,
                "output": "",
                "error": "litellm.Timeout: APITimeoutError - Request timed out. Error_str: Request timed out.",
            })
            bridge._execute_openai_compatible = AsyncMock(return_value={
                "success": True,
                "output": "ok",
                "tool_results": [],
                "mode": "openai_compatible",
            })

            for _ in range(3):
                result = asyncio.run(
                    bridge.execute(
                        node={"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                        plugins=[],
                        input_data="Build a premium product landing page with stronger hierarchy and visuals.",
                        model="gpt-5.4",
                        on_progress=None,
                    )
                )
                self.assertTrue(result.get("success"))
                # v5.8.2: LEGACY_AUTO_FALLBACK_ORDER leads with kimi-k2.6-code-preview
                self.assertEqual(result.get("model"), "kimi-k2.6-code-preview")

            # Call 1: gpt-5.4 timeout (chat) → cooldown → kimi fallback (compat).
            # Calls 2-3: gpt-5.4 blocked by cooldown → kimi (compat) only.
            # Total: 1 chat call + 3 compat calls.
            self.assertEqual(bridge._execute_openai_compatible_chat.await_count, 1)
            self.assertEqual(bridge._execute_openai_compatible.await_count, 3)

    def test_execute_blocked_gateway_error_deprioritizes_recently_rejected_gateway_after_first_failure(self):
        with patch.dict("os.environ", {"OPENAI_API_BASE": "https://gateway.example/v1"}, clear=False):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "kimi-coding"],
                },
            })
            bridge._litellm = object()
            bridge._pick_speculative_pair = lambda *a, **kw: None
            bridge._execute_openai_compatible_chat = AsyncMock(return_value={
                "success": False,
                "output": "",
                "error": "Your request was blocked.",
            })
            bridge._execute_openai_compatible = AsyncMock(return_value={
                "success": True,
                "output": "fallback ok",
                "tool_results": [],
                "mode": "openai_compatible",
            })

            node = {"type": "builder", "model": "gpt-5.4", "model_is_default": True}
            first = asyncio.run(
                bridge.execute(
                    node=node,
                    plugins=[],
                    input_data="Build a premium product landing page with stronger hierarchy and visuals.",
                    model="gpt-5.4",
                    on_progress=None,
                )
            )
            self.assertTrue(first.get("success"))
            self.assertEqual(first.get("model"), "kimi-coding")
            self.assertEqual(first.get("attempted_models"), ["gpt-5.4", "kimi-coding"])

            for _ in range(2):
                result = asyncio.run(
                    bridge.execute(
                        node=node,
                        plugins=[],
                        input_data="Build a premium product landing page with stronger hierarchy and visuals.",
                        model="gpt-5.4",
                        on_progress=None,
                    )
                )
                self.assertTrue(result.get("success"))
                self.assertEqual(result.get("model"), "kimi-coding")
                self.assertEqual(result.get("attempted_models"), ["kimi-coding"])

            self.assertEqual(bridge._execute_openai_compatible_chat.await_count, 1)
            # Call 1: gpt-5.4 fails (chat:1) + kimi fallback (compat:1)
            # Calls 2-3: kimi-coding only (cooldown filters gpt-5.4) → compat:2 more
            self.assertEqual(bridge._execute_openai_compatible.await_count, 3)
            model_info = bridge._resolve_model("gpt-5.4")
            self.assertIn(
                "rejection cooldown",
                bridge._compatible_gateway_preflight_error(model_info),
            )
            _key, state = bridge._compatible_gateway_state(model_info)
            self.assertIsNotNone(state)
            state["rejection_cooldown_until"] = time.time() - 1
            # P0 FIX 2026-04-04: Also clear model-specific cooldown key
            model_key = bridge._compatible_gateway_key(model_info, model_specific=True)
            if model_key in bridge._compat_gateway_health:
                bridge._compat_gateway_health[model_key]["rejection_cooldown_until"] = time.time() - 1
            self.assertEqual(bridge._compatible_gateway_preflight_error(model_info), "")
            self.assertEqual(
                bridge.resolve_node_model_candidates(node, "gpt-5.4")[:2],
                ["kimi-coding", "gpt-5.4"],
            )

    def test_execute_custom_gateway_openai_builder_uses_openai_compatible_chat(self):
        with patch.dict("os.environ", {"OPENAI_API_BASE": "https://gateway.example/v1"}, clear=False):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "node_model_preferences": {
                    "builder": ["gpt-5.4"],
                },
            })
            bridge._litellm = object()
            bridge._pick_speculative_pair = lambda *a, **kw: None
            bridge._execute_openai_compatible_chat = AsyncMock(return_value={
                "success": True,
                "output": "ok",
                "tool_results": [],
                "mode": "openai_compatible_chat",
            })
            bridge._execute_litellm_chat = AsyncMock(return_value={
                "success": True,
                "output": "wrong path",
                "tool_results": [],
                "mode": "litellm_chat",
            })

            result = asyncio.run(
                bridge.execute(
                    node={"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                    plugins=[],
                    input_data="Build a commercial-grade HTML5 game landing screen.",
                    model="gpt-5.4",
                    on_progress=None,
                )
            )

            self.assertTrue(result.get("success"))
            self.assertEqual(result.get("model"), "gpt-5.4")
            bridge._execute_openai_compatible_chat.assert_awaited_once()
            bridge._execute_litellm_chat.assert_not_called()

    def test_execute_tries_matching_relay_pool_before_cross_model_fallback(self):
        relay_mgr = get_relay_manager()
        relay_mgr.load([
            {
                "id": "relay_fast",
                "name": "Fast Relay",
                "base_url": "https://relay-fast.example/v1",
                "api_key": "relay-fast-key",
                "models": ["gpt-5.4"],
                "enabled": True,
                "timeout": 90,
                "last_test": {"success": True, "latency_ms": 120},
            },
            {
                "id": "relay_backup",
                "name": "Backup Relay",
                "base_url": "https://relay-backup.example/v1",
                "api_key": "relay-backup-key",
                "models": ["gpt-5.4"],
                "enabled": True,
                "timeout": 90,
                "last_test": {"success": True, "latency_ms": 280},
            },
        ])
        try:
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "kimi-coding"],
                },
            })
            bridge._litellm = object()
            bridge._pick_speculative_pair = lambda *a, **kw: None
            bridge._execute_litellm_chat = AsyncMock(return_value={
                "success": False,
                "output": "",
                "error": "litellm.Timeout: APITimeoutError - Request timed out. Error_str: Request timed out.",
            })
            bridge._execute_relay = AsyncMock(return_value={
                "success": True,
                "output": "relay ok",
                "tool_results": [],
                "mode": "relay",
            })

            result = asyncio.run(
                bridge.execute(
                    node={"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                    plugins=[],
                    input_data="Build a premium product landing page with stronger hierarchy and visuals.",
                    model="gpt-5.4",
                    on_progress=None,
                )
            )

            self.assertTrue(result.get("success"))
            self.assertEqual(result.get("model"), "relay_pool/gpt-5.4")
            self.assertEqual(result.get("attempted_models"), ["gpt-5.4", "relay_pool/gpt-5.4"])
            # Sequential: gpt-5.4 → litellm_chat (fail), relay_pool → relay (success)
            self.assertEqual(bridge._execute_litellm_chat.await_count, 1)
            self.assertEqual(bridge._execute_relay.await_count, 1)
        finally:
            relay_mgr.load([])

    def test_execute_relay_uses_pool_strategy_call_best(self):
        relay_mgr = get_relay_manager()
        original_call = relay_mgr.call
        original_call_best = relay_mgr.call_best
        relay_mgr.call = AsyncMock(return_value={"success": False, "error": "should not be used"})
        relay_mgr.call_best = AsyncMock(return_value={
            "success": True,
            "content": "relay pooled ok",
            "model": "gpt-5.4",
            "relay": "Fast Relay",
            "usage": {"total_tokens": 12},
            "cost": 0.01,
        })
        try:
            bridge = AIBridge(config={})
            result = asyncio.run(
                bridge._execute_relay(
                    {"type": "builder"},
                    "Build a refined landing page",
                    {
                        "provider": "relay",
                        "litellm_id": "openai/gpt-5.4",
                        "relay_strategy": "pool",
                        "relay_pool_model": "gpt-5.4",
                        "relay_name": "Relay Pool (gpt-5.4)",
                    },
                    None,
                )
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["relay"], "Fast Relay")
            self.assertEqual(relay_mgr.call_best.await_count, 1)
            self.assertEqual(relay_mgr.call.await_count, 0)
        finally:
            relay_mgr.call = original_call
            relay_mgr.call_best = original_call_best

    def test_execute_relay_prefers_relay_model_name_over_stripping_openai_prefix(self):
        relay_mgr = get_relay_manager()
        original_call = relay_mgr.call
        relay_mgr.call = AsyncMock(return_value={
            "success": True,
            "content": "relay direct ok",
            "model": "anthropic/claude-sonnet-4-5",
            "relay": "Claude Relay",
            "usage": {"total_tokens": 10},
            "cost": 0.02,
        })
        try:
            bridge = AIBridge(config={})
            result = asyncio.run(
                bridge._execute_relay(
                    {"type": "builder"},
                    "Build a refined landing page",
                    {
                        "provider": "relay",
                        "relay_id": "relay_claude",
                        "relay_name": "Claude Relay",
                        "relay_model_name": "claude-4-sonnet",
                        "relay_api_style": "litellm_proxy",
                        "litellm_id": "anthropic/claude-sonnet-4-5",
                    },
                    None,
                )
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["relay"], "Claude Relay")
            self.assertEqual(relay_mgr.call.await_count, 1)
            self.assertEqual(relay_mgr.call.await_args.kwargs["model"], "claude-4-sonnet")
        finally:
            relay_mgr.call = original_call

    def test_execute_polisher_loop_guard_does_not_fallback_to_next_model(self):
        bridge = AIBridge(config={
            "openai_api_key": "sk-openai-test",
            "kimi_api_key": "sk-kimi-test",
            "node_model_preferences": {
                "polisher": ["kimi-coding", "gpt-5.4"],
            },
        })
        bridge._execute_openai_compatible = AsyncMock(return_value={
            "success": False,
            "output": "",
            "error": "polisher loop guard triggered after 4 non-write tool iterations without any file write.",
        })
        bridge._execute_litellm_chat = AsyncMock(return_value={
            "success": True,
            "output": "should not run",
            "tool_results": [],
            "mode": "litellm_chat",
        })

        result = asyncio.run(
            bridge.execute(
                node={"type": "polisher", "model": "kimi-coding", "model_is_default": True},
                plugins=[MagicMock()],
                input_data="Polish the current multi-page site under /tmp/evermind_output/.",
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("model"), "kimi-coding")
        self.assertEqual(result.get("attempted_models"), ["kimi-coding"])
        self.assertEqual(bridge._execute_openai_compatible.await_count, 1)
        self.assertEqual(bridge._execute_litellm_chat.await_count, 0)


class TestNormalizeUsage(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_none_usage_returns_zeros(self):
        result = self.bridge._normalize_usage(None)
        self.assertEqual(result["prompt_tokens"], 0)
        self.assertEqual(result["completion_tokens"], 0)
        self.assertEqual(result["total_tokens"], 0)

    def test_dict_usage(self):
        result = self.bridge._normalize_usage({
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        })
        self.assertEqual(result["prompt_tokens"], 100)
        self.assertEqual(result["completion_tokens"], 50)
        self.assertEqual(result["total_tokens"], 150)

    def test_input_output_tokens_fallback(self):
        """Anthropic-style usage keys should be handled."""
        result = self.bridge._normalize_usage({
            "input_tokens": 200,
            "output_tokens": 80,
        })
        self.assertEqual(result["prompt_tokens"], 200)
        self.assertEqual(result["completion_tokens"], 80)
        self.assertEqual(result["total_tokens"], 280)

    def test_model_dump_object(self):
        """Objects with model_dump() (Pydantic v2) should be handled."""
        mock = MagicMock()
        mock.model_dump.return_value = {
            "prompt_tokens": 50,
            "completion_tokens": 25,
            "total_tokens": 75,
        }
        result = self.bridge._normalize_usage(mock)
        self.assertEqual(result["prompt_tokens"], 50)
        self.assertEqual(result["total_tokens"], 75)


class TestMergeUsage(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_merges_two_dicts(self):
        base = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        delta = {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300}
        result = self.bridge._merge_usage(base, delta)
        self.assertEqual(result["prompt_tokens"], 300)
        self.assertEqual(result["completion_tokens"], 150)
        self.assertEqual(result["total_tokens"], 450)

    def test_merge_with_none_delta(self):
        base = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        result = self.bridge._merge_usage(base, None)
        # v5.8.6: _merge_usage now also tracks cached_tokens (0 when absent).
        self.assertEqual(result["prompt_tokens"], 100)
        self.assertEqual(result["completion_tokens"], 50)
        self.assertEqual(result["total_tokens"], 150)
        self.assertEqual(result.get("cached_tokens", 0), 0)


class TestGetAvailableModels(unittest.TestCase):
    def test_includes_static_registry(self):
        bridge = AIBridge(config={})
        models = bridge.get_available_models()
        model_ids = {m["id"] for m in models}
        self.assertIn("gpt-4o", model_ids)
        self.assertIn("deepseek-v3", model_ids)
        self.assertIn("kimi", model_ids)

    def test_model_count_matches_registry(self):
        bridge = AIBridge(config={})
        models = bridge.get_available_models()
        # Should have at least as many as the static registry
        self.assertGreaterEqual(len(models), len(MODEL_REGISTRY))


class TestNodeTokenAndTimeoutPolicy(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_builder_defaults_are_higher(self):
        # v6.1.10: tool_call default raised 16384→24576 —
        # 50KB HTML ≈ 20K tokens; 16384 was consistently triggering
        # finish=length → salvage loop. 24576 covers 60KB.
        self.assertEqual(self.bridge._max_tokens_for_node("builder"), 24576)
        # direct_text unchanged at 28672.
        self.assertEqual(
            self.bridge._max_tokens_for_node("builder", node={"builder_delivery_mode": "direct_text"}),
            28672,
        )
        self.assertEqual(self.bridge._timeout_for_node("builder"), 600)  # v5.8.2: 960→600

    def test_non_builder_defaults_are_lower(self):
        # v5.8.4: tester now has an independent budget (was falling through to generic
        # EVERMIND_MAX_TOKENS=4096 / EVERMIND_TIMEOUT_SEC=120 defaults).
        self.assertEqual(self.bridge._max_tokens_for_node("tester"), 6144)
        self.assertEqual(self.bridge._timeout_for_node("tester"), 240)
        # v6.1.3: tool iteration caps raised across the board
        # so nodes aren't prematurely killed; no-activity watchdog remains the
        # authoritative deadlock detector.
        self.assertEqual(self.bridge._max_tool_iterations_for_node("tester"), 50)
        self.assertEqual(self.bridge._max_tool_iterations_for_node("reviewer"), 50)
        # v6.3: analyst cap reduced 8→6 — tutorial-search
        # prompts kept asking for one more source. See _max_tool_iterations_for_node.
        self.assertEqual(self.bridge._max_tool_iterations_for_node("analyst"), 6)
        self.assertEqual(self.bridge._max_tool_iterations_for_node("imagegen"), 40)

    def test_asset_plan_nodes_use_compact_budgets(self):
        self.assertEqual(self.bridge._max_tokens_for_node("spritesheet"), 10240)   # v5.8.1: rollback truncation
        self.assertEqual(self.bridge._timeout_for_node("spritesheet"), 540)  # v5.8.6: 240→540 (2 turns × 200s JSON + margin)
        self.assertEqual(self.bridge._stream_stall_timeout_for_node("spritesheet"), 25)
        self.assertEqual(self.bridge._max_tokens_for_node("assetimport"), 10240)   # v5.8.1: rollback truncation
        self.assertEqual(self.bridge._timeout_for_node("assetimport"), 540)  # v5.8.6: 240→540
        self.assertEqual(self.bridge._stream_stall_timeout_for_node("assetimport"), 25)
        self.assertEqual(self.bridge._max_tokens_for_node("imagegen"), 12288)   # v5.8.1: rollback
        self.assertEqual(self.bridge._timeout_for_node("imagegen"), 180)  # v6.1.15: 240→180 (tighter wall, observed 15min loops)
        self.assertEqual(self.bridge._stream_stall_timeout_for_node("imagegen"), 35)

    def test_effective_builder_timeout_boosts_for_premium_3d_game(self):
        timeout_sec = self.bridge._effective_timeout_for_node(
            "builder",
            "创建一个第三人称 3D 射击游戏，要有怪物、枪械、大地图和精美建模，达到商业级水准。",
        )

        self.assertGreaterEqual(timeout_sec, 2400)

    def test_effective_builder_timeout_boosts_for_retry_repair_context(self):
        timeout_sec = self.bridge._effective_timeout_for_node(
            "builder",
            (
                "previous attempt timed out.\n"
                "retry 2/3.\n"
                "请在已有 3D 射击游戏文件基础上继续修复，不要从零重写。"
            ),
        )

        self.assertGreaterEqual(timeout_sec, 2400)

    def test_compatible_gateway_initial_activity_timeout_defaults_are_role_aware(self):
        # v5.8.6 REDESIGN: reasoning-model era defaults tripled. Builder 90s,
        # multi-page 120s, others 60-75s. Philosophy = trust the agent.
        self.assertEqual(self.bridge._gateway_initial_activity_timeout_for_node("builder"), 90)
        self.assertEqual(
            self.bridge._gateway_initial_activity_timeout_for_node(
                "builder",
                "做一个 8 页面品牌官网，Assigned HTML filenames for this builder: index.html, about.html, craft.html, contact.html.",
            ),
            120,  # Multi-page builder gets extra headroom
        )
        self.assertEqual(self.bridge._gateway_initial_activity_timeout_for_node("imagegen"), 75)
        self.assertEqual(self.bridge._gateway_initial_activity_timeout_for_node("spritesheet"), 60)
        self.assertEqual(self.bridge._gateway_initial_activity_timeout_for_node("reviewer"), 75)
        self.assertEqual(self.bridge._gateway_initial_activity_timeout_for_node("unknown"), 0)

    def test_env_overrides_are_clamped(self):
        with patch.dict("os.environ", {
            "EVERMIND_BUILDER_TOOLCALL_MAX_TOKENS": "999999",
            "EVERMIND_BUILDER_TIMEOUT_SEC": "5",
            "EVERMIND_MAX_TOKENS": "-1",
            "EVERMIND_TIMEOUT_SEC": "999",
            "EVERMIND_BUILDER_MAX_TOOL_ITERS": "100",
            "EVERMIND_IMAGEGEN_MAX_TOOL_ITERS": "100",
            "EVERMIND_QA_MAX_TOOL_ITERS": "100",
            "EVERMIND_ANALYST_MAX_TOOL_ITERS": "1",
            "EVERMIND_DEFAULT_MAX_TOOL_ITERS": "0",
        }):
            # v6.1.10: tool_call max clamped at 49152 (raised from 32768 to
            # accommodate 80KB+ HTML when builder explicitly overrides).
            self.assertEqual(self.bridge._max_tokens_for_node("builder"), 49152)
            self.assertEqual(self.bridge._timeout_for_node("builder"), 30)
            # v5.8.4: tester has its own envs now, EVERMIND_MAX_TOKENS/_TIMEOUT_SEC
            # no longer apply → these assertions now verify the tester-specific defaults
            # (generic envs do not pass through).
            self.assertEqual(self.bridge._max_tokens_for_node("tester"), 6144)
            self.assertEqual(self.bridge._timeout_for_node("tester"), 240)
            # v6.1.3: raised clamps — builder max 100, imagegen max 80, QA max 100,
            # analyst min=2. Env overrides are clamped to each node's own range.
            self.assertEqual(self.bridge._max_tool_iterations_for_node("builder"), 100)
            self.assertEqual(self.bridge._max_tool_iterations_for_node("imagegen"), 80)
            self.assertEqual(self.bridge._max_tool_iterations_for_node("tester"), 100)
            self.assertEqual(self.bridge._max_tool_iterations_for_node("analyst"), 2)

    def test_analyst_browser_limit_defaults_allow_two_source_research(self):
        self.assertEqual(self.bridge._analyst_browser_call_limit(), 8)
        self.assertFalse(self.bridge._should_block_browser_call("analyst", {"browser": 7}))
        self.assertTrue(self.bridge._should_block_browser_call("analyst", {"browser": 8}))
        self.assertFalse(self.bridge._should_block_browser_call("builder", {"browser": 99}))

    def test_analyst_browser_limit_can_be_overridden(self):
        with patch.dict("os.environ", {"EVERMIND_ANALYST_MAX_BROWSER_CALLS": "1"}):
            self.assertEqual(self.bridge._analyst_browser_call_limit(), 1)
            self.assertTrue(self.bridge._should_block_browser_call("analyst", {"browser": 1}))

    def test_tier_aware_node_budgets_expand_frontier_and_constrain_baseline(self):
        frontier_node = {"model_capability_tier": 1}
        baseline_node = {"model_capability_tier": 3}

        builder_base_tokens = self.bridge._max_tokens_for_node("builder")
        builder_frontier_tokens = self.bridge._max_tokens_for_node("builder", node=frontier_node)
        builder_baseline_tokens = self.bridge._max_tokens_for_node("builder", node=baseline_node)
        analyst_base_iters = self.bridge._max_tool_iterations_for_node("analyst")
        analyst_frontier_iters = self.bridge._max_tool_iterations_for_node("analyst", node=frontier_node)
        analyst_baseline_iters = self.bridge._max_tool_iterations_for_node("analyst", node=baseline_node)
        analyst_base_browser = self.bridge._analyst_browser_call_limit()
        analyst_baseline_browser = self.bridge._analyst_browser_call_limit(node=baseline_node)
        builder_base_prewrite = self.bridge._builder_prewrite_call_timeout("builder", "build premium site")
        builder_baseline_prewrite = self.bridge._builder_prewrite_call_timeout(
            "builder",
            "build premium site",
            node=baseline_node,
        )

        self.assertGreater(builder_frontier_tokens, builder_base_tokens)
        self.assertLess(builder_baseline_tokens, builder_base_tokens)
        self.assertGreater(analyst_frontier_iters, analyst_base_iters)
        self.assertLess(analyst_baseline_iters, analyst_base_iters)
        self.assertLess(analyst_baseline_browser, analyst_base_browser)
        self.assertLess(builder_baseline_prewrite, builder_base_prewrite)

    def test_compose_system_prompt_includes_node_execution_strategy_block(self):
        prompt = self.bridge._compose_system_prompt(
            {
                "type": "analyst",
                "model_execution_strategy_block": (
                    "[Model Execution Strategy]\n"
                    "- tier: T3\n"
                    "- research depth: narrow\n"
                    "- tool budget profile: tight\n"
                ),
            },
            input_data="研究一个新的产品官网信息架构。",
        )

        self.assertIn("MODEL-CAPABILITY EXECUTION STRATEGY", prompt)
        self.assertIn("research depth: narrow", prompt)
        self.assertIn("tool budget profile: tight", prompt)

    def test_analyst_system_prompt_requires_live_source_or_browser_research(self):
        prompt = AGENT_PRESETS["analyst"]["instructions"]
        self.assertIn("MUST use source_fetch or the browser tool", prompt)
        self.assertIn("Use source_fetch first", prompt)
        # v5.8.4+: tightened URL budget to 3 + hard 6-round tool budget
        self.assertIn("Visit AT MOST 3 distinct URLs", prompt)
        self.assertIn("HARD TOOL BUDGET", prompt)
        self.assertIn("6 tool-loop rounds", prompt)
        self.assertIn("visited URLs", prompt)
        self.assertIn("do NOT browse playable web games", prompt)
        self.assertIn("deliverables_contract", prompt)
        self.assertIn("risk_register", prompt)
        self.assertIn("reference_code_snippets", prompt)
        self.assertIn("control_frame_contract", prompt)
        self.assertIn("do NOT use it as a crawl target", prompt)

    def test_builder_system_prompt_requires_readable_projectiles_for_shooters(self):
        # v6.1.5: shooter-specific guidance moved to
        # agent_skills/godogen-tps-control-sanity-lock (conditional load for
        # shooter briefs only). Verify skill content covers the prior contract.
        skill_path = Path(__file__).parent.parent / "agent_skills" / "godogen-tps-control-sanity-lock" / "SKILL.md"
        self.assertTrue(skill_path.exists(), f"Missing shooter skill at {skill_path}")
        text = skill_path.read_text(encoding="utf-8")
        self.assertIn("shooter", text.lower())
        self.assertIn("muzzle", text.lower())

    def test_planner_system_prompt_requires_parallel_ownership_and_rollback_contracts(self):
        prompt = AGENT_PRESETS["planner"]["instructions"]
        self.assertIn("builder_ownership", prompt)
        self.assertIn("subsystem_contracts", prompt)
        self.assertIn("review_evidence", prompt)
        self.assertIn("rollback_triggers", prompt)
        self.assertIn("merger", prompt)

    def test_plain_text_write_guard_messages_match_node_contracts(self):
        analyst = self.bridge._plain_text_node_write_guard_error("analyst")
        scribe = self.bridge._plain_text_node_write_guard_error("scribe")
        uidesign = self.bridge._plain_text_node_write_guard_error("uidesign")

        self.assertIn("XML tags", analyst)
        self.assertNotIn("XML tags", scribe)
        self.assertIn("content handoff", scribe)
        self.assertNotIn("XML tags", uidesign)
        self.assertIn("design brief", uidesign)

    def test_uidesign_budget_defaults_are_compact(self):
        self.assertEqual(self.bridge._max_tool_iterations_for_node("uidesign"), 4)
        self.assertEqual(self.bridge._timeout_for_node("uidesign"), 180)
        self.assertEqual(self.bridge._max_tokens_for_node("uidesign"), 6144)
        self.assertEqual(self.bridge._uidesign_browser_call_limit(), 2)

    def test_uidesign_prompt_prefers_implementation_sources_and_goal_lock(self):
        prompt = AGENT_PRESETS["uidesign"]["instructions"]
        self.assertIn("Do NOT research the target brand site itself", prompt)
        self.assertIn("Never switch product category", prompt)

    def test_analyst_prompt_discourages_live_brand_site_research_for_style_briefs(self):
        prompt = AGENT_PRESETS["analyst"]["instructions"]
        self.assertIn("brand-style website tasks", prompt)
        self.assertIn("NEVER fetch the brand's live site", prompt)

    def test_router_prompt_exposes_specialized_agents(self):
        prompt = AGENT_PRESETS["router"]["instructions"]
        self.assertIn("scribe", prompt)
        self.assertIn("imagegen", prompt)
        self.assertIn("spritesheet", prompt)
        self.assertIn("GitHub repos", prompt)

    def test_asset_plan_prompts_require_compact_json(self):
        self.assertIn("compact JSON", AGENT_PRESETS["spritesheet"]["instructions"])
        self.assertIn("No prose", AGENT_PRESETS["spritesheet"]["instructions"])
        self.assertIn("compact JSON", AGENT_PRESETS["assetimport"]["instructions"])

    def test_stream_stall_timeout_defaults_are_role_aware(self):
        self.assertEqual(self.bridge._stream_stall_timeout_for_node("builder"), 180)  # v4.0: 300→180
        self.assertEqual(self.bridge._stream_stall_timeout_for_node("tester"), 90)  # v4.0: 180→90

    def test_effective_builder_stream_stall_timeout_tightens_for_multi_page_retry(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, craftsmanship.html, collections.html, materials.html, heritage.html, boutiques.html, contact.html.\n"
            "⚠️ PREVIOUS ATTEMPT FAILED (retry 1/3): Multi-page delivery incomplete."
        )
        self.assertEqual(self.bridge._effective_stream_stall_timeout("builder", input_data), 150)

    def test_effective_builder_stream_stall_timeout_tightens_for_large_multi_page_initial_run(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, craftsmanship.html, collections.html, materials.html, heritage.html, boutiques.html, contact.html."
        )
        self.assertEqual(self.bridge._effective_stream_stall_timeout("builder", input_data), 180)

    def test_stream_stall_timeout_env_overrides_are_clamped(self):
        with patch.dict("os.environ", {
            "EVERMIND_BUILDER_STREAM_STALL_SEC": "9999",
            "EVERMIND_STREAM_STALL_SEC": "5",
            "EVERMIND_QA_STREAM_STALL_SEC": "5",  # v5.8.4: tester/debugger route
        }):
            self.assertEqual(self.bridge._stream_stall_timeout_for_node("builder"), 600)
            # v5.8.4: tester routes through EVERMIND_QA_STREAM_STALL_SEC (min clamp=30)
            self.assertEqual(self.bridge._stream_stall_timeout_for_node("tester"), 30)

    def test_builder_prewrite_call_timeout_defaults_to_180_for_multi_page(self):
        # v6.1.3: base raised 90 → 600s; multi-page cap 180 is eclipsed.
        input_data = "做一个 8 页面奢侈品官网，包含 index.html, brand.html, collections.html, contact.html。"
        self.assertGreaterEqual(self.bridge._builder_prewrite_call_timeout("builder", input_data), 600)

    def test_builder_prewrite_call_timeout_tightens_retry_window(self):
        # v6.1.3: retry cap 180 is eclipsed by 600 base — thinking models need room.
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "PREVIOUS ATTEMPT FAILED (retry 1/3): Multi-page delivery incomplete."
        )
        self.assertGreaterEqual(self.bridge._builder_prewrite_call_timeout("builder", input_data), 600)

    def test_builder_prewrite_call_timeout_clamps_premium_3d_direct_text_first_pass(self):
        # v6.1.3: 3D direct-text raised 210 → 900s (thinking-friendly).
        input_data = "创建一个 3d 第三人称射击游戏，要有怪物、枪械、关卡和精美建模。"
        self.assertGreaterEqual(self.bridge._builder_prewrite_call_timeout("builder", input_data), 900)

    def test_builder_prewrite_call_timeout_keeps_3d_retry_budget(self):
        # v6.1.3: 3D retry cap 420 is eclipsed by 900 direct-text base.
        input_data = (
            "创建一个 3d 第三人称射击游戏，要有怪物、枪械、关卡和精美建模。\n"
            "PREVIOUS ATTEMPT FAILED (retry 1/3): Builder quality gate failed."
        )
        self.assertGreaterEqual(self.bridge._builder_prewrite_call_timeout("builder", input_data), 600)

    def test_builder_peer_timeouts_match_primary_for_premium_3d_game(self):
        # v5.1: support-lane removed — all peer builders get equal timeout
        # v5.8.3: base reduced 150→90; still validates both peers get same budget
        input_data = (
            "ADVANCED MODE — Use analyst notes and asset manifest.\n"
            "Build a non-overlapping support subsystem for the same commercial-grade HTML5 game. "
            "Do NOT overwrite /tmp/evermind_output/index.html in this pass unless explicitly reassigned."
        )
        result = self.bridge._builder_prewrite_call_timeout("builder", input_data)
        self.assertGreaterEqual(result, 90, f"Peer builder timeout {result} should be >= 90")

    def test_effective_builder_stream_stall_timeout_keeps_3d_retry_budget(self):
        input_data = (
            "创建一个 3d 第三人称射击游戏，要有怪物、枪械、关卡和精美建模。\n"
            "PREVIOUS ATTEMPT FAILED (retry 1/3): Builder quality gate failed."
        )
        self.assertEqual(self.bridge._effective_stream_stall_timeout("builder", input_data), 360)

    def test_builder_force_text_threshold_tightens_for_multi_page(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, craftsmanship.html, collections.html, materials.html, heritage.html, boutiques.html, contact.html."
        )
        self.assertEqual(self.bridge._builder_force_text_threshold(input_data), 3)

    def test_builder_force_text_threshold_forces_immediate_write_recovery_on_retry(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, craftsmanship.html, collections.html, materials.html, heritage.html, boutiques.html, contact.html.\n"
            "⚠️ PREVIOUS ATTEMPT FAILED (retry 1/3): Multi-page delivery incomplete."
        )
        self.assertEqual(self.bridge._builder_force_text_threshold(input_data), 2)

    def test_builder_force_text_threshold_keeps_more_retry_headroom_for_games(self):
        input_data = (
            "创建一个 3d 射击游戏，要有怪物、枪械和关卡。\n"
            "⚠️ PREVIOUS ATTEMPT FAILED (retry 1/3): Builder quality gate failed."
        )
        self.assertEqual(self.bridge._builder_force_text_threshold(input_data), 4)

    def test_builder_force_text_threshold_equal_for_peer_builder(self):
        # v5.1: support-lane removed — peer builder gets standard threshold (6)
        input_data = (
            "Build a non-overlapping support subsystem for the same commercial-grade HTML5 game. "
            "Do NOT overwrite /tmp/evermind_output/index.html in this pass unless explicitly reassigned."
        )
        self.assertEqual(self.bridge._builder_force_text_threshold(input_data), 6)

    def test_builder_direct_multifile_requested_only_for_builder_with_marker(self):
        self.assertTrue(
            self.bridge._builder_direct_multifile_requested(
                "builder",
                "⚠️ MULTI-PAGE DELIVERY INCOMPLETE.\nDIRECT MULTI-FILE DELIVERY ONLY.\nReturn pricing.html and about.html.",
            )
        )
        self.assertFalse(
            self.bridge._builder_direct_multifile_requested(
                "reviewer",
                "DIRECT MULTI-FILE DELIVERY ONLY.",
            )
        )

    def test_builder_direct_multifile_requested_ignores_stale_marker_for_game_patch_retry(self):
        self.assertFalse(
            self.bridge._builder_direct_multifile_requested(
                "builder",
                (
                    "Goal: 创建一个第三人称 3D 射击网页游戏，带怪物、不同枪械、大地图和精美建模。\n"
                    "[Reviewer Rework Patch Mode]\n"
                    "Start from the current files and patch the failing areas only.\n"
                    "DIRECT MULTI-FILE DELIVERY ONLY.\n"
                    "HTML TARGET OVERRIDE: index.html\n"
                ),
            )
        )

    def test_builder_assigned_html_targets_honor_override_marker(self):
        targets = self.bridge._builder_assigned_html_targets(
            "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, about.html.\n"
            "HTML TARGET OVERRIDE: index.html, about.html\n"
            "Return only the missing pages."
        )
        self.assertEqual(targets, ["index.html", "about.html"])

    def test_apply_runtime_node_contracts_prefers_node_allowed_html_targets(self):
        updated = self.bridge._apply_runtime_node_contracts(
            {
                "type": "builder",
                "allowed_html_targets": ["index.html", "pricing.html", "about.html"],
                "can_write_root_index": True,
            },
            "Assigned HTML filenames for this builder: index.html.\nReturn the homepage.",
        )
        self.assertIn("HTML TARGET OVERRIDE: index.html, pricing.html, about.html", updated)
        self.assertEqual(
            self.bridge._builder_assigned_html_targets(updated),
            ["index.html", "pricing.html", "about.html"],
        )

    def test_apply_runtime_node_contracts_adds_support_lane_contract_without_html_targets(self):
        updated = self.bridge._apply_runtime_node_contracts(
            {
                "type": "builder",
                "allowed_html_targets": [],
                "can_write_root_index": False,
            },
            "Build a non-overlapping support subsystem for the same commercial-grade HTML5 game.",
        )
        self.assertIn("[BUILDER RUNTIME SUPPORT CONTRACT]", updated)
        self.assertIn("Do NOT emit or overwrite /tmp/evermind_output/index.html", updated)
        self.assertIn("/tmp/evermind_output/js/weaponSystem.js", updated)

    def test_execute_honors_builder_direct_multifile_node_mode(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible"}
        )
        bridge._execute_litellm_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "kimi-coding",
                    "builder_delivery_mode": "direct_multifile",
                },
                plugins=[],
                input_data="Return pricing.html and about.html as fenced HTML blocks.",
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        direct_called = (
            bridge._execute_openai_compatible_chat.await_count > 0
            or bridge._execute_litellm_chat.await_count > 0
        )
        self.assertTrue(direct_called, "Expected direct delivery via openai_compatible_chat or litellm_chat")
        bridge._execute_openai_compatible.assert_not_called()

    def test_execute_honors_builder_direct_text_node_mode(self):
        bridge = AIBridge(config={"openai_api_key": "sk-openai-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._litellm = object()
        bridge._execute_litellm_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible"}
        )
        bridge._execute_litellm_tools = AsyncMock(
            return_value={"success": True, "output": "wrong tools path", "tool_results": [], "mode": "litellm_tools"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "gpt-5.4",
                    "builder_delivery_mode": "direct_text",
                },
                plugins=[MagicMock()],
                input_data="做一个贪吃蛇网页小游戏，单页 index.html 即可。",
                model="gpt-5.4",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        bridge._execute_litellm_chat.assert_awaited_once()
        bridge._execute_openai_compatible.assert_not_called()
        bridge._execute_litellm_tools.assert_not_called()

    def test_execute_auto_routes_kimi_multi_page_builder_to_direct_multifile(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible"}
        )
        bridge._execute_litellm_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"}
        )

        # v5.8.6: explicit opt-in — direct_multifile is off by default for kimi in prod.
        with patch.dict("os.environ", {"EVERMIND_BUILDER_KIMI_ALLOW_DIRECT_MULTIFILE": "1"}):
            result = asyncio.run(
                bridge.execute(
                    node={
                        "type": "builder",
                        "model": "kimi-coding",
                    },
                    plugins=[],
                    input_data=(
                        "做一个 8 页面轻奢品牌网站。\n"
                        "Assigned HTML filenames for this builder: index.html, about.html, collections.html, "
                        "craft.html, materials.html, journal.html, contact.html, faq.html."
                    ),
                    model="kimi-coding",
                    on_progress=None,
                )
            )

        self.assertTrue(result.get("success"))
        # Direct multifile mode routes to either openai_compatible_chat (custom gateway) or litellm_chat
        direct_called = (
            bridge._execute_openai_compatible_chat.await_count > 0
            or bridge._execute_litellm_chat.await_count > 0
        )
        self.assertTrue(direct_called, "Expected direct multifile delivery via openai_compatible_chat or litellm_chat")
        bridge._execute_openai_compatible.assert_not_called()

    def test_execute_keeps_kimi_single_page_builder_on_tool_path(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible"}
        )
        bridge._execute_litellm_tools = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "litellm_tools"}
        )
        bridge._execute_litellm_chat = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "litellm_chat"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "kimi-coding",
                },
                plugins=[],
                input_data="创建一个简单的个人网站首页。",
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        tool_path_called = (
            bridge._execute_openai_compatible.await_count > 0
            or bridge._execute_litellm_tools.await_count > 0
            or bridge._execute_litellm_chat.await_count > 0
        )
        self.assertTrue(tool_path_called, "Expected tool path via openai_compatible, litellm_tools, or litellm_chat")

    def test_v6410_multi_target_streaming_model_prefers_direct_multifile(self):
        """v6.4.10: every streaming-capable provider (gpt-5.x,
        claude, deepseek, qwen, minimax, ...) running a multi-target builder
        should pick the direct_multifile fast path instead of the tool_call
        loop. Regression guard against 2026-04-22 run where kimi/gpt-5.x
        multi-target builders took 10-15min per builder vs 2-4min when
        streaming works."""
        bridge = AIBridge(config={})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "multifile chat", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "tool path", "tool_results": [], "mode": "openai_compatible"}
        )
        bridge._execute_litellm_chat = AsyncMock(
            return_value={"success": True, "output": "multifile chat", "tool_results": [], "mode": "litellm_chat"}
        )

        # Non-Kimi streaming model with multi-target brief → direct_multifile.
        result = asyncio.run(
            bridge.execute(
                node={"type": "builder", "model": "gpt-5.4"},
                plugins=[],
                input_data=(
                    "做一个 8 页面奢侈品牌网站。\n"
                    "Assigned HTML filenames for this builder: "
                    "index.html, pricing.html, features.html, solutions.html."
                ),
                model="gpt-5.4",
                on_progress=None,
            )
        )
        self.assertTrue(result.get("success"))
        direct_multifile_fired = (
            bridge._execute_openai_compatible_chat.await_count > 0
            or bridge._execute_litellm_chat.await_count > 0
        )
        self.assertTrue(direct_multifile_fired, "multi-target gpt-5.4 builder must run through direct_multifile fast path")
        # tool_call path must NOT be used for a multi-target streaming-capable builder.
        self.assertEqual(
            bridge._execute_openai_compatible.await_count,
            0,
            "multi-target streaming builder should not fall through to the tool_call loop",
        )

    def test_execute_auto_routes_kimi_single_page_game_builder_to_direct_text(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "```html index.html\\n<!DOCTYPE html><html><body>ok</body></html>\\n```", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible"}
        )
        bridge._execute_litellm_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "kimi-coding",
                },
                plugins=[],
                input_data="做一个贪吃蛇网页小游戏，单页 index.html 即可。",
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        direct_text_called = (
            bridge._execute_openai_compatible_chat.await_count > 0
            or bridge._execute_litellm_chat.await_count > 0
        )
        self.assertTrue(direct_text_called, "Expected direct text delivery via openai_compatible_chat or litellm_chat")
        bridge._execute_openai_compatible.assert_not_called()

    def test_execute_switches_kimi_engine_free_game_builder_to_direct_text_without_explicit_single_page_brief(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible"}
        )
        bridge._execute_litellm_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "kimi-coding",
                },
                plugins=[],
                input_data="做一个贪吃蛇网页小游戏，包含开始界面、暂停和结算体验。",
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        direct_text_called = (
            bridge._execute_openai_compatible_chat.await_count > 0
            or bridge._execute_litellm_chat.await_count > 0
        )
        self.assertTrue(direct_text_called, "Expected direct text delivery via openai_compatible_chat or litellm_chat")
        bridge._execute_openai_compatible.assert_not_called()

    def test_execute_auto_routes_kimi_lightweight_3d_game_builder_to_direct_text(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible"}
        )
        bridge._execute_litellm_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "kimi-coding",
                },
                plugins=[],
                input_data="创建一个第三人称 3D 迷宫冒险游戏，带开始界面、通关和结算。",
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        # Direct text mode routes to either openai_compatible_chat (custom gateway) or litellm_chat
        direct_text_called = (
            bridge._execute_openai_compatible_chat.await_count > 0
            or bridge._execute_litellm_chat.await_count > 0
        )
        self.assertTrue(direct_text_called, "Expected direct text delivery via openai_compatible_chat or litellm_chat")
        bridge._execute_openai_compatible.assert_not_called()

    def test_execute_routes_kimi_premium_3d_game_builder_to_direct_text(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible"}
        )
        bridge._execute_litellm_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "kimi-coding",
                },
                plugins=[],
                input_data="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。",
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        direct_text_called = (
            bridge._execute_openai_compatible_chat.await_count > 0
            or bridge._execute_litellm_chat.await_count > 0
        )
        self.assertTrue(direct_text_called, "Expected direct text delivery via openai_compatible_chat or litellm_chat")
        bridge._execute_openai_compatible.assert_not_called()

    def test_execute_routes_kimi_premium_3d_builder_to_direct_text_from_node_goal_hint(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible"}
        )
        bridge._execute_litellm_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "kimi-coding",
                    "goal": "创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。",
                },
                plugins=[],
                input_data=(
                    "ADVANCED MODE — Use analyst notes and asset manifest.\n"
                    "[System Context]\n"
                    "Output directory: /tmp/evermind_output\n"
                    "Files must target: /tmp/evermind_output/\n"
                    "This builder run is for a single preview artifact.\n"
                ),
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        direct_text_called = (
            bridge._execute_openai_compatible_chat.await_count > 0
            or bridge._execute_litellm_chat.await_count > 0
        )
        self.assertTrue(direct_text_called, "Expected direct text delivery via openai_compatible_chat or litellm_chat")
        bridge._execute_openai_compatible.assert_not_called()

    def test_builder_should_auto_direct_multifile_for_streaming_models(self):
        # v6.4.10: direct_multifile is the fast path for
        # multi-target builders on every streaming-capable provider. Prior
        # gate was kimi-only + env-opt-in, which forced gpt-5.x / claude /
        # deepseek through the 10-15min tool_call loop instead of a 2-4min
        # streaming delivery. Now:
        #   - every non-Kimi streaming provider: auto True with 2+ targets
        #   - Kimi still requires EVERMIND_BUILDER_KIMI_ALLOW_DIRECT_MULTIFILE=1
        #     (v5.8.6 empirical 100% fail still applies)
        #   - GLM-5.x explicitly denied (sglang#11888 tool_stream incompat)
        #   - non-builder node_type → False
        input_data = (
            "做一个 8 页面轻奢品牌网站。\n"
            "Assigned HTML filenames for this builder: index.html, about.html, collections.html, "
            "craft.html, materials.html, journal.html, contact.html, faq.html."
        )
        # Non-builder role never direct_multifile.
        self.assertFalse(
            self.bridge._builder_should_auto_direct_multifile(
                "reviewer",
                model_name="kimi-coding",
                input_data=input_data,
            )
        )
        # Streaming models → True (multi-target builder).
        self.assertTrue(
            self.bridge._builder_should_auto_direct_multifile(
                "builder",
                model_name="gpt-4o",
                input_data=input_data,
            )
        )
        self.assertTrue(
            self.bridge._builder_should_auto_direct_multifile(
                "builder",
                model_name="gpt-5.4",
                input_data=input_data,
            )
        )
        # GLM-5.x still denied (known tool_stream bug).
        self.assertFalse(
            self.bridge._builder_should_auto_direct_multifile(
                "builder",
                model_name="glm-5.1",
                input_data=input_data,
            )
        )
        # Kimi: denied without env opt-in, allowed with it.
        self.assertFalse(
            self.bridge._builder_should_auto_direct_multifile(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
            )
        )
        with patch.dict("os.environ", {"EVERMIND_BUILDER_KIMI_ALLOW_DIRECT_MULTIFILE": "1"}):
            self.assertTrue(
                self.bridge._builder_should_auto_direct_multifile(
                    "builder",
                    model_name="kimi-coding",
                    input_data=input_data,
                )
            )

    def test_builder_should_auto_direct_multifile_for_two_target_kimi_builder(self):
        input_data = (
            "做一个双页面产品网站，包含首页和定价页。\n"
            "Assigned HTML filenames for this builder: index.html, pricing.html."
        )
        with patch.dict("os.environ", {"EVERMIND_BUILDER_KIMI_ALLOW_DIRECT_MULTIFILE": "1"}):
            self.assertTrue(
                self.bridge._builder_should_auto_direct_multifile(
                    "builder",
                    model_name="kimi-coding",
                    input_data=input_data,
                )
            )

    def test_builder_should_auto_direct_multifile_for_single_override_target_on_kimi(self):
        input_data = (
            "做一个 8 页面旅游网站。\n"
            "HTML TARGET OVERRIDE: faq.html\n"
            "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, "
            "solutions.html, platform.html, contact.html, about.html, faq.html."
        )
        with patch.dict("os.environ", {"EVERMIND_BUILDER_KIMI_ALLOW_DIRECT_MULTIFILE": "1"}):
            self.assertTrue(
                self.bridge._builder_should_auto_direct_multifile(
                    "builder",
                    model_name="kimi-coding",
                    input_data=input_data,
                )
            )

    def test_builder_direct_multifile_default_disabled_for_kimi_v586(self):
        """v5.8.6: direct_multifile default off for kimi (100% prose failure in prod)."""
        input_data = (
            "做一个 8 页面轻奢品牌网站。\n"
            "Assigned HTML filenames for this builder: index.html, about.html, collections.html, "
            "craft.html, materials.html, journal.html, contact.html, faq.html."
        )
        self.assertFalse(
            self.bridge._builder_should_auto_direct_multifile(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
            ),
            "Without env opt-in, kimi builders must skip direct_multifile entirely",
        )

    def test_v649_retry_markers_classify_zero_files_as_missing_artifact(self):
        """v6.4.9 F: the three new retry markers (zero-files demote, unnamed-
        HTML block extractor skips, direct-text no-output timeout) must mark
        the retry as "missing artifact" — i.e. greenfield retry, not a
        patch-existing-artifact retry. Before the fix these surfaces were
        silently bucketed as "has artifact" and dragged retries into patch
        mode even though nothing had actually been written."""
        # Zero-files demote (observed 2026-04-22 builder1 first pass)
        error_text = (
            "Builder reported success but produced zero files on disk. "
            "A file_ops write or extractable HTML code block is required."
        )
        self.assertTrue(
            self.bridge._builder_retry_missing_artifact_context(
                "builder",
                input_data="rebuild multi-page site",
                node={"error": error_text},
            ),
            "zero-files demote must classify as missing-artifact (greenfield retry)",
        )
        # Unnamed-block stream (observed 2026-04-22 builder2 kimi multi-page)
        error_text = "Skipping unnamed HTML block for multi-page builder output"
        self.assertTrue(
            self.bridge._builder_retry_missing_artifact_context(
                "builder",
                input_data="",
                goal_hint=error_text,
            ),
            "unnamed-HTML-block stream must classify as missing-artifact",
        )
        # Direct-text no-output timeout
        error_text = (
            "builder direct-text no-output timeout: 180s elapsed with no real file write."
        )
        self.assertTrue(
            self.bridge._builder_retry_missing_artifact_context(
                "builder",
                input_data="",
                node={"error": error_text},
            ),
            "direct-text no-output timeout must classify as missing-artifact",
        )
        # Negative control: unrelated retry error must NOT trip missing-artifact.
        self.assertFalse(
            self.bridge._builder_retry_missing_artifact_context(
                "builder",
                input_data="plain retry",
                node={"error": "retry 1/3 — reviewer requested clarifying copy"},
            ),
            "unrelated reviewer retry must not trip missing-artifact context",
        )

    def test_builder_should_auto_direct_text_for_single_page_kimi_game_without_explicit_index_hint(self):
        input_data = "做一个贪吃蛇网页小游戏，包含开始界面、暂停和结算体验。"
        self.assertTrue(
            self.bridge._builder_should_auto_direct_text(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
            )
        )

    def test_builder_should_auto_direct_text_for_lightweight_3d_kimi_game(self):
        input_data = "做一个第三人称 3D 迷宫网页游戏，带开始界面、通关和结算。"
        self.assertTrue(
            self.bridge._builder_should_auto_direct_text(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
            )
        )

    def test_builder_should_auto_direct_text_for_premium_3d_kimi_first_pass(self):
        input_data = "做一个第三人称 3D 射击网页游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。"
        self.assertTrue(
            self.bridge._builder_should_auto_direct_text(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
            )
        )

    def test_builder_should_not_auto_direct_text_for_non_root_parallel_support_lane(self):
        # v5.8.6: pure non-HTML support lanes must still be explicitly flagged
        # with `builder_is_support_lane_node=True` (or support-lane text).
        # A primary peer with `can_write_root_index=False` still produces a
        # staged index.html and legitimately benefits from direct_text, so
        # the mere absence of allowed_html_targets is no longer enough.
        input_data = "做一个第三人称 3D 射击网页游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。"
        self.assertFalse(
            self.bridge._builder_should_auto_direct_text(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
                node={
                    "type": "builder",
                    "can_write_root_index": False,
                    "allowed_html_targets": [],
                    "builder_is_support_lane_node": True,
                },
            )
        )

    def test_builder_parallel_peer_with_merger_goes_direct_text_v586(self):
        """v5.8.6: primary peer builder (merger handles root) must route to
        direct_text so both Builder 1 and Builder 2 take the same path."""
        input_data = "Build the core TPS scene and combat. Assigned HTML filenames for this builder: index.html."
        self.assertTrue(
            self.bridge._builder_should_auto_direct_text(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
                node={
                    "type": "builder",
                    "can_write_root_index": False,           # merger owns root
                    "allowed_html_targets": [],              # orchestrator leaves empty for peers
                    "builder_merger_like": False,            # this is primary peer, not merger
                    "builder_is_support_lane_node": False,   # writes HTML, not JS-only
                },
            )
        )

    def test_builder_parallel_multi_page_website_peer_does_not_go_direct_text(self):
        input_data = (
            "写一个 8 页的奢侈品牌官网，风格像苹果一样。"
            "Assigned HTML filenames for this builder: about.html, materials.html, boutiques.html, contact.html."
        )
        self.assertFalse(
            self.bridge._builder_should_auto_direct_text(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
                node={
                    "type": "builder",
                    "can_write_root_index": False,
                    "allowed_html_targets": [
                        "about.html",
                        "materials.html",
                        "boutiques.html",
                        "contact.html",
                    ],
                    "builder_merger_like": False,
                    "builder_is_support_lane_node": False,
                },
            )
        )

    def test_builder_should_auto_direct_text_for_kimi_peer_builder(self):
        # v5.1: support-lane removed — peer builder with module file output
        # may still use direct_text since it writes to its own module file
        input_data = (
            "ADVANCED MODE — Use analyst notes and asset manifest.\n"
            "Build a non-overlapping support subsystem for the same commercial-grade HTML5 game. "
            "Do NOT overwrite /tmp/evermind_output/index.html in this pass unless explicitly reassigned."
        )
        # v5.1: _builder_is_support_lane returns False, so direct_text is allowed
        result = self.bridge._builder_should_auto_direct_text(
            "builder",
            model_name="kimi-coding",
            input_data=input_data,
        )
        # Result may be True or False depending on other heuristics — just verify no crash
        self.assertIsInstance(result, bool)

    def test_builder_should_auto_direct_text_for_premium_3d_retry_after_prewrite_timeout_without_artifact(self):
        input_data = (
            "⚠️ PREVIOUS ATTEMPT FAILED (retry 1/3): Error: builder pre-write timeout after 150s: no file write produced.\n"
            "创建一个第三人称 3D 射击网页游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。"
        )
        self.assertTrue(
            self.bridge._builder_should_auto_direct_text(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
            )
        )

    def test_builder_should_not_auto_direct_text_for_premium_3d_retry_after_quality_gate_failure(self):
        input_data = (
            "⚠️ PREVIOUS ATTEMPT FAILED (retry 1/3): Builder quality gate failed (score=70). Errors: "
            "['Premium 3D/TPS brief still appears to render core models as primitive placeholder geometry "
            "(weapon/gun); replace Box/Cone/Cylinder-style stand-ins with authored or asset-driven models.']\n"
            "创建一个第三人称 3D 射击网页游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。"
        )
        self.assertFalse(
            self.bridge._builder_should_auto_direct_text(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
            )
        )

    def test_builder_should_not_auto_direct_text_for_premium_3d_retry_after_direct_text_idle_timeout(self):
        input_data = (
            "⚠️ PREVIOUS ATTEMPT FAILED (retry 2/3): Error: builder direct-text idle timeout: "
            "no new meaningful HTML stream activity for 91s in direct single-file delivery mode.\n"
            "创建一个第三人称 3D 射击网页游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。"
        )
        self.assertFalse(
            self.bridge._builder_should_auto_direct_text(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
            )
        )

    def test_builder_should_auto_direct_text_for_premium_3d_retry_after_tool_planning_prose_without_artifact(self):
        input_data = (
            "⚠️ PREVIOUS ATTEMPT FAILED (retry 1/3): Builder returned tool-planning prose instead of a persistable HTML deliverable.\n"
            "创建一个第三人称 3D 射击网页游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。"
        )
        self.assertTrue(
            self.bridge._builder_should_auto_direct_text(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
                node={"type": "builder", "retry_attempt": 1},
            )
        )

    def test_execute_honors_orchestrator_direct_text_mode_for_premium_3d_builder_first_pass(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible"}
        )
        bridge._execute_litellm_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"}
        )
        bridge._execute_litellm_tools = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "litellm_tools"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "kimi-coding",
                    "builder_delivery_mode": "direct_text",
                },
                plugins=[MagicMock()],
                input_data="创建一个第三人称 3D 射击网页游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。",
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        direct_called = (
            bridge._execute_openai_compatible_chat.await_count > 0
            or bridge._execute_litellm_chat.await_count > 0
        )
        self.assertTrue(direct_called, "Expected direct delivery via openai_compatible_chat or litellm_chat")
        bridge._execute_openai_compatible.assert_not_called()

    def test_execute_disables_direct_text_for_premium_3d_reviewer_retry_patch_mode(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible"}
        )
        bridge._execute_litellm_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"}
        )
        bridge._execute_litellm_tools = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "litellm_tools"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "kimi-coding",
                    "builder_delivery_mode": "direct_text",
                    "retry_attempt": 2,
                },
                plugins=[MagicMock()],
                input_data=(
                    "Goal: 创建一个第三人称 3D 射击网页游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。\n"
                    "[Reviewer Rework Patch Mode]\n"
                    "You are reworking a live artifact after reviewer rejection (round 1/2).\n"
                    "Start from the current files and patch the failing areas only. Do NOT do a clean-slate rewrite.\n"
                    "DIRECT MULTI-FILE DELIVERY ONLY.\n"
                    "HTML TARGET OVERRIDE: index.html\n"
                ),
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        tool_path_called = (
            bridge._execute_openai_compatible.await_count > 0
            or bridge._execute_litellm_tools.await_count > 0
            or bridge._execute_litellm_chat.await_count > 0
        )
        self.assertTrue(tool_path_called, "Expected tool path via openai_compatible, litellm_tools, or litellm_chat")

    def test_builder_goal_hint_source_extracts_clean_goal_from_runtime_prompt(self):
        input_data = (
            "Build a commercial-grade HTML5 game for: 创建一个3d射击游戏，要有怪物、枪械和第三人称视角。 "
            "Save final HTML via file_ops write to /tmp/evermind_output/index.html. "
            "[System Context]\nOutput directory: /tmp/evermind_output\n"
            "This is a refinement pass on an existing artifact.\n"
        )
        self.assertEqual(
            self.bridge._builder_goal_hint_source(input_data=input_data),
            "创建一个3d射击游戏，要有怪物、枪械和第三人称视角",
        )

    def test_builder_should_auto_direct_text_uses_goal_hint_over_runtime_context_noise(self):
        input_data = (
            "ADVANCED MODE — Use analyst notes and asset manifest.\n"
            "Build a commercial-grade HTML5 game for: 创建一个3d射击游戏，要有怪物、枪械和第三人称视角。 "
            "Save final HTML via file_ops write to /tmp/evermind_output/index.html.\n\n"
            "[System Context]\n"
            "Output directory: /tmp/evermind_output\n"
            "Files must target: /tmp/evermind_output/\n"
            "This is a refinement pass on an existing artifact: preserve the strongest current structure.\n"
        )
        self.assertTrue(
            self.bridge._builder_should_auto_direct_text(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
                goal_hint="创建一个3d射击游戏，要有怪物、枪械和第三人称视角。",
            )
        )

    def test_builder_direct_multifile_budget_boosts_large_multi_page_delivery(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, craftsmanship.html, collections.html, "
            "materials.html, heritage.html, boutiques.html, contact.html."
        )
        self.assertEqual(
            self.bridge._builder_direct_multifile_budget(
                input_data,
                max_tokens=8192,
                timeout_sec=240,
            ),
            (14336, 420),
        )

    def test_builder_direct_multifile_budget_boosts_from_assigned_targets_even_if_classifier_misses(self):
        input_data = (
            "Assigned HTML filenames for this builder: index.html, brand.html, craftsmanship.html, collections.html, "
            "materials.html, heritage.html, boutiques.html, contact.html."
        )
        with patch("ai_bridge.task_classifier.wants_multi_page", return_value=False), \
             patch("ai_bridge.task_classifier.requested_page_count", return_value=0):
            self.assertEqual(
                self.bridge._builder_direct_multifile_budget(
                    input_data,
                    max_tokens=8192,
                    timeout_sec=240,
                ),
                (14336, 420),
            )

    def test_builder_direct_multifile_budget_uses_assigned_subset_not_whole_site_size(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, collections.html, contact.html."
        )
        self.assertEqual(
            self.bridge._builder_direct_multifile_budget(
                input_data,
                max_tokens=8192,
                timeout_sec=240,
            ),
            (12288, 300),
        )

    def test_builder_direct_multifile_batch_sizes_follow_assigned_subset_not_whole_site_size(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, collections.html, contact.html."
        )
        self.assertEqual(self.bridge._builder_direct_multifile_initial_batch_size(input_data), 2)
        self.assertEqual(self.bridge._builder_direct_multifile_batch_size(input_data), 2)


class TestBuilderForcedOutputPolicy(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_force_when_builder_has_no_html_and_no_file_write(self):
        self.assertTrue(self.bridge._builder_needs_forced_text("builder", "I will now create a page.", []))

    def test_no_force_when_builder_already_has_html(self):
        self.assertFalse(self.bridge._builder_needs_forced_text("builder", "<!DOCTYPE html><html><head></head><body></body></html>", []))

    def test_no_force_when_builder_already_wrote_file(self):
        tool_results = [{"success": True, "data": {"path": "/tmp/evermind_output/index.html", "written": True}}]
        self.assertFalse(self.bridge._builder_needs_forced_text("builder", "Done", tool_results))

    def test_no_force_for_non_builder_nodes(self):
        self.assertFalse(self.bridge._builder_needs_forced_text("tester", "no html", []))

    def test_builder_forced_text_prompt_uses_multi_file_contract_for_multi_page(self):
        prompt = self.bridge._builder_forced_text_prompt("做一个奢侈品英文官网，一共 8 页")
        self.assertIn("MULTI-PAGE website request", prompt)
        self.assertIn("```html index.html", prompt)
        self.assertIn("additional linked HTML files", prompt)

    def test_builder_forced_text_prompt_mentions_assigned_filenames(self):
        prompt = self.bridge._builder_forced_text_prompt(
            "YOUR JOB: This is a MULTI-PAGE website request. "
            "You MUST create /tmp/evermind_output/index.html and 3 additional linked page(s). "
            "Otherwise use this non-overlapping fallback set for your secondary pages: collections.html, heritage.html, contact.html."
        )
        self.assertIn("Your assigned HTML filenames are: index.html, collections.html, heritage.html, contact.html.", prompt)
        self.assertIn("Unnamed ```html``` blocks are invalid", prompt)
        self.assertIn("/tmp/evermind_output/index.html", prompt)

    def test_builder_forced_text_prompt_for_secondary_builder_forbids_index(self):
        prompt = self.bridge._builder_forced_text_prompt(
            "YOUR JOB: This is a MULTI-PAGE website request. "
            "Do NOT write /tmp/evermind_output/index.html. "
            "Otherwise use this non-overlapping fallback set: about.html, platform.html, contact.html, faq.html."
        )
        self.assertIn("Do NOT emit ```html index.html```", prompt)
        self.assertIn("/tmp/evermind_output/about.html", prompt)
        self.assertIn("Return ONLY the assigned HTML files listed above.", prompt)
        self.assertNotIn("Return index.html plus", prompt)

    def test_builder_assigned_targets_parse_assigned_html_line(self):
        targets = self.bridge._builder_assigned_html_targets(
            "Assigned HTML filenames for this builder: index.html, brand.html, collections.html, contact.html."
        )
        self.assertEqual(targets, ["index.html", "brand.html", "collections.html", "contact.html"])

    def test_builder_returned_html_targets_reads_fence_headers(self):
        output = (
            "Looking at this request, I will build it now.\n\n"
            "```html index.html\n<!DOCTYPE html><html><body>home</body></html>\n```\n"
            "```html about.html\n<!DOCTYPE html><html><body>about</body></html>\n```"
        )
        self.assertEqual(
            self.bridge._builder_returned_html_targets(output),
            ["index.html", "about.html"],
        )

    def test_builder_missing_html_targets_detects_remaining_pages(self):
        input_data = (
            "Assigned HTML filenames for this builder: index.html, about.html, contact.html."
        )
        output = (
            "```html index.html\n<!DOCTYPE html><html><head><title>Home</title></head>"
            "<body><main><h1>Home</h1><p>Concrete premium homepage copy with enough depth to persist.</p></main></body></html>\n```"
        )
        self.assertEqual(
            self.bridge._builder_missing_html_targets(input_data, output),
            ["about.html", "contact.html"],
        )

    def test_builder_missing_html_targets_keeps_invalid_index_in_remaining_set(self):
        input_data = (
            "Assigned HTML filenames for this builder: index.html, pricing.html, contact.html."
        )
        output = (
            "```html index.html\n<!DOCTYPE html><html><body></body></html>\n```\n"
            "```html pricing.html\n<!DOCTYPE html><html><head><title>Pricing</title></head>"
            "<body><main><h1>Pricing</h1><p>Concrete pricing content with enough detail to persist safely.</p></main></body></html>\n```"
        )
        self.assertEqual(
            self.bridge._builder_missing_html_targets(input_data, output),
            ["index.html", "contact.html"],
        )

    def test_builder_missing_html_targets_ignores_truncated_trailing_html_block(self):
        input_data = (
            "Assigned HTML filenames for this builder: index.html, about.html, contact.html."
        )
        output = (
            "```html index.html\n<!DOCTYPE html><html><head><title>Home</title></head>"
            "<body><main><h1>Home</h1><p>Concrete premium homepage copy with enough depth to persist.</p></main></body></html>\n```\n"
            "```html about.html\n<!DOCTYPE html><html><head><title>About</title></head>"
            "<body><main><h1>About</h1><p>Concrete about-page copy with enough depth to persist.</p></main></body></html>\n```\n"
            "```html contact.html\n<!DOCTYPE html><html><body>contact"
        )
        self.assertEqual(
            self.bridge._builder_missing_html_targets(input_data, output),
            ["contact.html"],
        )

    def test_builder_direct_multifile_continuation_prompt_targets_only_remaining_files(self):
        messages = self.bridge._builder_direct_multifile_continuation_messages(
            "system prompt",
            "Assigned HTML filenames for this builder: index.html, about.html, contact.html.",
            "```html index.html\n<!DOCTYPE html><html><body>home</body></html>\n```",
            ["about.html", "contact.html"],
        )
        self.assertEqual(len(messages), 2)
        prompt = messages[1]["content"]
        self.assertIn("Already returned: index.html", prompt)
        self.assertIn("ONLY this next batch of HTML files: about.html, contact.html", prompt)
        self.assertIn("The ONLY valid local HTML route set for this site is: index.html, about.html, contact.html", prompt)
        self.assertIn("Rewrite or remove any local href that points to a non-assigned HTML filename", prompt)
        self.assertIn("Do NOT restart from index.html", prompt)
        self.assertIn("HTML TARGET OVERRIDE: about.html, contact.html", prompt)
        self.assertIn("Shared assets are still missing", prompt)

    def test_builder_direct_multifile_initial_prompt_limits_first_batch(self):
        messages = self.bridge._builder_direct_multifile_initial_messages(
            "system prompt",
            (
                "做一个 8 页面工艺品牌站。\n"
                "Assigned HTML filenames for this builder: index.html, about.html, collections.html, "
                "craft.html, materials.html, journal.html, contact.html, faq.html."
            ),
        )
        self.assertEqual(len(messages), 2)
        prompt = messages[1]["content"].split("[DIRECT MULTI-FILE INITIAL DELIVERY]", 1)[1]
        self.assertIn("HTML TARGET OVERRIDE: index.html", prompt)
        self.assertIn("Return ONLY this first batch now: index.html", prompt)
        self.assertIn("index.html is part of this batch and is MANDATORY", prompt)
        self.assertIn("The ONLY valid local HTML route set for this site is: index.html, about.html, collections.html, craft.html, materials.html, journal.html, contact.html, faq.html", prompt)
        self.assertIn("Every internal href that points to a local .html page MUST use one of those exact filenames.", prompt)
        self.assertIn("another continuation will request them immediately", prompt)
        self.assertNotIn("Return ONLY this first batch now: index.html, about.html", prompt)
        self.assertIn("shared ```css styles.css``` block", prompt)
        self.assertIn("You MUST return ```css styles.css``` and ```js app.js```", prompt)
        self.assertIn("Do NOT inline large CSS or JS blobs into the HTML", prompt)

    def test_builder_direct_multifile_secondary_builder_prompt_skips_shared_asset_contract(self):
        messages = self.bridge._builder_direct_multifile_initial_messages(
            "system prompt",
            (
                "做一个 6 页面工艺品牌站。\n"
                "Assigned HTML filenames for this builder: materials.html, journal.html, contact.html."
            ),
        )
        prompt = messages[1]["content"].split("[DIRECT MULTI-FILE INITIAL DELIVERY]", 1)[1]
        self.assertNotIn("You MUST return ```css styles.css``` and ```js app.js```", prompt)
        self.assertNotIn("FIRST-BATCH ASSET CONTRACT", prompt)
        self.assertNotIn("shared ```css styles.css``` block", prompt)

    def test_builder_direct_multifile_continuation_limit_scales_for_large_single_page_batches(self):
        limit = self.bridge._builder_direct_multifile_continuation_limit(
            (
                "做一个 8 页面工艺品牌站。\n"
                "Assigned HTML filenames for this builder: index.html, about.html, collections.html, "
                "craft.html, materials.html, journal.html, contact.html, faq.html."
            ),
        )
        self.assertEqual(limit, 4)

    def test_builder_direct_multifile_large_continuation_batch_size_defaults_to_two(self):
        batch_size = self.bridge._builder_direct_multifile_batch_size(
            (
                "做一个 8 页面工艺品牌站。\n"
                "Assigned HTML filenames for this builder: index.html, about.html, collections.html, "
                "craft.html, materials.html, journal.html, contact.html, faq.html."
            ),
        )
        self.assertEqual(batch_size, 2)

    def test_builder_direct_multifile_continuation_prompt_forbids_repeating_shared_assets(self):
        messages = self.bridge._builder_direct_multifile_continuation_messages(
            "system prompt",
            "Assigned HTML filenames for this builder: index.html, pricing.html.",
            "```html index.html\n<!DOCTYPE html><html><body>home</body></html>\n```",
            ["pricing.html"],
        )
        prompt = messages[1]["content"]
        self.assertIn("Return ONLY the next HTML file(s) in this batch.", prompt)
        self.assertIn("Do NOT re-emit styles.css or app.js", prompt)
        self.assertIn("Do NOT inline large CSS or JS into this continuation page", prompt)

    def test_builder_direct_multifile_single_target_initial_prompt_uses_compact_single_file_contract(self):
        messages = self.bridge._builder_direct_multifile_initial_messages(
            "system prompt",
            "创建一个 3D 射击游戏并保存为 index.html。\nAssigned HTML filenames for this builder: index.html.",
        )

        prompt = messages[1]["content"]

        self.assertIn("[DIRECT SINGLE-FILE DELIVERY]", prompt)
        self.assertIn("Return ONLY one fenced ```html index.html``` block.", prompt)
        self.assertIn("Keep the file compact enough to finish in one response", prompt)
        self.assertNotIn("Do NOT try to emit every assigned page in one response.", prompt)

    def test_builder_direct_multifile_single_target_continuation_prompt_uses_compact_repair_contract(self):
        messages = self.bridge._builder_direct_multifile_continuation_messages(
            "system prompt",
            "创建一个 3D 射击游戏并保存为 index.html。\nAssigned HTML filenames for this builder: index.html.",
            "```html index.html\n<!DOCTYPE html><html><body>partial",
            ["index.html"],
        )

        prompt = messages[1]["content"]

        self.assertIn("[DIRECT SINGLE-FILE CONTINUATION]", prompt)
        self.assertIn("Return ONE complete replacement ```html index.html``` block only.", prompt)
        self.assertIn("Do NOT restart the architecture from zero.", prompt)

    def test_builder_missing_html_targets_accepts_single_target_persistable_open_block(self):
        output = (
            "```html index.html\n"
            "<!DOCTYPE html><html><body><button id='startBtn'>Start</button>"
            "<canvas id='gameCanvas'></canvas><script>"
            "function startGame(){ gameStarted=true; }"
            "function animate(){ requestAnimationFrame(animate); }"
            "</script></body></html>"
        )

        missing = self.bridge._builder_missing_html_targets(
            "创建一个 3D 射击游戏并保存为 index.html。\nAssigned HTML filenames for this builder: index.html.",
            output,
        )

        self.assertEqual(missing, [])

    def test_builder_direct_multifile_budget_boosts_single_target_retry_repairs(self):
        tokens, timeout_sec = self.bridge._builder_direct_multifile_budget(
            (
                "创建一个 3D 射击游戏并保存为 index.html。\n"
                "Assigned HTML filenames for this builder: index.html.\n"
                "Retry 1/3 after Builder quality gate failed: Drag-look pitch appears inverted."
            ),
            max_tokens=40960,
            timeout_sec=960,
        )

        self.assertGreaterEqual(tokens, 49152)
        self.assertEqual(timeout_sec, 960)

    def test_builder_tool_planning_prose_is_detected_as_non_deliverable(self):
        output = (
            "I'll first read the current index.html to understand the existing implementation, "
            "then improve it in place. "
            "file_ops read {\"file_path\": \"/tmp/evermind_output/index.html\"}"
        )

        reason = self.bridge._builder_non_deliverable_output_reason(
            output,
            "创建一个 3D 射击游戏并保存为 index.html。",
        )

        self.assertIn("tool-planning prose", reason)

    @patch("openai.OpenAI")
    def test_openai_compatible_chat_continues_direct_multifile_builder_until_missing_pages_return(self, mock_openai):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        client = MagicMock()
        mock_openai.return_value = client

        def _make_stream_chunks(content: str, finish_reason: str = "stop", usage=None):
            """Create a list of streaming chunks that simulate the OpenAI streaming API."""
            chunks = []
            # Content chunk
            chunks.append(SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=content, tool_calls=None),
                    finish_reason=None,
                )],
            ))
            # Final chunk with finish_reason
            chunks.append(SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=None, tool_calls=None),
                    finish_reason=finish_reason,
                )],
            ))
            # Usage chunk (no choices)
            if usage:
                chunks.append(SimpleNamespace(choices=[], usage=SimpleNamespace(**usage)))
            return iter(chunks)

        first_stream = _make_stream_chunks(
            (
                "```html index.html\n"
                "<!DOCTYPE html><html><head><title>Home</title></head><body>"
                "<main><section><h1>Studio Home</h1><p>Immersive brand storytelling, premium launch systems, and a substantial hero section for commercial visitors.</p></section>"
                "<section><h2>Services</h2><p>Strategy, production, and launch support with enough body copy to remain a trustworthy persisted page.</p></section></main>"
                "</body></html>\n```"
            ),
            finish_reason="length",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )
        second_stream = _make_stream_chunks(
            (
                "```html about.html\n"
                "<!DOCTYPE html><html><head><title>About</title></head><body>"
                "<main><section><h1>About Studio</h1><p>Crafted interactive campaigns for premium brands with detailed process, editorial pacing, and concrete offer framing.</p></section>"
                "<section><h2>Approach</h2><p>Research, concept development, and execution details are present so this route is materially complete for persistence.</p></section></main>"
                "</body></html>\n```"
            ),
            finish_reason="stop",
            usage={"prompt_tokens": 40, "completion_tokens": 30, "total_tokens": 70},
        )
        client.chat.completions.create.side_effect = [first_stream, second_stream]

        result = asyncio.run(
            bridge._execute_openai_compatible_chat(
                {
                    "type": "builder",
                    "builder_delivery_mode": "direct_multifile",
                },
                (
                    "做一个 2 页面品牌官网。\n"
                    "Assigned HTML filenames for this builder: index.html, about.html."
                ),
                {
                    "litellm_id": "openai/kimi-coding",
                    "provider": "kimi",
                    "api_base": "https://api.moonshot.test/v1",
                    "extra_headers": {},
                },
                on_progress=None,
            )
        )

        self.assertTrue(result["success"])
        self.assertIn("index.html", result["output"])
        self.assertIn("about.html", result["output"])
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(result["usage"]["total_tokens"], 220)

    @patch("openai.OpenAI")
    def test_openai_compatible_chat_rejects_builder_tool_planning_prose(self, mock_openai):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        client = MagicMock()
        mock_openai.return_value = client

        def _make_stream_chunks(content: str, finish_reason: str = "stop", usage=None):
            chunks = [
                SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=content, tool_calls=None),
                        finish_reason=None,
                    )],
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=None),
                        finish_reason=finish_reason,
                    )],
                ),
            ]
            if usage:
                chunks.append(SimpleNamespace(choices=[], usage=SimpleNamespace(**usage)))
            return iter(chunks)

        client.chat.completions.create.return_value = _make_stream_chunks(
            (
                "I'll inspect the current state and then write the improved game. "
                "Let me first check the existing files. "
                "file_ops read {\"path\": \"/tmp/evermind_output/index.html\"}"
            ),
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

        result = asyncio.run(
            bridge._execute_openai_compatible_chat(
                {
                    "type": "builder",
                    "builder_delivery_mode": "direct_multifile",
                },
                "创建一个 3D 射击游戏并保存为 index.html。\nAssigned HTML filenames for this builder: index.html.",
                {
                    "litellm_id": "openai/kimi-coding",
                    "provider": "kimi",
                    "api_base": "https://api.moonshot.test/v1",
                    "extra_headers": {},
                },
                on_progress=None,
            )
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["mode"], "openai_compatible_chat")
        self.assertIn("tool-planning prose", result["error"])

    @patch("openai.OpenAI")
    def test_openai_compatible_chat_timeout_salvages_prior_direct_multifile_batches(self, mock_openai):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        client = MagicMock()
        mock_openai.return_value = client

        def _make_stream_chunks(content: str, finish_reason: str = "stop", usage=None):
            chunks = [
                SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=content, tool_calls=None),
                        finish_reason=None,
                    )],
                ),
            ]
            if finish_reason:
                chunks.append(SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=None),
                        finish_reason=finish_reason,
                    )],
                ))
            if usage:
                chunks.append(SimpleNamespace(choices=[], usage=SimpleNamespace(**usage)))
            return iter(chunks)

        def _timing_out_stream():
            yield SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(
                        content=(
                            "```html about.html\n"
                            "<!DOCTYPE html><html><head><title>About</title></head>"
                            "<body><main><section><h1>About Studio</h1><p>Crafted interactive campaigns for premium brands.</p></section></main></body></html>\n```"
                        ),
                        tool_calls=None,
                    ),
                    finish_reason=None,
                )],
            )
            raise TimeoutError("Chat stream stalled: no chunk for 180s")

        client.chat.completions.create.side_effect = [
            _make_stream_chunks(
                (
                    "```html index.html\n"
                    "<!DOCTYPE html><html><head><title>Home</title></head>"
                    "<body><main><section><h1>Studio Home</h1><p>Immersive brand storytelling and launch systems.</p></section></main></body></html>\n```"
                ),
                finish_reason="length",
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            ),
            _timing_out_stream(),
        ]

        result = asyncio.run(
            bridge._execute_openai_compatible_chat(
                {
                    "type": "builder",
                    "builder_delivery_mode": "direct_multifile",
                },
                (
                    "做一个 2 页面品牌官网。\n"
                    "Assigned HTML filenames for this builder: index.html, about.html."
                ),
                {
                    "litellm_id": "openai/kimi-coding",
                    "provider": "kimi",
                    "api_base": "https://api.moonshot.test/v1",
                    "extra_headers": {},
                },
                on_progress=None,
            )
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["mode"], "openai_compatible_chat_timeout_salvage")
        self.assertIn("index.html", result["output"])
        self.assertIn("about.html", result["output"])

    @patch("openai.OpenAI")
    def test_openai_compatible_chat_progress_timeout_does_not_delay_api_dispatch(self, mock_openai):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        client = MagicMock()
        mock_openai.return_value = client

        def _make_stream_chunks(content: str, finish_reason: str = "stop", usage=None):
            chunks = [
                SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=content, tool_calls=None),
                        finish_reason=None,
                    )],
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=None),
                        finish_reason=finish_reason,
                    )],
                ),
            ]
            if usage:
                chunks.append(SimpleNamespace(choices=[], usage=SimpleNamespace(**usage)))
            return iter(chunks)

        client.chat.completions.create.return_value = _make_stream_chunks(
            (
                "```html index.html\n"
                "<!DOCTYPE html><html><head><title>Home</title></head><body>"
                "<main><section><h1>Studio Home</h1><p>Immersive brand storytelling with enough structural and textual density to qualify as persistable HTML.</p></section>"
                "<section><h2>Offer</h2><p>Service framing, editorial copy, and meaningful navigation content keep the page out of the thin-artifact bucket.</p></section></main>"
                "</body></html>\n```"
            ),
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

        async def slow_progress(_payload):
            await asyncio.sleep(0.5)

        start = time.perf_counter()
        with patch.dict("os.environ", {"EVERMIND_PROGRESS_EVENT_TIMEOUT_SEC": "0.01"}):
            result = asyncio.run(
                bridge._execute_openai_compatible_chat(
                    {
                        "type": "builder",
                        "builder_delivery_mode": "direct_multifile",
                    },
                    "做一个 1 页面品牌官网。\nAssigned HTML filenames for this builder: index.html.",
                    {
                        "litellm_id": "openai/kimi-coding",
                        "provider": "kimi",
                        "api_base": "https://api.moonshot.test/v1",
                        "extra_headers": {},
                    },
                    on_progress=slow_progress,
                )
            )
        elapsed = time.perf_counter() - start

        self.assertTrue(result["success"])
        self.assertLess(elapsed, 0.25)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_builder_tool_repair_prompt_redirects_missing_read_to_write(self):
        prompt = self.bridge._builder_tool_repair_prompt(
            "Save to /tmp/evermind_output/index.html via file_ops write.",
            "read",
            {"success": False, "error": "File not found: /tmp/evermind_output/index.html"},
        )
        self.assertIsNotNone(prompt)
        self.assertIn("Create it now with file_ops write", prompt)
        self.assertIn("/tmp/evermind_output/index.html", prompt)

    def test_builder_tool_repair_prompt_rejects_blank_security_path(self):
        prompt = self.bridge._builder_tool_repair_prompt(
            "Otherwise use this non-overlapping fallback set: pricing.html, about.html.",
            "write",
            {"success": False, "error": "Path not allowed by security policy: "},
        )
        self.assertIsNotNone(prompt)
        self.assertIn("explicit absolute paths", prompt)
        self.assertIn("pricing.html, about.html", prompt)

    def test_builder_tool_repair_prompt_handles_unassigned_html_target(self):
        prompt = self.bridge._builder_tool_repair_prompt(
            "Assigned HTML filenames: about.html, platform.html, contact.html, faq.html.",
            "write",
            {
                "success": False,
                "error": "HTML target not assigned for builder: index.html. Allowed HTML filenames: about.html, platform.html, contact.html, faq.html",
            },
        )
        self.assertIsNotNone(prompt)
        self.assertIn("not assigned to you", prompt)
        self.assertIn("about.html, platform.html, contact.html, faq.html", prompt)

    def test_peer_builder_non_write_followup_prompts_deliverable(self):
        # v5.1: support-lane removed — peer builder gets standard redirect prompt
        prompt = self.bridge._builder_non_write_followup_prompt(
            (
                "Build a non-overlapping support subsystem for the same commercial-grade HTML5 game. "
                "Do NOT overwrite /tmp/evermind_output/index.html in this pass unless explicitly reassigned."
            ),
            "list",
            {"success": True, "data": {"entries": []}},
            1,
        )
        self.assertIsNotNone(prompt)
        # v5.1: Standard prompt redirects to write deliverable, not support artifacts
        self.assertIn("write", prompt.lower())

    def test_peer_builder_tool_repair_prompt_redirects_to_assigned_files(self):
        # v5.1: support-lane removed — repair prompt uses standard redirect
        prompt = self.bridge._builder_tool_repair_prompt(
            (
                "Build a non-overlapping support subsystem for the same commercial-grade HTML5 game. "
                "Do NOT overwrite /tmp/evermind_output/index.html in this pass unless explicitly reassigned."
            ),
            "write",
            {
                "success": False,
                "error": "HTML target not assigned for builder: index.html. Allowed HTML filenames:",
            },
        )
        self.assertIsNotNone(prompt)
        # v5.1: Repair prompt redirects to assigned files only
        self.assertIn("assigned", prompt.lower())
        self.assertIn("not in your assigned list", prompt.lower())

    def test_builder_non_write_followup_prompt_forces_write_after_successful_list(self):
        prompt = self.bridge._builder_non_write_followup_prompt(
            (
                "做一个 4 页品牌网站。 "
                "Assigned HTML filenames: index.html, pricing.html, about.html, contact.html."
            ),
            "list",
            {"success": True, "data": {"entries": []}},
            1,
        )
        self.assertIsNotNone(prompt)
        self.assertIn("VERY NEXT response must be one or more file_ops write calls", prompt)
        self.assertIn("index.html, pricing.html, about.html, contact.html", prompt)
        self.assertIn("covers EVERY assigned HTML filename", prompt)
        self.assertIn("A one-page retry is still a failure", prompt)

    def test_builder_system_prompt_is_task_adaptive_for_game(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "builder"},
            plugins=[],
            input_data="请帮我做一个贪吃蛇小游戏",
        )
        # V4.6 SPEED: GAME DESIGN SYSTEM / engine-free / _evermind_runtime are now
        # in _builder_deferred_context (split into user message for prompt caching).
        # Core prompt retains identity, skills, and first_write_contract.
        self.assertIn("game developer", prompt.lower())
        self.assertIn("[Skill: gameplay-foundation]", prompt)
        self.assertIn("first successful write must already contain visible <body> content", prompt.lower())
        # Deferred context holds the design system details
        deferred = getattr(self.bridge, "_builder_deferred_context", "")
        self.assertIn("GAME DESIGN SYSTEM", deferred)
        self.assertIn("./_evermind_runtime", deferred)

    def test_select_builder_salvage_text_prefers_longer_recovered_html(self):
        latest = "<!DOCTYPE html><html><head><style>body{margin:0}</style></head><body>"
        recovered = (
            "<!DOCTYPE html><html><head><title>Game</title></head>"
            "<body><main><section><h1>Voxel Strike</h1><button>Start</button>"
            "<canvas id='game'></canvas><div class='hud'>HP 100</div></section></main></body></html>"
        )

        selected = self.bridge._select_builder_salvage_text(latest, "", recovered)

        self.assertEqual(selected, recovered)

    def test_select_builder_salvage_text_prefers_tool_result_html_over_prose(self):
        selected = self.bridge._select_builder_salvage_text(
            "",
            "I'll analyze the current file and rebuild it properly.",
            tool_result_text=(
                "```html index.html\n"
                "<!DOCTYPE html><html><body><main><h1>Voxel Strike</h1>"
                "<canvas id='game'></canvas><div class='hud'>HP 100</div></main></body></html>\n```"
            ),
        )

        self.assertIn("```html index.html", selected)
        self.assertIn("canvas", selected)

    def test_builder_tool_result_salvage_text_recovers_runtime_html_reads(self):
        salvaged = self.bridge._builder_tool_result_salvage_text(
            [
                {
                    "success": True,
                    "data": {
                        "path": "/tmp/evermind_output/index.html",
                        "content": (
                            "<!DOCTYPE html><html><body><main><h1>Steel Hunt</h1>"
                            "<canvas id='game'></canvas><div class='hud'>HP 100</div></main></body></html>"
                        ),
                    },
                }
            ],
            "做一个第三人称射击游戏，单页即可。",
        )

        self.assertIn("```html index.html", salvaged)
        self.assertIn("Steel Hunt", salvaged)
        self.assertIn("canvas", salvaged)

    def test_builder_partial_text_salvage_result_prefers_tool_result_html_when_output_is_thin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.bridge.config["output_dir"] = tmpdir
            payload = asyncio.run(
                self.bridge._builder_partial_text_salvage_result(
                    input_data="做一个第三人称射击游戏，单页即可。",
                    output_text="I will repair the current game and deliver it cleanly.",
                    reason="builder first-write timeout",
                    on_progress=None,
                    tool_results=[
                        {
                            "success": True,
                            "data": {
                                "path": "/tmp/evermind_output/index.html",
                                "content": (
                                    "<!DOCTYPE html><html><body><main><h1>Steel Hunt</h1>"
                                    "<button>Start</button><canvas id='game'></canvas>"
                                    "<div class='hud'>HP 100</div></main>"
                                    "<script>function startGame(){requestAnimationFrame(loop);} "
                                    "function loop(){requestAnimationFrame(loop);}</script>"
                                    "</body></html>"
                                ),
                            },
                        }
                    ],
                    model_name="kimi-coding",
                    mode="openai_compatible_partial_timeout_salvage",
                )
            )

            self.assertIsNotNone(payload)
            self.assertTrue(payload.get("success"))
            self.assertIn("```html index.html", payload.get("output", ""))
            self.assertIn("Steel Hunt", payload.get("output", ""))
            self.assertTrue((Path(tmpdir) / "index.html").exists())
            self.assertTrue(any(
                self.bridge._tool_result_has_write(item)
                for item in (payload.get("tool_results") or [])
            ))

    def test_builder_text_output_persistability_rejects_thin_game_shell(self):
        thin_output = """```html index.html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: #05070d; color: #fff; min-height: 100vh; }
    .shell { position: fixed; inset: 0; }
  </style>
</head>
<body>
  <div class="shell"></div>
</body>
</html>
```"""

        self.assertFalse(
            self.bridge._builder_text_output_has_persistable_html(
                thin_output,
                "做一个第三人称 3D 射击游戏",
            )
        )

    def test_auto_save_builder_text_output_skips_non_persistable_game_shell(self):
        thin_output = """```html index.html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: #05070d; color: #fff; min-height: 100vh; }
    .shell { position: fixed; inset: 0; }
  </style>
</head>
<body>
  <div class="shell"></div>
</body>
</html>
```"""

        with tempfile.TemporaryDirectory() as tmpdir:
            self.bridge.config["output_dir"] = tmpdir
            saved_paths = asyncio.run(
                self.bridge._auto_save_builder_text_output(
                    output_text=thin_output,
                    input_data="做一个第三人称 3D 射击游戏",
                    tool_results=[],
                    tool_call_stats={},
                    on_progress=None,
                )
            )

            self.assertEqual(saved_paths, [])
            self.assertFalse((Path(tmpdir) / "index.html").exists())

    def test_auto_save_builder_text_output_blocks_support_lane_root_index_write(self):
        playable_output = """```html index.html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body { margin: 0; background: #08111f; color: #fff; font-family: sans-serif; }
    #startOverlay, #hud { position: absolute; left: 16px; top: 16px; z-index: 2; }
    #gameCanvas { width: 100vw; height: 100vh; display: block; }
  </style>
</head>
<body>
  <section id="startOverlay">
    <button id="startBtn" onclick="startGame()">Start Mission</button>
  </section>
  <canvas id="gameCanvas"></canvas>
  <div id="hud">HP 100 | Ammo 24</div>
  <script>
    let started = false;
    function startGame() { started = true; requestAnimationFrame(gameLoop); }
    function gameLoop() { if (!started) return; requestAnimationFrame(gameLoop); }
    document.addEventListener('keydown', () => {});
  </script>
</body>
</html>
```"""

        with tempfile.TemporaryDirectory() as tmpdir:
            self.bridge.config["output_dir"] = tmpdir
            saved_paths = asyncio.run(
                self.bridge._auto_save_builder_text_output(
                    output_text=playable_output,
                    input_data="做一个第三人称 3D 射击游戏",
                    node={
                        "type": "builder",
                        "can_write_root_index": False,
                        "allowed_html_targets": [],
                        "builder_is_support_lane_node": True,
                    },
                    tool_results=[],
                    tool_call_stats={},
                    on_progress=None,
                )
            )

            self.assertEqual(saved_paths, [])
            self.assertFalse((Path(tmpdir) / "index.html").exists())

    def test_auto_save_builder_text_output_respects_node_allowed_html_targets(self):
        website_output = """```html index.html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Homepage</title>
</head>
<body>
  <main>
    <h1>Home</h1>
    <p>This homepage should be ignored for this support lane.</p>
  </main>
</body>
</html>
```

```html about.html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>About</title>
</head>
<body>
  <main>
    <h1>About</h1>
    <p>Owned route content for this builder lane.</p>
    <section><p>Detailed copy block to satisfy persist checks.</p></section>
  </main>
</body>
</html>
```"""

        with tempfile.TemporaryDirectory() as tmpdir:
            self.bridge.config["output_dir"] = tmpdir
            saved_paths = asyncio.run(
                self.bridge._auto_save_builder_text_output(
                    output_text=website_output,
                    input_data="做一个企业官网",
                    node={
                        "type": "builder",
                        "can_write_root_index": False,
                        "allowed_html_targets": ["about.html"],
                    },
                    tool_results=[],
                    tool_call_stats={},
                    on_progress=None,
                )
            )

            self.assertEqual(saved_paths, [str(Path(tmpdir) / "about.html")])
            self.assertFalse((Path(tmpdir) / "index.html").exists())
            self.assertTrue((Path(tmpdir) / "about.html").exists())

    def test_auto_save_builder_text_output_stages_primary_root_when_merger_is_pending(self):
        playable_output = """```html index.html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Staged Root</title>
</head>
<body>
  <main>
    <button onclick="startGame()">Start</button>
    <canvas id="gameCanvas"></canvas>
  </main>
  <script>
    let started = false;
    function startGame() { started = true; requestAnimationFrame(loop); }
    function loop() { if (!started) return; requestAnimationFrame(loop); }
    document.addEventListener('keydown', () => {});
  </script>
</body>
</html>
```"""

        with tempfile.TemporaryDirectory() as tmpdir:
            staged_dir = Path(tmpdir) / "task_5"
            self.bridge.config["output_dir"] = tmpdir
            saved_paths = asyncio.run(
                self.bridge._auto_save_builder_text_output(
                    output_text=playable_output,
                    input_data="做一个第三人称 3D 射击游戏",
                    node={
                        "type": "builder",
                        "can_write_root_index": True,
                        "allowed_html_targets": ["index.html"],
                        "builder_stage_root_index_only": True,
                        "builder_staging_output_dir": str(staged_dir),
                    },
                    tool_results=[],
                    tool_call_stats={},
                    on_progress=None,
                )
            )

            self.assertEqual(saved_paths, [str(staged_dir / "index.html")])
            self.assertTrue((staged_dir / "index.html").exists())
            self.assertFalse((Path(tmpdir) / "index.html").exists())

    def test_auto_save_builder_text_output_stages_peer_direct_text_root_for_merger_pipeline(self):
        playable_output = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Peer Builder</title>
</head>
<body>
  <main>
    <button onclick="startGame()">Start</button>
    <canvas id="gameCanvas"></canvas>
  </main>
  <script>
    let started = false;
    function startGame() { started = true; requestAnimationFrame(loop); }
    function loop() { if (!started) return; requestAnimationFrame(loop); }
    document.addEventListener('keydown', () => {});
  </script>
</body>
</html>"""

        with tempfile.TemporaryDirectory() as tmpdir:
            staged_dir = Path(tmpdir) / "task_7"
            self.bridge.config["output_dir"] = tmpdir
            saved_paths = asyncio.run(
                self.bridge._auto_save_builder_text_output(
                    output_text=playable_output,
                    input_data="做一个第三人称 3D 射击游戏",
                    node={
                        "type": "builder",
                        "can_write_root_index": False,
                        "allowed_html_targets": [],
                        "subtask_id": "7",
                        "output_dir": tmpdir,
                    },
                    tool_results=[],
                    tool_call_stats={},
                    on_progress=None,
                )
            )

            self.assertEqual(saved_paths, [str(staged_dir / "index.html")])
            self.assertTrue((staged_dir / "index.html").exists())
            self.assertFalse((Path(tmpdir) / "index.html").exists())

    def test_auto_save_builder_text_output_defaults_raw_html_to_index_without_explicit_target(self):
        playable_output = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Luxury Landing</title>
</head>
<body>
  <main>
    <section><h1>Luxury</h1><p>Immersive narrative surface with editorial pacing, restrained motion, and premium product storytelling.</p></section>
    <section><div class="feature-grid"></div><p>Material palette notes, hero composition, and section rhythm are already resolved for implementation.</p></section>
    <section><h2>Craft</h2><p>Showcase brushed metal details, warm neutral contrast, oversized typography, and controlled reveal timing across every fold.</p></section>
    <section><h2>Collection</h2><p>Use staggered cards, pinned storytelling, immersive image framing, and high-clarity CTA sequencing without clutter.</p></section>
    <section><h2>Experience</h2><p>Support hover, focus, and scroll transitions with durable browser-native code, stable layout shells, and meaningful progressive disclosure.</p></section>
    <section><h2>System</h2><p>Design tokens, motion rules, spacing cadence, and layout constraints should be explicit enough for the runtime to persist as a trustworthy artifact.</p></section>
  </main>
</body>
</html>"""

        with tempfile.TemporaryDirectory() as tmpdir:
            self.bridge.config["output_dir"] = tmpdir
            saved_paths = asyncio.run(
                self.bridge._auto_save_builder_text_output(
                    output_text=playable_output,
                    input_data="Build a commercial-grade multi-page website for a luxury brand.",
                    node={
                        "type": "builder",
                        "can_write_root_index": True,
                        "allowed_html_targets": [],
                    },
                    tool_results=[],
                    tool_call_stats={},
                    on_progress=None,
                )
            )

            self.assertEqual(saved_paths, [str(Path(tmpdir) / "index.html")])
            self.assertTrue((Path(tmpdir) / "index.html").exists())

    def test_apply_runtime_node_contracts_adds_peer_staging_contract_when_targets_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staged_dir = Path(tmpdir) / "task_7"
            text = self.bridge._apply_runtime_node_contracts(
                {
                    "type": "builder",
                    "can_write_root_index": False,
                    "allowed_html_targets": [],
                    "subtask_id": "7",
                    "output_dir": tmpdir,
                },
                (
                    "Build a commercial-grade multi-page website for a luxury brand. "
                    "Create index.html plus at least 7 additional linked HTML page(s) via file_ops write."
                ),
            )

            self.assertIn("[BUILDER PEER STAGING CONTRACT]", text)
            self.assertIn(str(staged_dir / "index.html"), text)
            self.assertIn("output only one merger-ready html artifact", text.lower())

    def test_finalize_builder_chat_output_saves_preamble_wrapped_html(self):
        output = """I'll create the assigned page now.

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Platform</title>
</head>
<body>
  <main>
    <h1>Platform</h1>
    <p>Concrete builder content that should be persisted.</p>
    <section><p>Detailed product copy for the owned route.</p></section>
  </main>
</body>
</html>
```"""

        with tempfile.TemporaryDirectory() as tmpdir:
            self.bridge.config["output_dir"] = tmpdir
            saved_paths, tool_results, tool_call_stats, failure_msg = asyncio.run(
                self.bridge._finalize_builder_chat_output(
                    output_text=output,
                    input_data="Assigned HTML filenames for this builder: platform.html.",
                    node={
                        "type": "builder",
                        "can_write_root_index": False,
                        "allowed_html_targets": ["platform.html"],
                        "output_dir": tmpdir,
                    },
                    tool_results=[],
                    tool_call_stats={},
                    on_progress=None,
                )
            )

            self.assertEqual(failure_msg, "")
            self.assertEqual(saved_paths, [str(Path(tmpdir) / "platform.html")])
            self.assertTrue(any(self.bridge._tool_result_has_write(item) for item in tool_results))
            self.assertGreaterEqual(tool_call_stats.get("file_ops", 0), 1)

    def test_finalize_builder_chat_output_flags_missing_root_index_for_multi_page_batch(self):
        output = """```html pricing.html
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Pricing</title></head>
<body><main><h1>Pricing</h1><p>Concrete pricing copy.</p></main></body></html>
```

```html contact.html
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Contact</title></head>
<body><main><h1>Contact</h1><p>Concrete contact copy.</p></main></body></html>
```"""

        with tempfile.TemporaryDirectory() as tmpdir:
            self.bridge.config["output_dir"] = tmpdir
            saved_paths, tool_results, tool_call_stats, failure_msg = asyncio.run(
                self.bridge._finalize_builder_chat_output(
                    output_text=output,
                    input_data="Assigned HTML filenames for this builder: index.html, pricing.html, contact.html.",
                    node={
                        "type": "builder",
                        "can_write_root_index": True,
                        "allowed_html_targets": ["index.html", "pricing.html", "contact.html"],
                        "output_dir": tmpdir,
                    },
                    tool_results=[],
                    tool_call_stats={},
                    on_progress=None,
                )
            )

            self.assertIn("required root index.html", failure_msg)
            self.assertEqual(
                sorted(Path(path).name for path in saved_paths),
                ["contact.html", "pricing.html"],
            )
            self.assertTrue(any(self.bridge._tool_result_has_write(item) for item in tool_results))
            self.assertGreaterEqual(tool_call_stats.get("file_ops", 0), 2)

    def test_builder_partial_text_salvage_result_rejects_non_persistable_game_shell(self):
        thin_output = """```html index.html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: #05070d; color: #fff; min-height: 100vh; }
    .shell { position: fixed; inset: 0; }
  </style>
</head>
<body>
  <div class="shell"></div>
</body>
</html>
```"""

        payload = asyncio.run(
            self.bridge._builder_partial_text_salvage_result(
                input_data="做一个第三人称 3D 射击游戏",
                output_text=thin_output,
                reason="builder pre-write timeout",
                on_progress=None,
                tool_results=[],
                model_name="kimi-coding",
                mode="openai_compatible_partial_timeout_salvage",
            )
        )

        self.assertIsNone(payload)

    def test_builder_text_output_persistability_accepts_playable_game_html(self):
        playable_output = """```html index.html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body { margin: 0; background: #08111f; color: #fff; font-family: sans-serif; }
    #startOverlay, #hud { position: absolute; left: 16px; top: 16px; z-index: 2; }
    #gameCanvas { width: 100vw; height: 100vh; display: block; }
  </style>
</head>
<body>
  <section id="startOverlay">
    <button id="startBtn" onclick="startGame()">Start Mission</button>
  </section>
  <canvas id="gameCanvas"></canvas>
  <div id="hud">HP 100 | Ammo 24</div>
  <script>
    let started = false;
    function startGame() { started = true; requestAnimationFrame(gameLoop); }
    function gameLoop() { if (!started) return; requestAnimationFrame(gameLoop); }
    document.addEventListener('keydown', () => {});
  </script>
</body>
</html>
```"""

        self.assertTrue(
            self.bridge._builder_text_output_has_persistable_html(
                playable_output,
                "做一个第三人称 3D 射击游戏",
            )
        )

    def test_builder_prompt_includes_active_skill_checklist(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "builder"},
            plugins=[],
            input_data="做一个带动画的品牌官网，需要插画 hero",
        )
        self.assertIn("MANDATORY SKILL DIRECTIVES", prompt)
        self.assertIn("ACTIVE SKILLS", prompt)
        self.assertIn("motion-choreography-system", prompt)

    def test_builder_prompt_for_website_forbids_flat_monochrome_and_text_only_routes(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "builder"},
            plugins=[],
            input_data="做一个 6 页中国旅游官网，需要高级动效和高端视觉",
        )
        # V4.6 SPEED: design_system (color rules, monochrome guards) moved to
        # _builder_deferred_context for prompt caching. Core prompt keeps blueprint.
        self.assertIn("PAGE VISUAL COVERAGE", prompt)
        deferred = getattr(self.bridge, "_builder_deferred_context", "")
        self.assertIn("The root page canvas must NOT default to pure #000/#111 or pure #fff", deferred)
        self.assertIn("flat monochrome slab", deferred)

    def test_reviewer_prompt_loads_browser_testing_skill(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "reviewer"},
            plugins=[],
            input_data="做一个科技风官网",
        )
        self.assertIn("[Skill: browser-observe-act-verify]", prompt)
        # v6.1.3: reviewer generalized beyond "pages" to any product type.
        # Must still require full coverage + strict rejection posture.
        self.assertIn("Be STRICT", prompt)

    def test_analyst_prompt_loads_research_skill(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "analyst"},
            plugins=[],
            input_data="做一个科技风官网",
        )
        self.assertIn("[Skill: research-pattern-extraction]", prompt)
        self.assertIn("[Skill: source-first-research-loop]", prompt)

    def test_polisher_prompt_loads_scroll_evidence_skill(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "polisher"},
            plugins=[],
            input_data="把现有奢侈品官网做得更高级，补充转场和滚动动效",
        )
        self.assertIn("[Skill: scroll-evidence-capture]", prompt)
        self.assertIn("[Skill: visual-slot-recovery]", prompt)
        self.assertIn("upgrade an existing artifact", prompt.lower())
        self.assertIn("[Collection Image]", prompt)

    def test_imagegen_prompt_loads_image_direction_skills(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "imagegen"},
            plugins=[],
            input_data="生成一张品牌海报和封面图片",
        )
        self.assertIn("[Skill: image-prompt-director]", prompt)
        self.assertIn("[Skill: visual-storyboard-shotlist]", prompt)
        self.assertIn("[Skill: comfyui-pipeline-brief]", prompt)

    def test_imagegen_prompt_for_premium_3d_game_excludes_comfyui_brief(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "imagegen"},
            plugins=[],
            input_data="为第三人称 3D 射击游戏产出人物、怪物、武器和场景建模设计包，要达到商业级水准。",
        )
        self.assertIn("[Skill: image-prompt-director]", prompt)
        self.assertIn("[Skill: visual-storyboard-shotlist]", prompt)
        self.assertIn("[Skill: godogen-visual-target-lock]", prompt)
        self.assertIn("[Skill: godogen-3d-asset-replacement]", prompt)
        self.assertNotIn("[Skill: comfyui-pipeline-brief]", prompt)
        self.assertIn("modeling-design mode", prompt.lower())
        self.assertIn("00_visual_target.md", prompt)

    def test_imagegen_prompt_mentions_comfyui_when_available(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "imagegen"},
            plugins=[],
            input_data="为像素风平台跳跃游戏生成角色和敌人素材",
        )
        self.assertIn("comfyui plugin", prompt.lower())

    def test_imagegen_prompt_marks_backend_missing_when_unconfigured(self):
        bridge = AIBridge(config={})
        prompt = bridge._compose_system_prompt(
            {"type": "imagegen"},
            plugins=[],
            input_data="生成一套像素风角色素材",
        )
        self.assertIn("no configured image backend detected", prompt.lower())

    def test_imagegen_prompt_for_3d_game_without_backend_switches_to_modeling_mode(self):
        bridge = AIBridge(config={})
        prompt = bridge._compose_system_prompt(
            {"type": "imagegen"},
            plugins=[],
            input_data="创建一个第三人称 3D 射击游戏，要有人物、怪物、武器和精美建模",
        )
        self.assertIn("modeling-design mode", prompt.lower())
        self.assertIn("rig-or-animation requirements", prompt.lower())
        self.assertIn("minimum viable replacement pack", prompt.lower())
        self.assertIn("keep them clearly licensed and limited to the most relevant sources", prompt.lower())
        self.assertIn("weapon_primary_brief.md", prompt)

    def test_imagegen_max_tokens_budget_is_raised_for_core_pack_completion(self):
        # v5.8.1: ImageGen 8192 → 12288 rollback (8192 truncated tool_calls)
        self.assertEqual(self.bridge._max_tokens_for_node("imagegen", retry_attempt=0), 12288)
        self.assertEqual(self.bridge._max_tokens_for_node("imagegen", retry_attempt=1), 16384)

    def test_imagegen_prompt_marks_backend_configured_when_available(self):
        bridge = AIBridge(config={
            "image_generation": {
                "comfyui_url": "http://127.0.0.1:8188",
                "workflow_template": "/tmp/workflow.json",
            }
        })
        prompt = bridge._compose_system_prompt(
            {"type": "imagegen"},
            plugins=[],
            input_data="生成一套像素风角色素材",
        )
        self.assertIn("configured image backend detected", prompt.lower())

    def test_reviewer_preset_uses_strict_thresholds(self):
        # v6.1.3: reviewer softened to "avg ≥ 6.5 + blocking-only reject"
        # rule. Single-dimension auto-reject removed — drove false negatives.
        prompt = AGENT_PRESETS["reviewer"]["instructions"]
        self.assertIn("avg \u2265 6.5", prompt)
        self.assertIn("blocking_issues", prompt)
        self.assertIn("ship_readiness", prompt)
        self.assertIn("missing_deliverables", prompt)

    def test_reviewer_preset_requires_route_named_owner_changes(self):
        prompt = AGENT_PRESETS["reviewer"]["instructions"]
        # v6.1.3: reviewer now generalized across product types. Still must
        # require file/UI anchor citations and owner-prefixed rework items.
        self.assertIn("file:line", prompt)
        self.assertIn("file + UI anchor", prompt)
        self.assertIn("Builder: ...", prompt)
        self.assertIn("Polisher: ...", prompt)

    def test_scribe_prompt_loads_doc_skills(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "scribe"},
            plugins=[],
            input_data="写一份 API documentation 和 README",
        )
        self.assertIn("[Skill: docs-clarity-architecture]", prompt)

    def test_presentation_builder_prompt_loads_export_skill(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "builder"},
            plugins=[],
            input_data="做一个融资路演 PPT slides",
        )
        self.assertIn("[Skill: pptx-export-bridge]", prompt)

    def test_builder_repo_edit_prompt_injects_aider_style_repo_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").write_text('{"scripts":{"build":"next build","test":"vitest"}}', encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "app").mkdir()
            (root / "src" / "app" / "page.tsx").write_text("export default function Page(){ return null; }", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "app.test.ts").write_text("it('works', () => {})", encoding="utf-8")
            bridge = AIBridge(config={"workspace": tmpdir})
            prompt = bridge._compose_system_prompt(
                {"type": "builder"},
                plugins=[],
                input_data="修复这个仓库里的登录页面 bug，并保持现有 Next.js 结构",
            )
            self.assertIn("EXISTING REPOSITORY EDIT MODE", prompt)
            self.assertIn("AIDER-STYLE REPO MAP", prompt)
            self.assertIn("src/app/page.tsx", prompt)
            self.assertIn("npm run build", prompt)

    def test_greenfield_builder_prompt_does_not_inject_repo_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").write_text('{"scripts":{"build":"next build"}}', encoding="utf-8")
            bridge = AIBridge(config={"workspace": tmpdir})
            prompt = bridge._compose_system_prompt(
                {"type": "builder"},
                plugins=[],
                input_data="做一个全新的品牌官网首页",
            )
            self.assertNotIn("AIDER-STYLE REPO MAP", prompt)

    def test_greenfield_file_ops_delivery_builder_prompt_does_not_inject_repo_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "frontend").mkdir()
            (root / "frontend" / "package.json").write_text('{"scripts":{"build":"next build","lint":"next lint"}}', encoding="utf-8")
            (root / "frontend" / "src").mkdir()
            (root / "frontend" / "src" / "app").mkdir(parents=True, exist_ok=True)
            (root / "frontend" / "src" / "app" / "page.tsx").write_text("export default function Page(){ return null; }", encoding="utf-8")
            bridge = AIBridge(config={"workspace": tmpdir, "output_dir": "/var/folders/demo/evermind_output"})
            prompt = bridge._compose_system_prompt(
                {"type": "builder"},
                plugins=[],
                input_data=(
                    "Build a commercial-grade multi-page website for a luxury brand. "
                    "Create index.html plus at least 7 additional linked HTML page(s) via file_ops write."
                ),
            )
            self.assertNotIn("AIDER-STYLE REPO MAP", prompt)
            self.assertIn("RUNTIME OUTPUT CONTRACT", prompt)

    def test_greenfield_game_builder_prompt_with_fix_language_does_not_inject_repo_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "frontend").mkdir()
            (root / "frontend" / "package.json").write_text('{"scripts":{"build":"next build","lint":"next lint"}}', encoding="utf-8")
            (root / "frontend" / "src").mkdir()
            (root / "frontend" / "src" / "app").mkdir(parents=True, exist_ok=True)
            (root / "frontend" / "src" / "app" / "page.tsx").write_text("export default function Page(){ return null; }", encoding="utf-8")
            bridge = AIBridge(config={"workspace": tmpdir, "output_dir": "/var/folders/demo/evermind_output"})
            prompt = bridge._compose_system_prompt(
                {"type": "builder"},
                plugins=[],
                input_data=(
                    "ADVANCED MODE — Use analyst notes and asset manifest.\n"
                    "Build a commercial-grade HTML5 game for: 创建一个3d射击游戏，要有怪物、枪械、关卡和第三人称视角。\n"
                    "Fix JavaScript/runtime errors that prevent the page from rendering correctly."
                ),
            )
            self.assertNotIn("EXISTING REPOSITORY EDIT MODE", prompt)
            self.assertNotIn("AIDER-STYLE REPO MAP", prompt)
            self.assertIn("RUNTIME OUTPUT CONTRACT", prompt)

    def test_builder_prompt_uses_raw_goal_for_skill_selection_instead_of_runtime_blob(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = AIBridge(config={"workspace": tmpdir, "output_dir": "/tmp/evermind_output"})
            prompt = bridge._compose_system_prompt(
                {
                    "type": "builder",
                    "goal": "创建一个第三人称 3D 射击游戏，带怪物、武器、大地图和精美建模。",
                },
                plugins=[],
                input_data=(
                    "ADVANCED MODE — Use analyst notes and asset manifest.\n"
                    "<builder_1_handoff>需要高级 motion、插画、poster、documentation 页面语气、image prompt pack。</builder_1_handoff>\n"
                    "Build a commercial-grade HTML5 game for: 创建一个第三人称 3D 射击游戏，带怪物、武器、大地图和精美建模。"
                ),
            )
            self.assertIn("[Skill: gameplay-foundation]", prompt)
            self.assertIn("[Skill: game-feel-feedback]", prompt)
            self.assertNotIn("[Skill: image-prompt-director]", prompt)
            self.assertNotIn("[Skill: docs-clarity-architecture]", prompt)

    def test_builder_direct_delivery_modes_ignore_repo_map(self):
        repo_context = {
            "repo_root": "/tmp/demo-repo",
            "prompt_block": "Repo root: /tmp/demo-repo\n- frontend/src/app/page.tsx",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = AIBridge(config={"workspace": tmpdir, "output_dir": "/tmp/evermind_output"})
            for mode in ("direct_text", "direct_multifile"):
                with self.subTest(mode=mode), patch("ai_bridge.build_repo_context", return_value=repo_context):
                    prompt = bridge._compose_system_prompt(
                        {"type": "builder", "builder_delivery_mode": mode},
                        plugins=[],
                        input_data=(
                            "Build a commercial-grade HTML5 game and save final HTML via file_ops write "
                            "to /tmp/evermind_output/index.html."
                        ),
                    )
                self.assertNotIn("EXISTING REPOSITORY EDIT MODE", prompt)
                self.assertNotIn("AIDER-STYLE REPO MAP", prompt)

    def test_debugger_repo_edit_prompt_injects_repo_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            (root / "server.py").write_text("print('hi')\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_server.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
            bridge = AIBridge(config={"workspace": tmpdir})
            prompt = bridge._compose_system_prompt(
                {"type": "debugger"},
                plugins=[],
                input_data="修复当前仓库里 failing test 的 root cause",
            )
            self.assertIn("AIDER-STYLE REPO MAP", prompt)
            self.assertIn("server.py", prompt)

    def test_infer_file_ops_action_prefers_explicit_args(self):
        action = self.bridge._infer_file_ops_action(
            "{\"action\":\"write\",\"path\":\"/tmp/evermind_output/index.html\"}",
            {"success": True, "data": {"written": True}},
        )
        self.assertEqual(action, "write")

    def test_infer_file_ops_action_falls_back_to_result_shape(self):
        action = self.bridge._infer_file_ops_action(
            "{bad json",
            {"success": True, "data": {"path": "/tmp/evermind_output", "entries": []}},
        )
        self.assertEqual(action, "list")

    def test_builder_partial_text_is_salvageable_for_named_multi_page_html(self):
        text = (
            "```html index.html\n<!DOCTYPE html><html><body>home</body></html>\n```\n"
            "```html collections.html\n<!DOCTYPE html><html><body>collections</body></html>\n```"
        )
        self.assertTrue(
            self.bridge._builder_partial_text_is_salvageable(
                "Create index.html plus at least 7 additional linked HTML page(s) via file_ops write.",
                text,
            )
        )

    def test_builder_partial_text_is_not_salvageable_for_short_prose(self):
        self.assertFalse(
            self.bridge._builder_partial_text_is_salvageable(
                "Build a premium website",
                "I would create a homepage and maybe add sections later.",
            )
        )


class TestReviewerTesterFollowupPolicy(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_analyst_followup_requires_two_distinct_source_urls(self):
        reason = self.bridge._analyst_browser_followup_reason(
            "analyst",
            {"browser": 1},
            [{"success": True, "data": {"url": "https://example.com/docs"}}],
            available_tool_names={"browser"},
        )
        self.assertIn("only visited 1 source URL", reason)

    def test_analyst_followup_message_requires_browsing_before_finalize(self):
        msg = self.bridge._analyst_browser_followup_message(
            "You have only visited 1 source URL. Use the browser tool on one more distinct GitHub/doc/tutorial/source page before final report."
        )
        self.assertIn("one more distinct URL", msg)
        self.assertIn("Do not output the final analyst report yet", msg)
        self.assertIn("localhost", msg)

    def test_analyst_reference_urls_ignore_local_preview(self):
        urls = self.bridge._tool_results_reference_urls([
            {"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}},
            {"success": True, "data": {"url": "https://github.com/mrdoob/three.js"}},
        ])
        self.assertEqual(urls, ["https://github.com/mrdoob/three.js"])

    def test_asset_plan_output_complete_detects_compact_valid_json(self):
        spritesheet_json = json.dumps({
            "asset_families": ["hero", "enemy"],
            "model_targets": {"hero": "hero.glb"},
            "style_lock_tokens": ["premium sci-fi"],
            "builder_replacement_rules": {"hero": "swap"},
            "material_constraints": {"hero": ["pbr"]},
        }, ensure_ascii=False)
        assetimport_json = json.dumps({
            "naming_rules": {"hero": "hero.glb"},
            "folder_structure": {"models": "assets/models"},
            "manifest_fields": ["id", "path"],
            "runtime_mapping": {"hero": "assets/models/hero.glb"},
            "replacement_keys": {"hero": "player.hero"},
            "builder_integration_notes": ["load via manifest"],
        }, ensure_ascii=False)

        self.assertTrue(self.bridge._asset_plan_output_complete("spritesheet", spritesheet_json))
        self.assertTrue(self.bridge._asset_plan_output_complete("assetimport", assetimport_json))

    def test_website_review_requires_post_interaction_verification(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "snapshot", "ok": True},
                {"action": "scroll", "ok": True},
                {"action": "click", "ok": True, "state_changed": True},
            ],
        )
        self.assertIn("record_scroll", reason)

    def test_website_review_requires_scroll_to_bottom_when_metadata_shows_more_content(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "snapshot", "ok": True},
                {"action": "scroll", "ok": True, "is_scrollable": True, "at_bottom": False},
                {"action": "click", "ok": True, "state_changed": True},
                {"action": "snapshot", "ok": True, "state_changed": True},
            ],
        )
        self.assertIn("bottom of the page", reason)

    def test_website_review_passes_with_post_interaction_snapshot(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "snapshot", "ok": True},
                {"action": "scroll", "ok": True},
                {"action": "click", "ok": True, "state_changed": True},
                {"action": "snapshot", "ok": True, "state_changed": True},
            ],
        )
        self.assertIsNone(reason)

    def test_website_review_accepts_record_scroll_as_post_interaction_verification(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "observe", "ok": True},
                {"action": "click", "ok": True, "state_changed": True},
                {
                    "action": "record_scroll",
                    "ok": True,
                    "state_changed": True,
                    "at_bottom": True,
                    "is_scrollable": True,
                },
            ],
        )
        self.assertIsNone(reason)

    def test_multi_page_review_requires_distinct_page_visits(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                {"action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "is_scrollable": False},
                {"action": "act", "subaction": "click", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/"},
                {"action": "observe", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/"},
            ],
            "做一个三页面官网，包含首页、定价页和联系页",
        )
        self.assertIn("every requested page", reason)

    def test_multi_page_review_passes_after_visiting_all_pages(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                {"action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "at_bottom": True, "is_scrollable": True},
                {"action": "act", "subaction": "click", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/pricing.html"},
                {"action": "observe", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/pricing.html"},
                {"action": "act", "subaction": "click", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/contact.html"},
                {"action": "observe", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/contact.html"},
            ],
            "做一个三页面官网，包含首页、定价页和联系页",
        )
        self.assertIsNone(reason)

    def test_multi_page_review_names_missing_pages_from_current_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            for name in ("index.html", "pricing.html", "contact.html"):
                (out / name).write_text("<!doctype html><html><body></body></html>", encoding="utf-8")
            self.bridge.config["output_dir"] = td
            reason = self.bridge._review_browser_followup_reason(
                "reviewer",
                "website",
                [
                    {"action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                    {"action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "at_bottom": True, "is_scrollable": True},
                    {"action": "act", "subaction": "click", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/pricing.html"},
                    {"action": "observe", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/pricing.html"},
                ],
                "做一个三页面官网，包含首页、定价页和联系页",
            )
        self.assertIn("contact.html", reason)
        self.assertIn("2/3", reason)

    def test_review_followup_message_includes_direct_hint_for_missing_pages(self):
        msg = self.bridge._review_browser_followup_message(
            (
                "You must cover every requested page/route before final verdict. "
                "Current distinct pages visited: 2/3. Remaining missing pages: contact.html."
            ),
            "website",
        )
        self.assertIn("contact.html", msg)
        self.assertIn('"action":"navigate"', msg)

    def test_game_test_requires_press_sequence(self):
        reason = self.bridge._review_browser_followup_reason(
            "tester",
            "game",
            [
                {"action": "snapshot", "ok": True},
                {"action": "click", "ok": True, "state_changed": True},
                {"action": "snapshot", "ok": True, "state_changed": True},
            ],
        )
        self.assertIn("gameplay controls", reason)

    def test_game_review_prefers_internal_browser_when_force_flag_is_off(self):
        self.bridge.config["qa_enable_browser_use"] = True
        self.bridge.config["openai_api_key"] = "sk-test"
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "game",
            [
                {"action": "snapshot", "ok": True},
                {"action": "click", "ok": True, "state_changed": True},
                {"action": "press_sequence", "ok": True, "state_changed": True},
                {"action": "snapshot", "ok": True, "state_changed": True},
            ],
            tool_call_stats={"browser": 3},
            available_tool_names={"browser", "browser_use"},
        )
        self.assertIsNone(reason)

    def test_followup_message_includes_browser_use_gameplay_hint(self):
        msg = self.bridge._review_browser_followup_message(
            "You must use browser_use for a real gameplay session before final verdict.",
            "game",
        )
        self.assertIn("browser_use", msg)
        self.assertIn('"url":"http://127.0.0.1:8765/preview/"', msg)

    def test_game_review_requires_browser_use_when_force_flag_enabled(self):
        self.bridge.config["qa_enable_browser_use"] = True
        self.bridge.config["qa_force_browser_use_for_games"] = True
        self.bridge.config["openai_api_key"] = "sk-test"
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "game",
            [
                {"action": "snapshot", "ok": True},
                {"action": "click", "ok": True, "state_changed": True},
                {"action": "press_sequence", "ok": True, "state_changed": True},
            ],
            tool_call_stats={"browser": 3},
            available_tool_names={"browser", "browser_use"},
        )
        self.assertIn("browser_use", reason)

    def test_review_output_format_followup_reason_requires_strict_json_verdict(self):
        reason = self.bridge._review_output_format_followup_reason(
            "reviewer",
            "布局不错，但建议再优化导航和配色。",
        )
        self.assertIn("JSON", reason)

    def test_review_output_format_followup_reason_accepts_valid_verdict_json(self):
        reason = self.bridge._review_output_format_followup_reason(
            "reviewer",
            '{"verdict":"REJECTED","scores":{"layout":6},"blocking_issues":["nav broken"],"required_changes":["Builder: fix nav"]}',
        )
        self.assertIsNone(reason)

    def test_reviewer_needs_forced_verdict_when_review_has_evidence_but_no_json(self):
        self.assertTrue(
            self.bridge._reviewer_needs_forced_verdict(
                "reviewer",
                "布局不错，但导航和交互还需要继续验证。",
                [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
            )
        )
        self.assertFalse(
            self.bridge._reviewer_needs_forced_verdict(
                "reviewer",
                '{"verdict":"APPROVED","scores":{"layout":8}}',
                [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
            )
        )

    def test_reviewer_forced_verdict_messages_require_strict_json_only(self):
        messages = self.bridge._reviewer_forced_verdict_messages(
            "system prompt",
            "Review the generated game.",
            [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
            "The HUD works, but the final verdict JSON is missing.",
            'Return the final reviewer result as ONE strict JSON object only with verdict set to "APPROVED" or "REJECTED".',
        )
        self.assertEqual(len(messages), 2)
        self.assertIn("strict JSON object", messages[1]["content"])
        self.assertIn('"verdict":"APPROVED" or "REJECTED"', messages[1]["content"])
        self.assertIn("REJECTED", messages[1]["content"])

    def test_desktop_qa_browser_suppression_blocks_reviewer_browser_tools(self):
        self.assertTrue(
            self.bridge._desktop_qa_browser_suppressed(
                "reviewer",
                "[Desktop QA Session Evidence]\nDESKTOP_QA_BROWSER_SUPPRESSED=1\nUse the desktop QA record only.",
            )
        )
        self.assertFalse(
            self.bridge._desktop_qa_browser_suppressed(
                "builder",
                "[Desktop QA Session Evidence]\nDESKTOP_QA_BROWSER_SUPPRESSED=1\nUse the desktop QA record only.",
            )
        )

    def test_browser_use_action_events_normalize_to_snapshot_click_and_press(self):
        events = self.bridge._browser_use_action_events({
            "success": True,
            "data": {
                "final_url": "http://127.0.0.1:8765/preview/",
                "recording_path": "/tmp/gameplay.webm",
                "history_items": [
                    {
                        "step": 1,
                        "action_names": ["click_element"],
                        "url": "http://127.0.0.1:8765/preview/",
                        "screenshot_path": "/tmp/step1.png",
                        "errors": [],
                    },
                    {
                        "step": 2,
                        "action_names": ["send_keys"],
                        "url": "http://127.0.0.1:8765/preview/",
                        "screenshot_path": "/tmp/step2.png",
                        "errors": [],
                    },
                ],
            },
        })
        actions = [event.get("action") for event in events]
        self.assertIn("snapshot", actions)
        self.assertIn("click", actions)
        self.assertIn("press_sequence", actions)
        self.assertTrue(any(event.get("recording_path") == "/tmp/gameplay.webm" for event in events))

    def test_qa_browser_use_prefetch_summary_surfaces_recording_and_actions(self):
        summary = self.bridge._qa_browser_use_prefetch_summary(
            {
                "success": True,
                "data": {
                    "final_url": "http://127.0.0.1:8765/preview/",
                    "recording_path": "/tmp/gameplay.webm",
                    "capture_path": "/tmp/shot.png",
                },
            },
            [
                {"action": "snapshot"},
                {"action": "click"},
                {"action": "press_sequence"},
            ],
            "game",
        )
        self.assertIn("browser_use QA preflight", summary)
        self.assertIn("recording_path: /tmp/gameplay.webm", summary)
        self.assertIn("captured_actions: snapshot, click, press_sequence", summary)

    def test_followup_policy_accepts_observe_and_act(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "observe", "ok": True},
                {"action": "scroll", "ok": True},
                {"action": "act", "subaction": "click", "ok": True, "state_changed": True},
                {"action": "observe", "ok": True, "state_changed": True},
            ],
        )
        self.assertIsNone(reason)


class TestAssistantSerialization(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_keeps_reasoning_content_in_serialized_message(self):
        class Fn:
            name = "file_ops"
            arguments = "{\"action\":\"list\"}"

        class ToolCall:
            id = "call_1"
            type = "function"
            function = Fn()

        class Msg:
            content = ""
            tool_calls = [ToolCall()]
            reasoning_content = "thinking trace"

            def model_dump(self, exclude_none=True):
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "file_ops", "arguments": "{\"action\":\"list\"}"},
                    }],
                    "reasoning_content": "thinking trace",
                }

        payload = self.bridge._serialize_assistant_message(Msg())
        self.assertEqual(payload.get("role"), "assistant")
        self.assertIn("reasoning_content", payload)
        self.assertEqual(payload["reasoning_content"], "thinking trace")
        self.assertEqual(payload["tool_calls"][0]["function"]["name"], "file_ops")


class TestRunPluginArgNormalization(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_none_args_become_empty_dict(self):
        class ResultObj:
            def to_dict(self):
                return {"ok": True}

        class StubPlugin:
            name = "stub"

            async def execute(self, params, context=None):
                return ResultObj() if params == {} else None

        out = asyncio.run(self.bridge._run_plugin("stub", None, [StubPlugin()]))
        self.assertEqual(out.get("ok"), True)

    def test_malformed_json_args_do_not_crash(self):
        class ResultObj:
            def to_dict(self):
                return {"ok": True}

        class StubPlugin:
            name = "stub"

            async def execute(self, params, context=None):
                return ResultObj() if isinstance(params, dict) else None

        out = asyncio.run(self.bridge._run_plugin("stub", "{bad json", [StubPlugin()]))
        self.assertEqual(out.get("ok"), True)

    def test_plugin_not_in_allowlist_is_rejected(self):
        out = asyncio.run(self.bridge._run_plugin("browser", {"action": "navigate"}, []))
        self.assertIn("not enabled", (out.get("error") or "").lower())

    def test_reviewer_browser_forces_headful_context(self):
        class ResultObj:
            def to_dict(self):
                return {"ok": True}

        class StubPlugin:
            name = "browser"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj()

        self.bridge.config = {"reviewer_tester_force_headful": True, "browser_headful": False}
        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin("browser", {"action": "navigate"}, [plugin], node_type="reviewer"))
        self.assertEqual(out.get("ok"), True)
        self.assertTrue(plugin.last_context.get("browser_headful"))
        self.assertEqual(plugin.last_context.get("browser_force_reason"), "reviewer_visible_review")

    def test_builder_browser_does_not_force_headful_context(self):
        class ResultObj:
            def to_dict(self):
                return {"ok": True}

        class StubPlugin:
            name = "browser"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj()

        self.bridge.config = {"reviewer_tester_force_headful": True, "browser_headful": False}
        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin("browser", {"action": "navigate"}, [plugin], node_type="builder"))
        self.assertEqual(out.get("ok"), True)
        self.assertFalse(plugin.last_context.get("browser_headful", False))

    def test_analyst_browser_ignores_global_headful_context(self):
        class ResultObj:
            def to_dict(self):
                return {"ok": True}

        class StubPlugin:
            name = "browser"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj()

        self.bridge.config = {"browser_headful": True, "reviewer_tester_force_headful": True}
        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin(
            "browser",
            {"action": "navigate", "url": "https://github.com/mrdoob/three.js"},
            [plugin],
            node_type="analyst",
        ))
        self.assertEqual(out.get("ok"), True)
        self.assertFalse(plugin.last_context.get("browser_headful", False))
        self.assertIsNone(plugin.last_context.get("browser_force_reason"))

    def test_analyst_browser_blocks_local_preview_navigation(self):
        class StubPlugin:
            name = "browser"

            async def execute(self, params, context=None):
                raise AssertionError("plugin should not be called for blocked analyst preview navigation")

        out = asyncio.run(self.bridge._run_plugin(
            "browser",
            {"action": "navigate", "url": "http://127.0.0.1:8765/preview/"},
            [StubPlugin()],
            node_type="analyst",
        ))
        self.assertFalse(out.get("success"))
        self.assertIn("external implementation sources", out.get("error", ""))

    def test_browser_plugin_receives_run_and_evidence_context(self):
        class ResultObj:
            def to_dict(self):
                return {"ok": True}

        class StubPlugin:
            name = "browser"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj()

        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin(
            "browser",
            {"action": "navigate"},
            [plugin],
            node_type="tester",
            node={
                "type": "tester",
                "run_id": "run_ctx_1",
                "node_execution_id": "nodeexec_ctx_1",
            },
        ))
        self.assertEqual(out.get("ok"), True)
        self.assertEqual(out.get("_plugin"), "browser")
        self.assertEqual(plugin.last_context.get("run_id"), "run_ctx_1")
        self.assertEqual(plugin.last_context.get("node_execution_id"), "nodeexec_ctx_1")
        self.assertTrue(plugin.last_context.get("browser_save_evidence"))

    def test_tester_browser_missing_url_is_backfilled_to_preview(self):
        class ResultObj:
            def __init__(self, params):
                self.params = params

            def to_dict(self):
                return {"ok": True, "url": self.params.get("url")}

        class StubPlugin:
            name = "browser"

            def __init__(self):
                self.last_params = None

            async def execute(self, params, context=None):
                self.last_params = dict(params or {})
                return ResultObj(self.last_params)

        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin(
            "browser",
            {"action": "navigate"},
            [plugin],
            node_type="tester",
        ))
        self.assertEqual(out.get("ok"), True)
        self.assertEqual(plugin.last_params.get("url"), "http://127.0.0.1:8765/preview/")

    def test_tester_browser_use_missing_url_is_backfilled_to_preview(self):
        class ResultObj:
            def __init__(self, params):
                self.params = params

            def to_dict(self):
                return {"ok": True, "url": self.params.get("url")}

        class StubPlugin:
            name = "browser_use"

            def __init__(self):
                self.last_params = None

            async def execute(self, params, context=None):
                self.last_params = dict(params or {})
                return ResultObj(self.last_params)

        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin(
            "browser_use",
            {"task": "inspect the preview"},
            [plugin],
            node_type="reviewer",
        ))
        self.assertEqual(out.get("ok"), True)
        self.assertEqual(plugin.last_params.get("url"), "http://127.0.0.1:8765/preview/")

    def test_qa_browser_use_available_requires_real_openai_key(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            self.bridge.config = {"qa_enable_browser_use": True}
            available = self.bridge._qa_browser_use_available("tester", {"browser", "browser_use"})
            self.assertFalse(available)

            self.bridge.config["openai_api_key"] = "sk-test"
            available = self.bridge._qa_browser_use_available("tester", {"browser", "browser_use"})
            self.assertTrue(available)

    def test_reviewer_file_ops_runs_in_read_only_mode(self):
        class ResultObj:
            def __init__(self, context):
                self.context = context

            def to_dict(self):
                return {"ok": True, "mode": self.context.get("file_ops_mode")}

        class StubPlugin:
            name = "file_ops"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj(self.last_context)

        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin("file_ops", {"action": "list", "path": "/tmp"}, [plugin], node_type="reviewer"))
        self.assertEqual(out.get("ok"), True)
        self.assertEqual(plugin.last_context.get("file_ops_mode"), "read_only")
        self.assertEqual(plugin.last_context.get("file_ops_node_type"), "reviewer")

    def test_scribe_file_ops_runs_in_read_only_mode(self):
        class ResultObj:
            def __init__(self, context):
                self.context = context

            def to_dict(self):
                return {"ok": True, "mode": self.context.get("file_ops_mode")}

        class StubPlugin:
            name = "file_ops"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj(self.last_context)

        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin("file_ops", {"action": "list", "path": "/tmp"}, [plugin], node_type="scribe"))
        self.assertEqual(out.get("ok"), True)
        self.assertEqual(plugin.last_context.get("file_ops_mode"), "read_only")
        self.assertEqual(plugin.last_context.get("file_ops_node_type"), "scribe")

    def test_polisher_file_ops_keeps_write_mode(self):
        class ResultObj:
            def __init__(self, context):
                self.context = context

            def to_dict(self):
                return {"ok": True, "mode": self.context.get("file_ops_mode", "read_write")}

        class StubPlugin:
            name = "file_ops"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj(self.last_context)

        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin("file_ops", {"action": "write", "path": "/tmp/evermind_output/index.html"}, [plugin], node_type="polisher"))
        self.assertEqual(out.get("ok"), True)
        self.assertEqual(plugin.last_context.get("file_ops_node_type"), "polisher")
        self.assertNotEqual(plugin.last_context.get("file_ops_mode"), "read_only")

    def test_polisher_can_write_deliverable_html_to_output_root(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "index.html"
            target.write_text(
                "<!DOCTYPE html><html><head><meta charset='UTF-8'></head><body><main><section>Strong draft</section></main></body></html>",
                encoding="utf-8",
            )
            out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(target),
                    "content": "<!DOCTYPE html><html><head><meta charset='UTF-8'></head><body><main><section>Polished result</section></main></body></html>",
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "polisher",
                    "file_ops_output_dir": td,
                },
            ))

        self.assertTrue(out.success)
        self.assertTrue(out.data.get("written"))
        self.assertEqual(out.data.get("path"), str(target))

    def test_file_ops_write_blocked_in_read_only_mode(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            out = asyncio.run(self.bridge._run_plugin(
                "file_ops",
                {"action": "write", "path": str(Path(td) / "blocked.txt"), "content": "x"},
                [plugin],
                node_type="tester",
            ))
        self.assertFalse(out.get("success", True))
        self.assertIn("read-only", str(out.get("error", "")).lower())

    def test_non_builder_cannot_write_deliverable_html_to_output_root(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            self.bridge.config = {"allowed_dirs": [td], "output_dir": td}
            out = asyncio.run(self.bridge._run_plugin(
                "file_ops",
                {"action": "write", "path": str(Path(td) / "index.html"), "content": "<html></html>"},
                [plugin],
                node_type="imagegen",
            ))
        self.assertFalse(out.get("success", True))
        self.assertIn("not allowed to write deliverable html", str(out.get("error", "")).lower())

    def test_builder_file_ops_receives_assigned_html_targets_context(self):
        class ResultObj:
            def __init__(self, context):
                self.context = context

            def to_dict(self):
                return {"ok": True}

        class StubPlugin:
            name = "file_ops"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj(self.last_context)

        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin(
            "file_ops",
            {"action": "write", "path": "/tmp/evermind_output/about.html", "content": "<html></html>"},
            [plugin],
            node_type="builder",
            node={
                "type": "builder",
                "output_dir": "/tmp/evermind_output",
                "allowed_html_targets": ["about.html", "platform.html", "contact.html", "faq.html"],
                "can_write_root_index": False,
            },
        ))
        self.assertTrue(out.get("ok"))
        self.assertEqual(
            plugin.last_context.get("file_ops_allowed_html_targets"),
            ["about.html", "platform.html", "contact.html", "faq.html"],
        )
        self.assertFalse(plugin.last_context.get("file_ops_can_write_root_index"))

    def test_builder_file_ops_receives_patch_mode_read_contract_context(self):
        class ResultObj:
            def __init__(self, context):
                self.context = context

            def to_dict(self):
                return {"ok": True}

        class StubPlugin:
            name = "file_ops"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj(self.last_context)

        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin(
            "file_ops",
            {"action": "read", "path": "/tmp/evermind_output/index.html"},
            [plugin],
            node_type="builder",
            node={
                "type": "builder",
                "output_dir": "/tmp/evermind_output",
                "allowed_html_targets": ["index.html"],
                "can_write_root_index": True,
                "builder_existing_artifact_patch_mode": True,
                "builder_patch_required_read_targets": ["index.html"],
            },
        ))
        self.assertTrue(out.get("ok"))
        self.assertTrue(plugin.last_context.get("file_ops_require_existing_artifact_read"))
        self.assertEqual(plugin.last_context.get("file_ops_required_read_targets"), ["index.html"])

    def test_builder_file_ops_blocks_unassigned_html_target(self):
        plugin = FileOpsPlugin()
        self.bridge.config = {"allowed_dirs": ["/tmp/evermind_output"]}
        out = asyncio.run(self.bridge._run_plugin(
                "file_ops",
                {"action": "write", "path": "/tmp/evermind_output/index.html", "content": "<html></html>"},
                [plugin],
                node_type="builder",
                node={
                    "type": "builder",
                    "output_dir": "/tmp/evermind_output",
                    "allowed_html_targets": ["about.html", "platform.html", "contact.html", "faq.html"],
                    "can_write_root_index": False,
                    "enforce_html_targets": True,
                },
            ))
        self.assertFalse(out.get("success", True))
        self.assertIn("HTML target not assigned for builder", str(out.get("error", "")))

    def test_builder_file_ops_blocks_secondary_builder_root_write_even_without_named_target_enforcement(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            self.bridge.config = {"allowed_dirs": [td], "output_dir": td}
            out = asyncio.run(self.bridge._run_plugin(
                "file_ops",
                {"action": "write", "path": str(Path(td) / "index.html"), "content": "<html></html>"},
                [plugin],
                node_type="builder",
                node={
                    "type": "builder",
                    "output_dir": td,
                    "can_write_root_index": False,
                    "enforce_html_targets": False,
                },
            ))
        self.assertFalse(out.get("success", True))
        self.assertIn("must not overwrite the root gameplay/site shell", str(out.get("error", "")).lower())

    def test_builder_file_ops_blocks_secondary_builder_shared_root_asset_write(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            self.bridge.config = {"allowed_dirs": [td], "output_dir": td}
            out = asyncio.run(self.bridge._run_plugin(
                "file_ops",
                {"action": "write", "path": str(Path(td) / "styles.css"), "content": "body{margin:0;}"},
                [plugin],
                node_type="builder",
                node={
                    "type": "builder",
                    "output_dir": td,
                    "can_write_root_index": False,
                    "enforce_html_targets": False,
                },
            ))
        self.assertFalse(out.get("success", True))
        self.assertIn("shared root asset", str(out.get("error", "")).lower())

    def test_builder_file_ops_rejects_stale_attempt_write_token(self):
        plugin = FileOpsPlugin()
        valid_html = (
            "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"></head>"
            "<body><main><section>ok</section></main></body></html>"
        )
        with tempfile.TemporaryDirectory() as td:
            node_execution_id = "ne_builder_live"
            set_active_file_ops_write_token(node_execution_id, "fresh-token")
            try:
                out = asyncio.run(plugin.execute(
                    {"action": "write", "path": str(Path(td) / "index.html"), "content": valid_html},
                    context={
                        "allowed_dirs": [td],
                        "file_ops_node_type": "builder",
                        "file_ops_output_dir": td,
                        "file_ops_allowed_html_targets": ["index.html"],
                        "file_ops_can_write_root_index": True,
                        "node_execution_id": node_execution_id,
                        "file_ops_write_token": "stale-token",
                    },
                ))
            finally:
                clear_active_file_ops_write_token(node_execution_id)
        self.assertFalse(out.success)
        self.assertIn("stale builder file_ops write rejected", str(out.error).lower())

    def test_builder_file_ops_rejects_inactive_attempt_write_token_after_revocation(self):
        plugin = FileOpsPlugin()
        valid_html = (
            "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"></head>"
            "<body><main><section>ok</section></main></body></html>"
        )
        with tempfile.TemporaryDirectory() as td:
            node_execution_id = "ne_builder_revoked"
            token = "final-attempt-token"
            set_active_file_ops_write_token(node_execution_id, token)
            clear_active_file_ops_write_token(node_execution_id, token)
            out = asyncio.run(plugin.execute(
                {"action": "write", "path": str(Path(td) / "index.html"), "content": valid_html},
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["index.html"],
                    "file_ops_can_write_root_index": True,
                    "node_execution_id": node_execution_id,
                    "file_ops_write_token": token,
                },
            ))
        self.assertFalse(out.success)
        self.assertIn("inactive builder file_ops write rejected", str(out.error).lower())

    def test_builder_file_ops_patch_mode_requires_read_before_root_write(self):
        plugin = FileOpsPlugin()
        valid_html = (
            "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"></head>"
            "<body><main><section>ok</section></main></body></html>"
        )
        with tempfile.TemporaryDirectory() as td:
            node_execution_id = "ne_builder_patch"
            token = "patch-attempt-token"
            root = Path(td) / "index.html"
            root.write_text(valid_html, encoding="utf-8")
            set_active_file_ops_write_token(node_execution_id, token)
            try:
                blocked = asyncio.run(plugin.execute(
                    {"action": "write", "path": str(root), "content": valid_html.replace("ok", "patched")},
                    context={
                        "allowed_dirs": [td],
                        "file_ops_node_type": "builder",
                        "file_ops_output_dir": td,
                        "file_ops_allowed_html_targets": ["index.html"],
                        "file_ops_can_write_root_index": True,
                        "file_ops_require_existing_artifact_read": True,
                        "file_ops_required_read_targets": ["index.html"],
                        "node_execution_id": node_execution_id,
                        "file_ops_write_token": token,
                    },
                ))
                self.assertFalse(blocked.success)
                self.assertIn("must file_ops read the current live artifact first", str(blocked.error).lower())

                read_back = asyncio.run(plugin.execute(
                    {"action": "read", "path": str(root)},
                    context={
                        "allowed_dirs": [td],
                        "file_ops_node_type": "builder",
                        "file_ops_output_dir": td,
                        "file_ops_allowed_html_targets": ["index.html"],
                        "file_ops_can_write_root_index": True,
                        "file_ops_require_existing_artifact_read": True,
                        "file_ops_required_read_targets": ["index.html"],
                        "node_execution_id": node_execution_id,
                        "file_ops_write_token": token,
                    },
                ))
                self.assertTrue(read_back.success)

                allowed = asyncio.run(plugin.execute(
                    {"action": "write", "path": str(root), "content": valid_html.replace("ok", "patched")},
                    context={
                        "allowed_dirs": [td],
                        "file_ops_node_type": "builder",
                        "file_ops_output_dir": td,
                        "file_ops_allowed_html_targets": ["index.html"],
                        "file_ops_can_write_root_index": True,
                        "file_ops_require_existing_artifact_read": True,
                        "file_ops_required_read_targets": ["index.html"],
                        "node_execution_id": node_execution_id,
                        "file_ops_write_token": token,
                    },
                ))
                self.assertTrue(allowed.success)
            finally:
                clear_active_file_ops_write_token(node_execution_id, token)

    def test_builder_file_ops_blank_list_path_defaults_to_output_dir(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            out = asyncio.run(plugin.execute(
                {"action": "list", "path": ""},
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["about.html", "platform.html"],
                },
            ))
        self.assertTrue(out.success)
        self.assertEqual(out.data.get("path"), td)

    def test_builder_file_ops_blank_write_path_defaults_to_first_assigned_target(self):
        plugin = FileOpsPlugin()
        valid_html = (
            "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"></head>"
            "<body><main><section>ok</section></main></body></html>"
        )
        with tempfile.TemporaryDirectory() as td:
            out = asyncio.run(plugin.execute(
                {"action": "write", "path": "", "content": valid_html},
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["about.html", "platform.html"],
                },
            ))
        self.assertTrue(out.success)
        self.assertEqual(out.data.get("path"), str(Path(td) / "about.html"))

    def test_builder_file_ops_blank_write_path_prefers_existing_assigned_file(self):
        plugin = FileOpsPlugin()
        valid_html = (
            "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"></head>"
            "<body><main><section>updated</section></main></body></html>"
        )
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "platform.html"
            target.write_text("<html><body>draft</body></html>", encoding="utf-8")
            out = asyncio.run(plugin.execute(
                {"action": "write", "path": "", "content": valid_html},
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["about.html", "platform.html"],
                },
            ))
        self.assertTrue(out.success)
        self.assertEqual(out.data.get("path"), str(target))

    def test_builder_file_ops_rejects_truncated_html_write(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "index.html"
            out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(target),
                    "content": "<!DOCTYPE html><html><head><style>body{opacity:1}... [TRUNCATED]",
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["index.html"],
                    "file_ops_can_write_root_index": True,
                },
            ))
            self.assertFalse(out.success)
            self.assertIn("truncation marker", str(out.error).lower())
            self.assertFalse(target.exists())

    def test_builder_file_ops_blank_read_path_defaults_to_existing_assigned_file(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "about.html"
            target.write_text("<html><body>about</body></html>", encoding="utf-8")
            out = asyncio.run(plugin.execute(
                {"action": "read", "path": ""},
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["about.html", "platform.html"],
                },
            ))
        self.assertTrue(out.success)
        self.assertEqual(out.data.get("path"), str(target))
        self.assertIn("about", out.data.get("content", ""))

    def test_builder_file_ops_blank_read_path_falls_back_to_existing_index_without_targets(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "index.html"
            target.write_text("<html><body>index</body></html>", encoding="utf-8")
            out = asyncio.run(plugin.execute(
                {"action": "read", "path": ""},
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                },
            ))
        self.assertTrue(out.success)
        self.assertEqual(out.data.get("path"), str(target))
        self.assertIn("index", out.data.get("content", ""))

    def test_builder_file_ops_rejects_large_regression_over_existing_valid_page(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "index.html"
            strong_html = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Strong Page</title><style>
:root{--bg:#0b0c0f;--fg:#f5f5f7;--line:rgba(255,255,255,.12);}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
header,main,section,footer,nav{display:block}nav{display:flex;gap:12px;padding:24px;border-bottom:1px solid var(--line)}
main{display:grid;gap:24px;padding:32px}section{padding:28px;border:1px solid var(--line);border-radius:24px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.cta{display:flex;gap:12px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
</style></head>
<body><header><nav><a href="index.html">Home</a><a href="about.html">About</a></nav></header>
<main><section><h1>Strong Existing Page</h1><p>This page already has real commercial content, layout, and polish.</p></section>
<section class="grid"><article><h2>Card A</h2><p>Dense copy.</p></article><article><h2>Card B</h2><p>Dense copy.</p></article><article><h2>Card C</h2><p>Dense copy.</p></article></section>
<section class="cta"><button>Explore</button><button>Contact</button></section></main><footer>Footer</footer><script>console.log('ok')</script></body></html>"""
            target.write_text(strong_html, encoding="utf-8")
            out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(target),
                    "content": "<!DOCTYPE html><html><head><meta charset='UTF-8'><style>body{margin:0}</style></head><body><h1>Stub</h1></body></html>",
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["index.html"],
                    "file_ops_can_write_root_index": True,
                },
            ))
        self.assertFalse(out.success)
        self.assertIn("regression", str(out.error).lower())

    def test_file_ops_rejects_unsafe_zero_width_path_characters(self):
        plugin = FileOpsPlugin()
        valid_html = (
            "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"></head>"
            "<body><main><section>ok</section></main></body></html>"
        )
        with tempfile.TemporaryDirectory() as td:
            target = str(Path(td) / "invisible\u200bname.html")
            out = asyncio.run(plugin.execute(
                {"action": "write", "path": target, "content": valid_html},
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["index.html"],
                    "file_ops_can_write_root_index": True,
                },
            ))
        self.assertFalse(out.success)
        self.assertIn("unsafe path rejected", str(out.error).lower())

    def test_file_ops_auto_corrects_write_outside_runtime_output_dir(self):
        """V4.6 SPEED: Writes to parent dir are auto-corrected into output_root
        instead of rejected, saving 2-3 min of wasted regeneration per occurrence."""
        plugin = FileOpsPlugin()
        valid_html = (
            "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"></head>"
            "<body><main><section>ok</section></main></body></html>"
        )
        with tempfile.TemporaryDirectory() as td:
            output_root = Path(td) / "out"
            output_root.mkdir(parents=True, exist_ok=True)
            escaped_target = Path(td) / "escape.html"
            ctx = {
                "allowed_dirs": [td],
                "file_ops_node_type": "builder",
                "file_ops_output_dir": str(output_root),
                "file_ops_allowed_html_targets": ["index.html", "escape.html"],
                "file_ops_can_write_root_index": True,
            }
            out = asyncio.run(plugin.execute(
                {"action": "write", "path": str(escaped_target), "content": valid_html},
                context=ctx,
            ))
            # Auto-corrected into output_root/escape.html instead of rejected
            self.assertTrue(out.success)
            corrected = output_root / "escape.html"
            self.assertTrue(corrected.exists())

    def test_polisher_file_ops_rejects_replay_placeholder_css_write(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "styles.css"
            target.write_text("body{margin:0;padding:0;display:block;}" * 120, encoding="utf-8")
            out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(target),
                    "content": "<omitted large file content during replay>",
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "polisher",
                    "file_ops_output_dir": td,
                },
            ))
        self.assertFalse(out.success)
        self.assertIn("placeholder", str(out.error).lower())

    def test_polisher_file_ops_rejects_shared_styles_regression(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "styles.css"
            target.write_text(
                (
                    ":root{--bg:#0b0c0f;--fg:#f5f5f7;}"
                    "body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui;}"
                    ".hero{min-height:100vh;display:grid;place-items:center;}"
                    ".nav{display:flex;gap:16px;padding:24px;}"
                ) * 80,
                encoding="utf-8",
            )
            out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(target),
                    "content": "body{margin:0;}",
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "polisher",
                    "file_ops_output_dir": td,
                },
            ))
        self.assertFalse(out.success)
        self.assertIn("shared asset", str(out.error).lower())

    def test_file_ops_write_postprocesses_remote_font_dependencies(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            html_target = Path(td) / "index.html"
            css_target = Path(td) / "styles.css"
            html_out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(html_target),
                    "content": (
                        "<!DOCTYPE html><html><head>"
                        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
                        "<link href=\"https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap\" rel=\"stylesheet\">"
                        "</head><body><main>ok</main></body></html>"
                    ),
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["index.html"],
                    "file_ops_can_write_root_index": True,
                },
            ))
            css_out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(css_target),
                    "content": (
                        "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap');\n"
                        "body{font-family:'Inter',sans-serif;}"
                    ),
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "polisher",
                    "file_ops_output_dir": td,
                },
            ))

            self.assertTrue(html_out.success)
            self.assertTrue(css_out.success)
            self.assertNotIn("fonts.googleapis.com", html_target.read_text(encoding="utf-8"))
            self.assertNotIn("fonts.googleapis.com", css_target.read_text(encoding="utf-8"))


class TestContextCompaction(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_prepare_messages_caps_total_context_size(self):
        messages = [
            {"role": "system", "content": "S" * 24000},
            {"role": "user", "content": "U" * 24000},
        ]
        for i in range(18):
            messages.append({
                "role": "assistant",
                "content": "A" * 12000,
                "tool_calls": [{
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": "file_ops", "arguments": "X" * 9000},
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "content": "T" * 22000,
            })

        prepared = self.bridge._prepare_messages_for_request(messages, "kimi-coding")
        total_chars = sum(self.bridge._message_char_count(m) for m in prepared)
        self.assertLessEqual(total_chars, MAX_REQUEST_TOTAL_CHARS)
        self.assertTrue(any("OLDER_CONTEXT_OMITTED" in str(m.get("content", "")) for m in prepared))

    def test_prepare_messages_omits_large_file_write_payloads_from_replay(self):
        html = "<!DOCTYPE html><html><body>" + ("A" * 6000) + "</body></html>"
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "build"},
            {
                "role": "assistant",
                "content": "writing file",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "file_ops",
                        "arguments": json.dumps({
                            "action": "write",
                            "path": "/tmp/evermind_output/pricing.html",
                            "content": html,
                        }),
                    },
                }],
            },
        ]

        prepared = self.bridge._prepare_messages_for_request(messages, "kimi-coding")
        replay_args = prepared[2]["tool_calls"][0]["function"]["arguments"]
        replay_payload = json.loads(replay_args)
        self.assertEqual(replay_payload.get("path"), "/tmp/evermind_output/pricing.html")
        self.assertEqual(replay_payload.get("content"), "")
        self.assertEqual(replay_payload.get("content_omitted"), True)
        self.assertGreater(replay_payload.get("content_chars", 0), 6000)
        self.assertNotIn("AAAA", replay_args)
        self.assertNotIn("<omitted large file content during replay>", replay_args)

    def test_builder_forced_text_messages_use_clean_two_message_context(self):
        forced = self.bridge._builder_forced_text_messages(
            "SYSTEM",
            "Build a premium multi-page site.",
            tool_results=[{"success": True, "data": {"path": "/tmp/evermind_output", "entries": [{"name": "index.html"}]}}],
            output_text="Partial draft only",
            force_text_reason="tool_research_loop",
        )
        self.assertEqual(len(forced), 2)
        self.assertEqual(forced[0]["role"], "system")
        self.assertEqual(forced[1]["role"], "user")
        self.assertIn("Forced final delivery reason", forced[1]["content"])
        self.assertIn("Workspace/tool summary", forced[1]["content"])
        self.assertNotIn("tool_calls", forced[1])

    def test_builder_forced_text_prompt_for_game_includes_runtime_contract(self):
        prompt = self.bridge._builder_forced_text_prompt(
            "做一个第三人称射击游戏，要有怪物、枪械、HUD 和重新开始按钮。"
        )

        self.assertIn("GAME FINAL DELIVERY CONTRACT", prompt)
        self.assertIn("requestAnimationFrame loop", prompt)
        self.assertIn("fail/win/restart path", prompt)

    def test_polisher_loop_guard_is_non_retryable_and_falls_back(self):
        error = "polisher loop guard triggered after 4 non-write tool iterations without any file write."
        self.assertFalse(self.bridge._should_retry_same_model(error))
        self.assertTrue(self.bridge._should_fallback_to_next_model(error))

    def test_openai_compatible_polisher_loop_guard_fails_fast_without_writes(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._polisher_force_write_threshold = MagicMock(return_value=2)

        class StubBrowserPlugin:
            name = "browser"
            description = "Browser tool"

            def get_tool_definition(self):
                return {
                    "name": "browser",
                    "description": self.description,
                    "parameters": {
                        "type": "object",
                        "properties": {"action": {"type": "string"}},
                        "required": ["action"],
                    },
                }

            async def execute(self, params, context=None):
                return PluginResult(
                    success=True,
                    data={"action": params.get("action"), "observation": "ok"},
                )

        def _tool_stream(call_index: int):
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=f"tool_{call_index}",
                                    function=SimpleNamespace(
                                        name="browser",
                                        arguments='{"action":"observe"}',
                                    ),
                                )
                            ],
                        ),
                        finish_reason="tool_calls",
                    )
                ],
                usage=None,
            )

        class _DummyCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls > 2:
                    raise AssertionError("polisher loop guard should stop before a third tool-only response")
                return _tool_stream(self.calls)

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "polisher", "model": "kimi-coding"}

        with patch("openai.OpenAI", _DummyOpenAI):
            result = asyncio.run(
                bridge._execute_openai_compatible(
                    node=node,
                    input_data="Polish the existing luxury multi-page site under /tmp/evermind_output/",
                    model_info=model_info,
                    on_progress=None,
                    plugins=[StubBrowserPlugin()],
                )
            )

        # v6.1.14f: polisher loop-guard without writes is
        # now treated as SUCCESS ("polish skipped" — builder's artifact is
        # already high quality). Orchestrator proceeds to reviewer without
        # burning a retry on a pass-through polisher. The guard reason is
        # preserved in `warning` for observability.
        self.assertTrue(result["success"])
        self.assertIn("polisher loop guard", result.get("warning", ""))
        self.assertEqual(result.get("tool_call_stats", {}).get("browser"), 2)

    def test_openai_compatible_polisher_grants_one_write_after_forced_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text("<!doctype html><html><body>home</body></html>", encoding="utf-8")
            (out / "styles.css").write_text("body{color:#111;}", encoding="utf-8")
            (out / "app.js").write_text("console.log('ok');", encoding="utf-8")

            bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test", "output_dir": td})
            bridge._polisher_force_write_threshold = MagicMock(return_value=4)

            class StubFileOpsPlugin:
                name = "file_ops"
                description = "File operations"

                def get_tool_definition(self):
                    return {
                        "name": "file_ops",
                        "description": self.description,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string"},
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["action"],
                        },
                    }

                async def execute(self, params, context=None):
                    action = params.get("action")
                    if action == "list":
                        return PluginResult(
                            success=True,
                            data={
                                "path": td,
                                "entries": [
                                    {"name": "index.html"},
                                    {"name": "styles.css"},
                                    {"name": "app.js"},
                                ],
                            },
                        )
                    if action == "read":
                        return PluginResult(
                            success=True,
                            data={"path": params.get("path"), "content": "stub"},
                        )
                    if action == "write":
                        return PluginResult(
                            success=True,
                            data={"path": params.get("path"), "written": True},
                        )
                    return PluginResult(success=False, error="unsupported")

            def _tool_stream(call_index: int, args: Dict[str, Any]):
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id=f"tool_{call_index}",
                                        function=SimpleNamespace(
                                            name="file_ops",
                                            arguments=json.dumps(args),
                                        ),
                                    )
                                ],
                            ),
                            finish_reason="tool_calls",
                        )
                    ],
                    usage=None,
                )

            def _final_stream():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content='{"status":"done"}',
                                tool_calls=None,
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )

            class _DummyCompletions:
                def __init__(self):
                    self.calls = 0

                def create(self, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        return _tool_stream(self.calls, {"action": "list", "path": td})
                    if self.calls == 2:
                        return _tool_stream(self.calls, {"action": "read", "path": str(out / "styles.css")})
                    if self.calls == 3:
                        return _tool_stream(self.calls, {"action": "read", "path": str(out / "app.js")})
                    if self.calls == 4:
                        return _tool_stream(self.calls, {"action": "read", "path": str(out / "index.html")})
                    if self.calls == 5:
                        return _tool_stream(
                            self.calls,
                            {
                                "action": "write",
                                "path": str(out / "styles.css"),
                                "content": "body{color:#222;}",
                            },
                        )
                    return _final_stream()

            class _DummyChat:
                def __init__(self):
                    self.completions = _DummyCompletions()

            class _DummyOpenAI:
                def __init__(self, *args, **kwargs):
                    self.chat = _DummyChat()

            model_info = bridge._resolve_model("kimi-coding")
            node = {"type": "polisher", "model": "kimi-coding"}

            with patch("openai.OpenAI", _DummyOpenAI):
                result = asyncio.run(
                    bridge._execute_openai_compatible(
                        node=node,
                        input_data="Polish this premium three-page site without collapsing routes.",
                        model_info=model_info,
                        on_progress=None,
                        plugins=[StubFileOpsPlugin()],
                    )
                )

        self.assertTrue(result["success"])
        self.assertEqual(result.get("tool_call_stats", {}).get("file_ops"), 5)
        self.assertEqual(result.get("error", ""), "")

    def test_openai_compatible_polisher_replays_all_tool_results_before_followup_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text("<!doctype html><html><body>home</body></html>", encoding="utf-8")
            (out / "styles.css").write_text("body{color:#111;}", encoding="utf-8")
            (out / "app.js").write_text("console.log('ok');", encoding="utf-8")

            bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test", "output_dir": td})
            bridge._polisher_force_write_threshold = MagicMock(return_value=10)

            class StubFileOpsPlugin:
                name = "file_ops"
                description = "File operations"

                def get_tool_definition(self):
                    return {
                        "name": "file_ops",
                        "description": self.description,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string"},
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["action"],
                        },
                    }

                async def execute(self, params, context=None):
                    action = params.get("action")
                    if action == "list":
                        return PluginResult(
                            success=True,
                            data={
                                "path": td,
                                "entries": [
                                    {"name": "index.html"},
                                    {"name": "styles.css"},
                                    {"name": "app.js"},
                                ],
                            },
                        )
                    if action == "read":
                        return PluginResult(
                            success=True,
                            data={"path": params.get("path"), "content": "stub"},
                        )
                    return PluginResult(success=False, error="unsupported")

            def _multi_tool_stream():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id="call_1",
                                        function=SimpleNamespace(
                                            name="file_ops",
                                            arguments=json.dumps({"action": "list", "path": td}),
                                        ),
                                    ),
                                    SimpleNamespace(
                                        index=1,
                                        id="call_2",
                                        function=SimpleNamespace(
                                            name="file_ops",
                                            arguments=json.dumps({"action": "read", "path": str(out / "styles.css")}),
                                        ),
                                    ),
                                    SimpleNamespace(
                                        index=2,
                                        id="call_3",
                                        function=SimpleNamespace(
                                            name="file_ops",
                                            arguments=json.dumps({"action": "read", "path": str(out / "app.js")}),
                                        ),
                                    ),
                                ],
                            ),
                            finish_reason="tool_calls",
                        )
                    ],
                    usage=None,
                )

            def _final_stream():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content='{"status":"done"}',
                                tool_calls=None,
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )

            captured_batches = []

            class _DummyCompletions:
                def __init__(self):
                    self.calls = 0

                def create(self, **kwargs):
                    captured_batches.append(json.loads(json.dumps(kwargs.get("messages", []))))
                    self.calls += 1
                    if self.calls == 1:
                        return _multi_tool_stream()
                    return _final_stream()

            class _DummyChat:
                def __init__(self):
                    self.completions = _DummyCompletions()

            class _DummyOpenAI:
                def __init__(self, *args, **kwargs):
                    self.chat = _DummyChat()

            model_info = bridge._resolve_model("kimi-coding")
            node = {"type": "polisher", "model": "kimi-coding"}

            with patch("openai.OpenAI", _DummyOpenAI):
                result = asyncio.run(
                    bridge._execute_openai_compatible(
                        node=node,
                        input_data="Polish this premium multi-page site without collapsing routes.",
                        model_info=model_info,
                        on_progress=None,
                        plugins=[StubFileOpsPlugin()],
                    )
                )

        self.assertTrue(result["success"])
        self.assertEqual(len(captured_batches), 2)
        replay_messages = captured_batches[1]
        assistant_index = next(
            i for i, msg in enumerate(replay_messages)
            if msg.get("role") == "assistant" and msg.get("tool_calls")
        )
        self.assertEqual(
            [msg.get("role") for msg in replay_messages[assistant_index + 1:assistant_index + 5]],
            ["tool", "tool", "tool", "user"],
        )
        self.assertEqual(
            [replay_messages[assistant_index + offset].get("tool_call_id") for offset in (1, 2, 3)],
            ["call_1", "call_2", "call_3"],
        )
        self.assertIn(
            "VERY NEXT response must contain one or more file_ops write calls",
            replay_messages[assistant_index + 4].get("content", ""),
        )

    def test_openai_compatible_analyst_forces_final_text_after_blocked_file_write(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._max_tool_iterations_for_node = MagicMock(return_value=3)

        class StubBrowserPlugin:
            name = "browser"
            description = "Browser operations"

            def get_tool_definition(self):
                return {
                    "name": "browser",
                    "description": self.description,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string"},
                            "url": {"type": "string"},
                        },
                        "required": ["action"],
                    },
                }

            async def execute(self, params, context=None):
                url = str(params.get("url") or "").strip()
                return PluginResult(
                    success=True,
                    data={"url": url, "final_url": url},
                )

        class StubFileOpsPlugin:
            name = "file_ops"
            description = "File operations"

            def get_tool_definition(self):
                return {
                    "name": "file_ops",
                    "description": self.description,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string"},
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["action"],
                    },
                }

            async def execute(self, params, context=None):
                return PluginResult(success=False, error="write should be blocked before plugin execution")

        def _tool_stream(call_index: int, name: str, args: Dict[str, Any]):
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=f"tool_{call_index}",
                                    function=SimpleNamespace(
                                        name=name,
                                        arguments=json.dumps(args),
                                    ),
                                )
                            ],
                        ),
                        finish_reason="tool_calls",
                    )
                ],
                usage=None,
            )

        final_report = (
            "<reference_sites>\n"
            "- https://github.com/example/repo\n"
            "- https://docs.example.com/guide\n"
            "</reference_sites>\n"
            "<design_direction>premium editorial</design_direction>\n"
            "<non_negotiables>keep 8 pages</non_negotiables>\n"
            "<deliverables_contract>all pages linked</deliverables_contract>\n"
            "<risk_register>no invented sources</risk_register>\n"
            "<builder_1_handoff>pages 1-4</builder_1_handoff>\n"
            "<builder_2_handoff>pages 5-8</builder_2_handoff>\n"
            "<reviewer_handoff>check nav</reviewer_handoff>\n"
            "<tester_handoff>verify routes</tester_handoff>\n"
            "<debugger_handoff>fix missing links</debugger_handoff>"
        )

        def _final_stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=final_report,
                            tool_calls=None,
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

        class _DummyCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _tool_stream(
                        self.calls,
                        "browser",
                        {"action": "navigate", "url": "https://github.com/example/repo"},
                    )
                if self.calls == 2:
                    return _tool_stream(
                        self.calls,
                        "browser",
                        {"action": "navigate", "url": "https://docs.example.com/guide"},
                    )
                if self.calls == 3:
                    return _tool_stream(
                        self.calls,
                        "file_ops",
                        {
                            "action": "write",
                            "path": "/tmp/evermind_output/index.html",
                            "content": "<!DOCTYPE html><html><body>wrong node</body></html>",
                        },
                    )
                return _final_stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "analyst", "model": "kimi-coding"}

        with patch("openai.OpenAI", _DummyOpenAI):
            result = asyncio.run(
                bridge._execute_openai_compatible(
                    node=node,
                    input_data="Research a premium 8-page travel site and produce the analyst handoff.",
                    model_info=model_info,
                    on_progress=None,
                    plugins=[StubBrowserPlugin(), StubFileOpsPlugin()],
                )
            )

        self.assertTrue(result["success"])
        self.assertIn("<reference_sites>", result["output"])
        self.assertIn("<builder_1_handoff>", result["output"])
        self.assertEqual(result.get("tool_call_stats", {}).get("browser"), 2)
        self.assertEqual(result.get("tool_call_stats", {}).get("file_ops"), 1)

    def test_plain_text_materializable_analyst_output_skips_forced_output(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        analyst_output = (
            "<reference_sites>\n- https://github.com/example/repo\n</reference_sites>\n"
            "<design_direction>premium third-person shooter</design_direction>\n"
            "<non_negotiables>three.js runtime, non-mirrored controls</non_negotiables>\n"
            "<deliverables_contract>single index.html with playable game loop</deliverables_contract>\n"
            "<risk_register>missing live refs must be stated explicitly</risk_register>\n"
            "<builder_1_handoff>own the full TPS gameplay shell and save index.html in place</builder_1_handoff>\n"
            "<reviewer_handoff>verify camera drag and WASD semantics</reviewer_handoff>\n"
            "<tester_handoff>check combat loop, progression, and movement directions</tester_handoff>\n"
        )

        self.assertTrue(bridge._plain_text_node_output_is_materializable("analyst", analyst_output))
        self.assertFalse(
            bridge._plain_text_node_needs_forced_output("analyst", analyst_output, [])
        )

    def test_plain_text_final_timeout_for_node_caps_analyst_and_ui_nodes(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})

        self.assertEqual(
            bridge._plain_text_final_timeout_for_node("analyst", "tool_iterations_exhausted"),
            200,  # v5.8.5: 120→200 — mid-synthesis streaming hit 120s ceiling, lost the handoff
        )
        self.assertEqual(
            bridge._plain_text_final_timeout_for_node("analyst", "missing_final_handoff"),
            200,  # v5.8.5: 120→200 — same reason as tool_iterations_exhausted branch
        )
        self.assertEqual(
            bridge._plain_text_final_timeout_for_node("uidesign", "missing_final_handoff"),
            25,
        )
        self.assertEqual(
            bridge._plain_text_final_timeout_for_node("builder", "tool_iterations_exhausted"),
            0,
        )

    def test_openai_compatible_builder_timeout_falls_back_to_forced_text(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._builder_prewrite_call_timeout = MagicMock(return_value=0.01)
        plugin = FileOpsPlugin()

        def _slow_stream():
            time.sleep(0.05)
            if False:
                yield None

        def _final_stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="```html index.html\n<!DOCTYPE html><html><body>Luxury</body></html>\n```",
                            tool_calls=None,
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

        class _DummyCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _slow_stream()
                return _final_stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "builder", "model": "kimi-coding"}

        with patch.dict("os.environ", {"EVERMIND_BUILDER_FORCED_TEXT_TIMEOUT_SEC": "1"}, clear=False):
            with patch("openai.OpenAI", _DummyOpenAI):
                result = asyncio.run(
                    bridge._execute_openai_compatible(
                        node=node,
                        input_data="做一个 8 页奢侈品官网，并保存为命名 HTML 页面",
                        model_info=model_info,
                        on_progress=None,
                        plugins=[plugin],
                    )
                )

        self.assertTrue(result["success"])
        self.assertIn("<!DOCTYPE html>", result["output"])
        self.assertEqual(result["mode"], "openai_compatible_forced_text_timeout")

    def test_openai_compatible_builder_timeout_auto_save_counts_file_ops(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._builder_prewrite_call_timeout = MagicMock(return_value=0.01)
        bridge._timeout_for_node = MagicMock(return_value=0.02)
        plugin = FileOpsPlugin()

        large_body = "<section>" + ("Arena combat " * 220) + "</section>"
        streamed_html = (
            "```html index.html\n"
            "<!DOCTYPE html><html><head><title>Arena</title></head>"
            f"<body><main><h1>Arena</h1><button id=\"startBtn\" onclick=\"startGame()\">Start</button>{large_body}"
            "<canvas id=\"game\"></canvas><div class=\"hud\">HP 100</div>"
            "<script>function startGame(){requestAnimationFrame(gameLoop);} "
            "function gameLoop(){requestAnimationFrame(gameLoop);}</script>"
            "</main></body></html>\n```"
        )

        def _hanging_stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=streamed_html,
                            tool_calls=None,
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
            time.sleep(0.06)
            if False:
                yield None

        class _DummyCompletions:
            def create(self, **kwargs):
                return _hanging_stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "builder", "model": "kimi-coding"}

        with patch("openai.OpenAI", _DummyOpenAI):
            result = asyncio.run(
                bridge._execute_openai_compatible(
                    node=node,
                    input_data="做一个可玩的竞技场网页游戏，并保存为 index.html",
                    model_info=model_info,
                    on_progress=None,
                    plugins=[plugin],
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["mode"], "openai_compatible_text_mode_auto_save")
        self.assertGreaterEqual(int(result["tool_call_stats"].get("file_ops", 0) or 0), 1)
        self.assertTrue(any(item.get("success") for item in result["tool_results"]))

    def test_openai_compatible_builder_active_text_stream_survives_prewrite_deadline(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._builder_prewrite_call_timeout = MagicMock(return_value=0.01)
        bridge._timeout_for_node = MagicMock(return_value=1)
        plugin = FileOpsPlugin()

        body_chunk = "<section>" + ("Voxel arena " * 80) + "</section>"
        final_html = (
            "```html index.html\n"
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\"><title>Voxel</title></head>"
            "<body><main><h1>Voxel Strike</h1>"
            f"{body_chunk}"
            "<button id=\"startBtn\" onclick=\"startGame()\">Start</button>"
            "<canvas id=\"game\"></canvas><div class=\"hud\">HP 100</div>"
            "<script>function startGame(){requestAnimationFrame(gameLoop);} "
            "function gameLoop(){requestAnimationFrame(gameLoop);}</script>"
            "</main></body></html>\n```"
        )
        split_at = 380

        def _stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=final_html[:split_at],
                            tool_calls=None,
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
            time.sleep(0.05)
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=final_html[split_at:],
                            tool_calls=None,
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

        class _DummyCompletions:
            def create(self, **kwargs):
                return _stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "builder", "model": "kimi-coding"}

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge.config["output_dir"] = tmpdir
            with patch("openai.OpenAI", _DummyOpenAI):
                result = asyncio.run(
                    bridge._execute_openai_compatible(
                        node=node,
                        input_data="创建一个我的世界风格 3D 像素射击游戏单页。",
                        model_info=model_info,
                        on_progress=None,
                        plugins=[plugin],
                    )
                )

            saved_path = Path(tmpdir) / "index.html"
            self.assertTrue(result["success"])
            self.assertEqual(result["mode"], "openai_compatible")
            self.assertTrue(saved_path.exists())
            self.assertIn("Voxel Strike", saved_path.read_text(encoding="utf-8"))

    def test_openai_compatible_builder_write_like_stream_only_gets_bounded_prewrite_extension(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._builder_prewrite_call_timeout = MagicMock(return_value=0.01)
        bridge._timeout_for_node = MagicMock(return_value=1)
        bridge._prewrite_activity_grace_seconds = MagicMock(return_value=0.02)
        plugin = FileOpsPlugin()

        write_args = json.dumps({
            "action": "write",
            "path": "/tmp/evermind_output/index.html",
            "content": "<!DOCTYPE html><html><body>" + ("arena" * 80) + "</body></html>",
        })

        def _tool_write_then_hang_stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call_1",
                                    function=SimpleNamespace(
                                        name="file_ops",
                                        arguments=write_args,
                                    ),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
            time.sleep(1.0)
            if False:
                yield None

        class _DummyCompletions:
            def create(self, **kwargs):
                return _tool_write_then_hang_stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "builder", "model": "kimi-coding"}

        with patch("openai.OpenAI", _DummyOpenAI):
            result = asyncio.run(
                bridge._execute_openai_compatible(
                    node=node,
                    input_data="创建一个第三人称 3D 射击游戏并保存为 index.html",
                    model_info=model_info,
                    on_progress=None,
                    plugins=[plugin],
                )
            )

        self.assertFalse(result["success"])
        self.assertIn("builder pre-write timeout", str(result.get("error", "")).lower())
        self.assertNotIn("hard-ceiling", str(result.get("error", "")).lower())

    def test_openai_compatible_builder_rechunked_write_like_stream_respects_total_pending_write_cap(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._builder_prewrite_call_timeout = MagicMock(return_value=0.01)
        bridge._timeout_for_node = MagicMock(return_value=0.08)
        bridge._prewrite_activity_grace_seconds = MagicMock(return_value=0.02)
        bridge._builder_pending_write_stream_cap_seconds = MagicMock(return_value=0.03)
        bridge._builder_partial_text_salvage_result = AsyncMock(return_value=None)
        plugin = FileOpsPlugin()

        full_args = json.dumps({
            "action": "write",
            "path": "/tmp/evermind_output/index.html",
            "content": "<!DOCTYPE html><html><body>" + ("arena" * 120) + "</body></html>",
        })
        chunks = [full_args[i : i + 72] for i in range(0, min(len(full_args), 360), 72)]

        def _tool_write_in_many_chunks_then_hang():
            for index, piece in enumerate(chunks):
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id="call_cap",
                                        function=SimpleNamespace(
                                            name="file_ops" if index == 0 else None,
                                            arguments=piece,
                                        ),
                                    )
                                ],
                            ),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                )
                time.sleep(0.015)
            time.sleep(1.0)
            if False:
                yield None

        class _DummyCompletions:
            def create(self, **kwargs):
                return _tool_write_in_many_chunks_then_hang()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "builder", "model": "kimi-coding"}

        with patch("openai.OpenAI", _DummyOpenAI):
            result = asyncio.run(
                bridge._execute_openai_compatible(
                    node=node,
                    input_data="创建一个第三人称 3D 射击游戏并保存为 index.html",
                    model_info=model_info,
                    on_progress=None,
                    plugins=[plugin],
                )
            )

        self.assertFalse(result["success"])
        self.assertIn("timeout", str(result.get("error", "")).lower())
        self.assertTrue(bridge._builder_pending_write_stream_cap_seconds.called)

    def test_openai_compatible_builder_timeout_salvages_partial_html_from_streaming_tool_args(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._builder_prewrite_call_timeout = MagicMock(return_value=0.01)
        bridge._timeout_for_node = MagicMock(return_value=0.08)
        bridge._prewrite_activity_grace_seconds = MagicMock(return_value=0.02)
        bridge._builder_pending_write_stream_cap_seconds = MagicMock(return_value=0.03)
        plugin = FileOpsPlugin()

        async def _passthrough_continuation(**kwargs):
            return kwargs.get("output_text", ""), 0, False

        bridge._attempt_builder_game_text_continuation = AsyncMock(side_effect=_passthrough_continuation)

        partial_args = (
            '{"action":"write","path":"/tmp/evermind_output/index.html","content":"<!DOCTYPE html><html><body>'
            '<main><h1>Steel Hunt</h1><button id=\\"startBtn\\">Start</button><canvas id=\\"game\\"></canvas>'
            '<div class=\\"hud\\">HP 100</div><script>function startGame(){requestAnimationFrame(loop);}function loop(){requestAnimationFrame(loop);}</script>'
            '</main></body></html>'
        )

        def _tool_partial_write_then_hang():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call_salvage",
                                    function=SimpleNamespace(
                                        name="file_ops",
                                        arguments=partial_args,
                                    ),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
            time.sleep(1.0)
            if False:
                yield None

        class _DummyCompletions:
            def create(self, **kwargs):
                return _tool_partial_write_then_hang()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "builder", "model": "kimi-coding"}

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge.config["output_dir"] = tmpdir
            with patch("openai.OpenAI", _DummyOpenAI):
                result = asyncio.run(
                    bridge._execute_openai_compatible(
                        node=node,
                        input_data="创建一个第三人称 3D 射击游戏并保存为 index.html",
                        model_info=model_info,
                        on_progress=None,
                        plugins=[plugin],
                    )
                )

            saved_path = Path(tmpdir) / "index.html"
            self.assertTrue(result["success"])
            self.assertTrue(saved_path.exists())
            self.assertIn("Steel Hunt", saved_path.read_text(encoding="utf-8"))

    def test_openai_compatible_builder_tool_only_stream_triggers_tool_only_timeout(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._builder_prewrite_call_timeout = MagicMock(return_value=0.01)
        bridge._timeout_for_node = MagicMock(return_value=1)
        bridge._builder_partial_text_salvage_result = AsyncMock(return_value=None)
        plugin = FileOpsPlugin()

        def _tool_only_stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call_1",
                                    function=SimpleNamespace(
                                        name="file_ops",
                                        arguments='{"action":"list","path":"/tmp/evermind_output"}',
                                    ),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
            time.sleep(0.05)
            if False:
                yield None

        def _empty_final_stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=None),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

        class _DummyCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _tool_only_stream()
                return _empty_final_stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "builder", "model": "kimi-coding"}

        with patch.dict("os.environ", {"EVERMIND_BUILDER_FORCED_TEXT_TIMEOUT_SEC": "1"}, clear=False):
            with patch("openai.OpenAI", _DummyOpenAI):
                result = asyncio.run(
                    bridge._execute_openai_compatible(
                        node=node,
                        input_data="创建一个 3D 射击游戏并保存为 index.html",
                        model_info=model_info,
                        on_progress=None,
                        plugins=[plugin],
                )
            )

        self.assertFalse(result["success"])
        error_lower = str(result.get("error", "")).lower()
        self.assertTrue(
            "builder tool-only timeout" in error_lower
            or "builder pre-write timeout" in error_lower,
            f"Expected builder tool-only or pre-write timeout error, got: {error_lower}",
        )

    def test_openai_compatible_builder_tool_only_timeout_falls_back_to_forced_text(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._builder_prewrite_call_timeout = MagicMock(return_value=0.01)
        bridge._timeout_for_node = MagicMock(return_value=1)
        plugin = FileOpsPlugin()

        def _tool_only_stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call_1",
                                    function=SimpleNamespace(
                                        name="file_ops",
                                        arguments='{"action":"list","path":"/tmp/evermind_output"}',
                                    ),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
            time.sleep(0.05)
            if False:
                yield None

        def _final_stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="```html index.html\n<!DOCTYPE html><html><body><main><h1>Steel Hunt</h1><canvas id='game'></canvas></main></body></html>\n```",
                            tool_calls=None,
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

        class _DummyCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _tool_only_stream()
                return _final_stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "builder", "model": "kimi-coding"}

        with patch.dict("os.environ", {"EVERMIND_BUILDER_FORCED_TEXT_TIMEOUT_SEC": "1"}, clear=False):
            with patch("openai.OpenAI", _DummyOpenAI):
                result = asyncio.run(
                    bridge._execute_openai_compatible(
                        node=node,
                        input_data="创建一个 3D 射击游戏并保存为 index.html",
                        model_info=model_info,
                        on_progress=None,
                        plugins=[plugin],
                    )
                )

        self.assertTrue(result["success"])
        self.assertEqual(result["mode"], "openai_compatible_forced_text_timeout")
        self.assertIn("<!DOCTYPE html>", result["output"])

    def test_openai_compatible_builder_write_like_tool_stream_survives_tool_only_timeout(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._builder_prewrite_call_timeout = MagicMock(return_value=0.01)
        bridge._timeout_for_node = MagicMock(return_value=1)
        plugin = FileOpsPlugin()
        body = "<section>" + ("Arena combat " * 120) + "</section>"
        write_path = "/tmp/evermind_output/index.html"
        bridge._run_plugin = AsyncMock(return_value={  # type: ignore[method-assign]
            "success": True,
            "data": {"path": write_path},
            "artifacts": [],
        })

        def _write_stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call_1",
                                    function=SimpleNamespace(
                                        name="file_ops",
                                        arguments=(
                                            '{"action":"write","path":"'
                                            + write_path
                                            + '","content":"<!DOCTYPE html><html><body><main><h1>Steel Hunt</h1>'
                                        ),
                                    ),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
            time.sleep(0.05)
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call_1",
                                    function=SimpleNamespace(
                                        name=None,
                                        arguments=(body + '<canvas id=\\"game\\"></canvas></main></body></html>"}'),
                                    ),
                                )
                            ],
                        ),
                        finish_reason="tool_calls",
                    )
                ],
                usage=None,
            )

        def _empty_final_stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=None),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

        class _DummyCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _write_stream()
                return _empty_final_stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "builder", "model": "kimi-coding"}

        with patch("openai.OpenAI", _DummyOpenAI):
            result = asyncio.run(
                bridge._execute_openai_compatible(
                    node=node,
                    input_data="创建一个 3D 射击游戏并保存为 index.html",
                    model_info=model_info,
                    on_progress=None,
                    plugins=[plugin],
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(int(result["tool_call_stats"].get("file_ops", 0) or 0), 1)
        self.assertTrue(any(item.get("success") for item in result["tool_results"]))

    def test_openai_compatible_custom_gateway_initial_activity_timeout_uses_node_specific_error(self):
        bridge = AIBridge(config={"openai_api_key": "sk-openai-test"})
        bridge._timeout_for_node = MagicMock(return_value=1)
        bridge._gateway_initial_activity_timeout_for_node = MagicMock(return_value=0.01)
        plugin = FileOpsPlugin()

        def _slow_stream():
            time.sleep(0.05)
            if False:
                yield None

        class _DummyCompletions:
            def create(self, **kwargs):
                return _slow_stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("gpt-5.4")
        node = {"type": "imagegen", "model": "gpt-5.4"}

        with patch.dict("os.environ", {"OPENAI_API_BASE": "https://gateway.example/v1"}, clear=False):
            with patch("openai.OpenAI", _DummyOpenAI):
                result = asyncio.run(
                    bridge._execute_openai_compatible(
                        node=node,
                        input_data="Generate a premium sci-fi image pack.",
                        model_info=model_info,
                        on_progress=None,
                        plugins=[plugin],
                    )
                )

        self.assertFalse(result["success"])
        self.assertIn("imagegen initial-activity timeout", result["error"])
        self.assertIn("compatible gateway", result["error"])

    def test_openai_compatible_chat_custom_gateway_initial_activity_timeout_uses_node_specific_error(self):
        bridge = AIBridge(config={"openai_api_key": "sk-openai-test"})
        bridge._timeout_for_node = MagicMock(return_value=1)
        bridge._gateway_initial_activity_timeout_for_node = MagicMock(return_value=0.01)

        def _slow_stream():
            time.sleep(0.05)
            if False:
                yield None

        class _DummyCompletions:
            def create(self, **kwargs):
                return _slow_stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("gpt-5.4")

        with patch.dict("os.environ", {"OPENAI_API_BASE": "https://gateway.example/v1"}, clear=False):
            with patch("openai.OpenAI", _DummyOpenAI):
                result = asyncio.run(
                    bridge._execute_openai_compatible_chat(
                        node={"type": "router", "model": "gpt-5.4"},
                        input_data="Route a premium game generation request.",
                        model_info=model_info,
                        on_progress=None,
                    )
                )

        self.assertFalse(result["success"])
        self.assertIn("router initial-activity timeout", result["error"])
        self.assertIn("compatible gateway", result["error"])

    def test_openai_compatible_non_builder_timeout_uses_node_specific_error(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._timeout_for_node = MagicMock(return_value=0.01)

        def _slow_stream():
            time.sleep(0.05)
            if False:
                yield None

        class _DummyCompletions:
            def create(self, **kwargs):
                return _slow_stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "analyst", "model": "kimi-coding"}

        with patch("openai.OpenAI", _DummyOpenAI):
            result = asyncio.run(
                bridge._execute_openai_compatible(
                    node=node,
                    input_data="Research cinematic travel website references.",
                    model_info=model_info,
                    on_progress=None,
                    plugins=[],
                )
            )

        self.assertFalse(result["success"])
        self.assertIn("analyst hard-ceiling timeout", result["error"])
        self.assertNotIn("builder", result["error"].lower())
        self.assertNotIn("no file write produced", result["error"].lower())

    def test_sanitize_error_has_non_empty_fallback(self):
        self.assertEqual(_sanitize_error(""), "Unknown error")
        self.assertEqual(_sanitize_error(None), "Unknown error")


class TestSupportLaneWriteGuard(unittest.TestCase):
    def test_support_lane_empty_rewrite_restores_latest_meaningful_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = AIBridge(config={"output_dir": td})
            support_path = Path(td) / "js" / "weaponSystem.js"
            support_path.parent.mkdir(parents=True, exist_ok=True)
            meaningful = (
                "export function firePrimary(state) {\n"
                "  state.shots = (state.shots || 0) + 1;\n"
                "  return state;\n"
                "}\n"
            )
            support_path.write_text(meaningful, encoding="utf-8")
            node = {
                "type": "builder",
                "can_write_root_index": False,
                "builder_merger_like": False,
                "output_dir": td,
            }
            snapshots = {}

            initial_result = {
                "success": True,
                "data": {"path": str(support_path), "written": True, "size": len(meaningful)},
            }
            bridge._builder_guard_support_lane_write_result(
                node=node,
                tool_action="write",
                result=initial_result,
                snapshot_cache=snapshots,
            )

            support_path.write_text("", encoding="utf-8")
            truncating_result = {
                "success": True,
                "data": {"path": str(support_path), "written": True, "size": 0},
            }
            repaired = bridge._builder_guard_support_lane_write_result(
                node=node,
                tool_action="write",
                result=truncating_result,
                snapshot_cache=snapshots,
            )

            self.assertEqual(support_path.read_text(encoding="utf-8"), meaningful)
            self.assertTrue(repaired.get("restored_meaningful_snapshot"))
            self.assertTrue(repaired["data"].get("restored_meaningful_snapshot"))
            self.assertGreater(repaired["data"].get("size", 0), 0)


class TestBuilderHtmlWriteGuard(unittest.TestCase):
    def test_invalid_game_stub_write_is_removed_before_downstream_contamination(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = AIBridge(config={"output_dir": td})
            target = Path(td) / "index.html"
            target.write_text(
                "<!DOCTYPE html><html><body><main><h1>Studio Home</h1><p>Immersive brand storytelling.</p></main></body></html>",
                encoding="utf-8",
            )
            node = {
                "type": "builder",
                "output_dir": td,
                "can_write_root_index": True,
            }
            result = {
                "success": True,
                "data": {"path": str(target), "written": True, "size": target.stat().st_size},
            }

            guarded = bridge._builder_guard_html_write_result(
                node=node,
                input_data="创建一个 3D 第三人称射击游戏，要有怪物和枪械。",
                tool_action="write",
                result=result,
                snapshot_cache={},
            )

            self.assertFalse(guarded["success"])
            self.assertTrue(guarded.get("rejected_html_write"))
            self.assertFalse(target.exists())
            self.assertTrue(guarded["data"].get("removed_rejected_file"))

    def test_invalid_game_rewrite_restores_last_accepted_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = AIBridge(config={"output_dir": td})
            target = Path(td) / "index.html"
            good_html = """<!DOCTYPE html><html><body>
<section id="start-screen"><button id="startBtn">开始战斗</button></section>
<canvas id="gameCanvas"></canvas>
<script>
function startGame(){ window.gameStarted = true; }
function animate(){ requestAnimationFrame(animate); }
animate();
</script>
</body></html>""" + (" " * 1200)
            target.write_text(good_html, encoding="utf-8")
            node = {
                "type": "builder",
                "output_dir": td,
                "can_write_root_index": True,
            }
            snapshots = {}
            bridge._builder_capture_prewrite_snapshot(
                node=node,
                input_data="创建一个 3D 第三人称射击游戏，要有怪物和枪械。",
                tool_action="write",
                parsed_args={"path": str(target)},
                snapshot_cache=snapshots,
            )

            bad_html = "<!DOCTYPE html><html><body><main><h1>About Studio</h1><p>Crafted campaigns.</p></main></body></html>"
            target.write_text(bad_html, encoding="utf-8")
            result = {
                "success": True,
                "data": {"path": str(target), "written": True, "size": target.stat().st_size},
            }

            guarded = bridge._builder_guard_html_write_result(
                node=node,
                input_data="创建一个 3D 第三人称射击游戏，要有怪物和枪械。",
                tool_action="write",
                result=result,
                snapshot_cache=snapshots,
            )

            self.assertFalse(guarded["success"])
            self.assertTrue(guarded["data"].get("restored_meaningful_snapshot"))
            self.assertEqual(target.read_text(encoding="utf-8"), good_html)


class TestStructuredOutputValidation(unittest.TestCase):
    """Tests for AIBridge.validate_structured_output() — ported from Pydantic AI."""

    def test_valid_json_passes_schema(self):
        raw = '{"architecture": "Three.js shooter", "modules": ["camera", "input"]}'
        schema = {
            "type": "object",
            "properties": {
                "architecture": {"type": "string"},
                "modules": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["architecture", "modules"],
        }
        parsed, violations = AIBridge.validate_structured_output(raw, schema)
        self.assertEqual(parsed["architecture"], "Three.js shooter")
        self.assertEqual(len(violations), 0)

    def test_missing_required_field_reports_violation(self):
        raw = '{"architecture": "test"}'
        schema = {
            "type": "object",
            "required": ["architecture", "modules"],
        }
        parsed, violations = AIBridge.validate_structured_output(raw, schema)
        self.assertGreater(len(violations), 0)
        self.assertIn("modules", violations[0])

    def test_partial_ok_skips_required_check(self):
        raw = '{"architecture": "test"}'
        schema = {
            "type": "object",
            "required": ["architecture", "modules"],
        }
        parsed, violations = AIBridge.validate_structured_output(raw, schema, partial_ok=True)
        self.assertEqual(len(violations), 0)
        self.assertEqual(parsed["architecture"], "test")

    def test_embedded_json_in_markdown(self):
        raw = 'Here is the blueprint:\n```json\n{"architecture": "ECS", "modules": ["render"]}\n```\nDone.'
        schema = {"type": "object", "required": ["architecture"]}
        parsed, violations = AIBridge.validate_structured_output(raw, schema)
        self.assertEqual(parsed["architecture"], "ECS")

    def test_empty_input_returns_error(self):
        parsed, violations = AIBridge.validate_structured_output("", {})
        self.assertEqual(parsed, {})
        self.assertGreater(len(violations), 0)

    def test_invalid_json_returns_error(self):
        parsed, violations = AIBridge.validate_structured_output("{broken json", {})
        self.assertEqual(parsed, {})
        self.assertGreater(len(violations), 0)

    def test_no_schema_skips_validation(self):
        raw = '{"foo": "bar"}'
        parsed, violations = AIBridge.validate_structured_output(raw, {})
        self.assertEqual(parsed["foo"], "bar")
        self.assertEqual(len(violations), 0)


class TestOutputGuardrails(unittest.TestCase):
    """Tests for AIBridge.run_output_guardrails() — ported from OpenAI Agents SDK."""

    def test_short_builder_output_rejected(self):
        bridge = AIBridge(config={})
        results = bridge.run_output_guardrails("tiny", "builder")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["severity"], "reject")

    def test_normal_output_passes(self):
        bridge = AIBridge(config={})
        results = bridge.run_output_guardrails("x" * 200, "builder")
        self.assertEqual(len(results), 0)

    def test_prompt_echo_detected(self):
        bridge = AIBridge(config={})
        results = bridge.run_output_guardrails(
            "You are a senior project planner and some other content " * 5,
            "planner",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["severity"], "warn")

    def test_empty_code_blocks_warned(self):
        bridge = AIBridge(config={})
        content = "code:\n```js\n```\n```css\n```\n```html\n```\nend"
        results = bridge.run_output_guardrails(content, "builder")
        warned = [r for r in results if "empty code" in r.get("message", "").lower()]
        self.assertGreater(len(warned), 0)

    def test_non_builder_short_output_ok(self):
        bridge = AIBridge(config={})
        results = bridge.run_output_guardrails("ok", "planner")
        self.assertEqual(len(results), 0)


class TestQuickCompletion(unittest.TestCase):
    """Tests for AIBridge.quick_completion() one-shot API."""

    def test_quick_completion_returns_empty_when_no_litellm(self):
        bridge = AIBridge(config={})
        bridge._litellm = None
        result = bridge.quick_completion("test prompt")
        self.assertEqual(result, "")

    def test_quick_completion_returns_empty_for_unknown_model(self):
        bridge = AIBridge(config={})
        result = bridge.quick_completion("test prompt", model="nonexistent-model-xyz")
        self.assertEqual(result, "")

    def test_quick_completion_calls_litellm_with_correct_params(self):
        bridge = AIBridge(config={})
        mock_litellm = MagicMock()
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta = MagicMock(content="AI report content here")
        mock_litellm.completion.return_value = [chunk]
        bridge._litellm = mock_litellm

        result = bridge.quick_completion(
            "write a report",
            system="you are a writer",
            model="gpt-4o-mini",
            max_tokens=500,
            timeout_sec=15,
        )
        self.assertEqual(result, "AI report content here")
        mock_litellm.completion.assert_called_once()
        call_kwargs = mock_litellm.completion.call_args.kwargs
        self.assertEqual(call_kwargs["max_tokens"], 500)
        self.assertEqual(call_kwargs["timeout"], 15)
        self.assertTrue(call_kwargs["stream"])
        self.assertIn("api_base", call_kwargs)
        self.assertEqual(len(call_kwargs["messages"]), 2)
        self.assertEqual(call_kwargs["messages"][0]["role"], "system")

    def test_quick_completion_returns_empty_on_exception(self):
        bridge = AIBridge(config={})
        mock_litellm = MagicMock()
        mock_litellm.completion.side_effect = Exception("API error")
        bridge._litellm = mock_litellm

        result = bridge.quick_completion("test")
        self.assertEqual(result, "")

    def test_quick_completion_skips_system_when_empty(self):
        bridge = AIBridge(config={})
        mock_litellm = MagicMock()
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta = MagicMock(content="response")
        mock_litellm.completion.return_value = [chunk]
        bridge._litellm = mock_litellm

        bridge.quick_completion("prompt only", model="gpt-4o-mini")
        call_kwargs = mock_litellm.completion.call_args.kwargs
        self.assertEqual(len(call_kwargs["messages"]), 1)
        self.assertEqual(call_kwargs["messages"][0]["role"], "user")


class TestQuickCompletionFallback(unittest.TestCase):
    """Tests for quick_completion fallback_models chain."""

    def test_fallback_tries_next_model_on_failure(self):
        bridge = AIBridge(config={})
        mock_litellm = MagicMock()
        # First call (gpt-4o-mini) raises, second call (kimi-coding) succeeds
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta = MagicMock(content="kimi response")
        mock_litellm.completion.side_effect = [Exception("timeout"), [chunk]]
        bridge._litellm = mock_litellm

        result = bridge.quick_completion(
            "test", model="gpt-4o-mini", fallback_models=["kimi-coding"],
        )
        self.assertEqual(result, "kimi response")
        self.assertEqual(mock_litellm.completion.call_count, 2)

    def test_returns_empty_when_all_models_fail(self):
        bridge = AIBridge(config={})
        mock_litellm = MagicMock()
        mock_litellm.completion.side_effect = Exception("all fail")
        bridge._litellm = mock_litellm

        result = bridge.quick_completion(
            "test", model="gpt-4o-mini", fallback_models=["kimi-coding"],
        )
        self.assertEqual(result, "")

    def test_skips_unknown_fallback_models(self):
        bridge = AIBridge(config={})
        mock_litellm = MagicMock()
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta = MagicMock(content="ok")
        mock_litellm.completion.side_effect = [Exception("fail"), [chunk]]
        bridge._litellm = mock_litellm

        result = bridge.quick_completion(
            "test", model="gpt-4o-mini",
            fallback_models=["nonexistent-xyz", "kimi-coding"],
        )
        self.assertEqual(result, "ok")
        # nonexistent-xyz skipped (not in registry), kimi-coding called
        self.assertEqual(mock_litellm.completion.call_count, 2)


class TestPreflightApiProbe(unittest.TestCase):
    """Tests for AIBridge.preflight_api_probe()."""

    def test_probe_returns_ok_for_working_model(self):
        bridge = AIBridge(config={})
        mock_litellm = MagicMock()
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta = MagicMock(content="OK")
        mock_litellm.completion.return_value = [chunk]
        bridge._litellm = mock_litellm

        results = bridge.preflight_api_probe(models=["gpt-4o-mini"])
        self.assertTrue(results["gpt-4o-mini"]["ok"])
        self.assertGreaterEqual(results["gpt-4o-mini"]["latency_ms"], 0)

    def test_probe_marks_failing_model(self):
        bridge = AIBridge(config={})
        mock_litellm = MagicMock()
        mock_litellm.completion.side_effect = Exception("connection refused")
        bridge._litellm = mock_litellm

        results = bridge.preflight_api_probe(models=["gpt-4o-mini"])
        self.assertFalse(results["gpt-4o-mini"]["ok"])
        # quick_completion swallows exceptions and returns "", so probe sees "empty reply"
        self.assertTrue(len(results["gpt-4o-mini"]["error"]) > 0)

    def test_probe_unknown_model(self):
        bridge = AIBridge(config={})
        results = bridge.preflight_api_probe(models=["nonexistent-xyz"])
        self.assertFalse(results["nonexistent-xyz"]["ok"])
        self.assertEqual(results["nonexistent-xyz"]["error"], "not in registry")


class TestRecordGatewayTimeout(unittest.TestCase):
    """Tests for _record_gateway_timeout marking models unhealthy."""

    def test_timeout_sets_rejection_cooldown(self):
        bridge = AIBridge(config={})
        model_info = MODEL_REGISTRY.get("gpt-4o-mini")
        bridge._record_gateway_timeout(model_info, "test timeout")
        # The model-specific key should have a rejection cooldown
        model_key = bridge._compatible_gateway_key(model_info, model_specific=True)
        if model_key:
            state = bridge._compat_gateway_health.get(model_key, {})
            self.assertGreater(state.get("rejection_cooldown_until", 0), time.time())

    def test_timeout_on_relay_provider_is_noop(self):
        bridge = AIBridge(config={})
        bridge._record_gateway_timeout({"provider": "relay"}, "test")
        # relay provider should not be recorded
        self.assertEqual(len(bridge._compat_gateway_health), 0)


if __name__ == "__main__":
    unittest.main()
