import asyncio
import pathlib
import tempfile
import unittest

from agentic_runtime import (
    AgenticLoop,
    AgenticTool,
    FunctionAgenticTool,
    LoopConfig,
    ToolRegistry,
    get_tool_registry,
    get_tools_for_role,
)
from agentic_tools import TOOL_DEFINITIONS, tool_context_compress, tool_file_edit


class DummyTool(AgenticTool):
    name = "dummy_tool"
    description = "Dummy tool for agentic runtime tests."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, arguments, context):
        return "dummy-result"


class ExplodingTool(AgenticTool):
    name = "exploding_tool"
    description = "Raises unexpectedly."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, arguments, context):
        raise RuntimeError("boom")


class TestAgenticRuntimeRoleTools(unittest.TestCase):
    def test_builder_role_exposes_file_list_when_registered(self):
        registry = get_tool_registry()
        actual = [tool.name for tool in registry.list_tools(get_tools_for_role("builder"))]
        self.assertIn("file_list", actual)

    def test_reviewer_role_exposes_file_list_when_registered(self):
        registry = get_tool_registry()
        actual = [tool.name for tool in registry.list_tools(get_tools_for_role("reviewer"))]
        self.assertIn("file_list", actual)


class TestAgenticLoopToolTimeline(unittest.TestCase):
    def test_tool_results_include_start_and_end_timestamps(self):
        async def _run():
            tool_registry = ToolRegistry()
            tool_registry.register(DummyTool())

            calls = {"count": 0}

            async def llm_call(messages, tools=None, tool_choice=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "tc_1",
                                    "function": {
                                        "name": "dummy_tool",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                    }
                return {
                    "message": {"content": "done", "tool_calls": []},
                    "usage": {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8},
                }

            loop = AgenticLoop(
                LoopConfig(
                    max_iterations=4,
                    max_tool_calls=4,
                    timeout_seconds=5,
                    allowed_tools=["dummy_tool"],
                    node_type="builder",
                    node_key="builder1",
                ),
                tool_registry,
                llm_call,
            )
            return await loop.run("system", "user")

        result = asyncio.run(_run())
        self.assertTrue(result["success"])
        self.assertEqual(len(result["tool_results"]), 1)
        tool_result = result["tool_results"][0]
        self.assertGreater(tool_result["started_at"], 0)
        self.assertGreaterEqual(tool_result["completed_at"], tool_result["started_at"])
        self.assertGreaterEqual(tool_result["duration_ms"], 0)

    def test_tool_result_failures_are_recorded_as_errors(self):
        async def _run():
            tool_registry = ToolRegistry()
            tool_registry.register(
                FunctionAgenticTool(
                    "context_compress",
                    tool_context_compress,
                    TOOL_DEFINITIONS["context_compress"],
                )
            )

            calls = {"count": 0}

            async def llm_call(messages, tools=None, tool_choice=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "tc_1",
                                    "function": {
                                        "name": "context_compress",
                                        "arguments": '{"level":"BAD"}',
                                    },
                                }
                            ],
                        },
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                    }
                return {
                    "message": {"content": "done", "tool_calls": []},
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
                }

            loop = AgenticLoop(
                LoopConfig(
                    max_iterations=3,
                    max_tool_calls=3,
                    timeout_seconds=5,
                    allowed_tools=["context_compress"],
                    node_type="builder",
                    node_key="builder1",
                ),
                tool_registry,
                llm_call,
            )
            return await loop.run("system", "user")

        result = asyncio.run(_run())
        self.assertTrue(result["tool_results"])
        self.assertIn("Invalid level", result["tool_results"][0]["error"])

    def test_loop_detection_marks_result_exhausted(self):
        async def _run():
            tool_registry = ToolRegistry()
            tool_registry.register(DummyTool())

            async def llm_call(messages, tools=None, tool_choice=None):
                return {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": f"tc_{len(messages)}",
                                "function": {"name": "dummy_tool", "arguments": "{}"},
                            }
                        ],
                    },
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }

            loop = AgenticLoop(
                LoopConfig(
                    max_iterations=20,
                    max_tool_calls=20,
                    timeout_seconds=5,
                    allowed_tools=["dummy_tool"],
                    node_type="builder",
                    node_key="builder1",
                ),
                tool_registry,
                llm_call,
            )
            return await loop.run("system", "user")

        result = asyncio.run(_run())
        self.assertFalse(result["success"])
        self.assertTrue(result["exhausted"])
        self.assertEqual(result["exhaustion_reason"], "loop_detected")

    def test_max_tool_calls_is_enforced_mid_iteration(self):
        async def _run():
            tool_registry = ToolRegistry()
            tool_registry.register(DummyTool())

            calls = {"count": 0}

            async def llm_call(messages, tools=None, tool_choice=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "tc_1",
                                    "function": {"name": "dummy_tool", "arguments": "{}"},
                                },
                                {
                                    "id": "tc_2",
                                    "function": {"name": "dummy_tool", "arguments": "{}"},
                                },
                            ],
                        },
                        "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
                    }
                return {
                    "message": {"content": "done", "tool_calls": []},
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }

            loop = AgenticLoop(
                LoopConfig(
                    max_iterations=3,
                    max_tool_calls=1,
                    timeout_seconds=5,
                    allowed_tools=["dummy_tool"],
                    node_type="builder",
                    node_key="builder1",
                ),
                tool_registry,
                llm_call,
            )
            return await loop.run("system", "user")

        result = asyncio.run(_run())
        self.assertFalse(result["success"])
        self.assertTrue(result["exhausted"])
        self.assertEqual(result["exhaustion_reason"], "max_tool_calls")
        self.assertEqual(len(result["tool_results"]), 1)

    def test_file_edit_create_is_tracked_as_created_file(self):
        async def _run():
            tool_registry = ToolRegistry()
            tool_registry.register(
                FunctionAgenticTool("file_edit", tool_file_edit, TOOL_DEFINITIONS["file_edit"])
            )
            target = pathlib.Path(tempfile.mkdtemp()) / "created.txt"

            calls = {"count": 0}

            async def llm_call(messages, tools=None, tool_choice=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "tc_1",
                                    "function": {
                                        "name": "file_edit",
                                        "arguments": f'{{"file_path":"{target}","old_string":"","new_string":"hello"}}',
                                    },
                                }
                            ],
                        },
                        "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
                    }
                return {
                    "message": {"content": "done", "tool_calls": []},
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }

            loop = AgenticLoop(
                LoopConfig(
                    max_iterations=3,
                    max_tool_calls=3,
                    timeout_seconds=5,
                    allowed_tools=["file_edit"],
                    node_type="builder",
                    node_key="builder1",
                ),
                tool_registry,
                llm_call,
            )
            return target, await loop.run("system", "user")

        target, result = asyncio.run(_run())
        self.assertTrue(target.exists())
        self.assertIn(str(target), result["files_created"])
        self.assertNotIn(str(target), result["files_modified"])

    def test_unexpected_tool_exception_is_recorded_as_error(self):
        async def _run():
            tool_registry = ToolRegistry()
            tool_registry.register(ExplodingTool())

            calls = {"count": 0}

            async def llm_call(messages, tools=None, tool_choice=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "tc_1",
                                    "function": {"name": "exploding_tool", "arguments": "{}"},
                                }
                            ],
                        },
                        "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
                    }
                return {
                    "message": {"content": "done", "tool_calls": []},
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }

            loop = AgenticLoop(
                LoopConfig(
                    max_iterations=3,
                    max_tool_calls=3,
                    timeout_seconds=5,
                    allowed_tools=["exploding_tool"],
                    node_type="builder",
                    node_key="builder1",
                ),
                tool_registry,
                llm_call,
            )
            return await loop.run("system", "user")

        result = asyncio.run(_run())
        self.assertTrue(result["tool_results"])
        self.assertIn("boom", result["tool_results"][0]["error"])


if __name__ == "__main__":
    unittest.main()
