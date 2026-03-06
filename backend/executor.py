"""
Evermind Backend — Node Executor
Manages workflow execution, dependency resolution, and parallel node processing.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Set

from plugins.base import Plugin, PluginRegistry, NODE_DEFAULT_PLUGINS
from ai_bridge import AIBridge, HandoffManager

logger = logging.getLogger("evermind.executor")


class NodeExecutor:
    """Executes workflow nodes with plugin capabilities and AI model integration."""

    def __init__(self, ai_bridge: AIBridge, on_event: Callable = None):
        self.ai_bridge = ai_bridge
        self.on_event = on_event  # Callback for real-time events → WebSocket
        self.running = False
        self._cancel = False
        self.handoff_mgr = HandoffManager(ai_bridge)

    async def emit(self, event_type: str, data: Dict):
        """Send real-time event to frontend via WebSocket."""
        if self.on_event:
            await self.on_event({"type": event_type, **data})

    async def execute_workflow(self, nodes: List[Dict], edges: List[Dict]):
        """Execute a complete workflow with dependency resolution."""
        self.running = True
        self._cancel = False
        done: Set[str] = set()

        await self.emit("workflow_start", {"total_nodes": len(nodes)})

        try:
            while self.running and not self._cancel:
                # Find ready nodes (all input dependencies satisfied)
                ready = [n for n in nodes
                         if n["id"] not in done
                         and n.get("status") != "error"
                         and all(self._get_source_node_id(e, edges, nodes) in done
                                 for e in self._get_input_edges(n["id"], edges))]

                if not ready:
                    # Check if all done or stuck
                    all_processed = all(n["id"] in done or n.get("status") == "error" for n in nodes)
                    if all_processed:
                        await self.emit("workflow_complete", {
                            "completed": len(done),
                            "total": len(nodes)
                        })
                    break

                # Execute ready nodes in parallel
                tasks = [self._execute_node(node, nodes, edges, done) for node in ready]
                await asyncio.gather(*tasks)

        except Exception as e:
            logger.error(f"Workflow execution error: {e}")
            await self.emit("workflow_error", {"error": str(e)})
        finally:
            self.running = False

    async def execute_single(self, node: Dict, input_data: str = "") -> Dict:
        """Execute a single node (for testing or step-by-step execution)."""
        return await self._execute_node_core(node, input_data)

    async def _execute_node(self, node: Dict, all_nodes: List[Dict],
                            edges: List[Dict], done: Set[str]):
        """Execute a single node within a workflow context."""
        node_id = node["id"]

        # Gather input from connected upstream nodes
        input_data = self._gather_inputs(node, all_nodes, edges, done)

        await self.emit("node_start", {
            "node_id": node_id,
            "node_type": node["type"],
            "node_name": node.get("name", node["type"])
        })

        try:
            result = await self._execute_node_core(node, input_data)

            # Store result
            node["lastOutput"] = result.get("output", "")
            node["status"] = "done" if result.get("success") else "error"
            node["progress"] = 100
            done.add(node_id)

            await self.emit("node_complete", {
                "node_id": node_id,
                "success": result.get("success", False),
                "output_preview": str(result.get("output", ""))[:500],
                "tool_results": result.get("tool_results", [])
            })

            return result

        except Exception as e:
            node["status"] = "error"
            await self.emit("node_error", {
                "node_id": node_id,
                "error": str(e)
            })
            return {"success": False, "error": str(e)}

    async def _execute_node_core(self, node: Dict, input_data: str) -> Dict:
        """Core node execution: resolve plugins → call AI bridge."""
        node_type = node.get("type", "")

        # Get plugins for this node
        enabled_plugins = node.get("plugins", NODE_DEFAULT_PLUGINS.get(node_type, []))
        plugins = [PluginRegistry.get(p) for p in enabled_plugins if PluginRegistry.get(p)]

        # Progress callback
        async def on_progress(data):
            await self.emit("node_progress", {
                "node_id": node.get("id"),
                **data
            })

        # For "local execution" nodes, run the plugin directly
        if node_type in ("localshell", "fileread", "filewrite", "screenshot", "gitops", "browser", "uicontrol"):
            return await self._execute_local_node(node, input_data, plugins)

        # For AI nodes, call the AI bridge with plugins as tools
        result = await self.ai_bridge.execute(
            node=node,
            plugins=plugins,
            input_data=input_data,
            model=node.get("model", "gpt-5.4"),
            on_progress=on_progress
        )

        # Router handoff: if the AI output contains a handoff target, delegate
        if node_type == "router" and result.get("success") and result.get("output"):
            try:
                import json
                parsed = json.loads(result["output"])
                if isinstance(parsed, dict) and "target" in parsed:
                    handoff_result = await self.handoff_mgr.handoff(
                        from_node=node,
                        to_node_type=parsed["target"],
                        task=parsed.get("task", input_data),
                        all_nodes=[],  # Will be filled in workflow context
                        on_progress=on_progress
                    )
                    result["handoff"] = handoff_result
            except (json.JSONDecodeError, KeyError):
                pass  # Not a handoff response, that's fine

        return result

    async def _execute_local_node(self, node: Dict, input_data: str,
                                   plugins: List[Plugin]) -> Dict:
        """Execute a local-type node by directly invoking its primary plugin."""
        node_type = node["type"]
        plugin_map = {
            "localshell": ("shell", lambda: {"command": input_data}),
            "fileread": ("file_ops", lambda: {"action": "read", "path": input_data}),
            "filewrite": ("file_ops", lambda: {"action": "write", "path": node.get("_write_path", "/tmp/output.txt"), "content": input_data}),
            "screenshot": ("screenshot", lambda: {}),
            "gitops": ("git", lambda: {"action": input_data or "status"}),
            "browser": ("browser", lambda: {"action": "navigate", "url": input_data}),
            "uicontrol": ("ui_control", lambda: {"action": "click", "x": 0, "y": 0}),
        }

        mapping = plugin_map.get(node_type)
        if not mapping:
            return {"success": False, "output": "", "error": f"No handler for {node_type}"}

        plugin_name, params_fn = mapping
        plugin = PluginRegistry.get(plugin_name)
        if not plugin:
            return {"success": False, "output": "", "error": f"Plugin {plugin_name} not loaded"}

        result = await plugin.execute(params_fn(), context=self.ai_bridge.config)
        return {
            "success": result.success,
            "output": str(result.data) if result.data else "",
            "error": result.error,
            "tool_results": [{"plugin": plugin_name, "result": result.to_dict()}]
        }

    def _get_input_edges(self, node_id: str, edges: List[Dict]) -> List[Dict]:
        """Get all edges pointing INTO this node."""
        return [e for e in edges if e["to"].startswith(node_id)]

    def _get_source_node_id(self, edge: Dict, edges: List[Dict], nodes: List[Dict]) -> str:
        """Get the source node ID for an edge."""
        from_port = edge["from"]
        for n in nodes:
            for o in n.get("outputs", []):
                if o["id"] == from_port:
                    return n["id"]
        return ""

    def _gather_inputs(self, node: Dict, all_nodes: List[Dict],
                       edges: List[Dict], done: Set[str]) -> str:
        """Gather and concatenate outputs from upstream nodes."""
        input_edges = self._get_input_edges(node["id"], edges)
        inputs = []
        for edge in input_edges:
            src_id = self._get_source_node_id(edge, edges, all_nodes)
            src_node = next((n for n in all_nodes if n["id"] == src_id), None)
            if src_node and src_node.get("lastOutput"):
                inputs.append(f"[From {src_node.get('name', src_id)}]: {src_node['lastOutput']}")
        return "\n\n".join(inputs) if inputs else node.get("_direct_input", "")

    def stop(self):
        """Stop the current workflow execution."""
        self._cancel = True
        self.running = False
