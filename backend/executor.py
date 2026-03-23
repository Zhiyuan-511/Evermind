"""
Evermind Backend — Node Executor
Manages workflow execution, dependency resolution, and parallel node processing.
"""

import asyncio
import logging
from typing import Callable, Dict, List, Optional, Set

from plugins.base import Plugin, PluginRegistry, get_default_plugins_for_node
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

    def _normalize_node(self, node: Dict) -> Dict:
        """Normalize React Flow frontend nodes into backend execution nodes."""
        normalized = dict(node)
        data = node.get("data", {}) if isinstance(node.get("data"), dict) else {}
        node_type = data.get("nodeType") or node.get("type", "")
        normalized["type"] = node_type
        normalized["name"] = node.get("name") or data.get("label") or data.get("name") or node_type
        normalized["model"] = node.get("model") or data.get("model") or "gpt-5.4"
        normalized["plugins"] = node.get("plugins") or data.get("plugins") or []
        normalized["prompt"] = node.get("prompt") or data.get("prompt")
        normalized["_write_path"] = node.get("_write_path") or data.get("_write_path")
        normalized["_direct_input"] = node.get("_direct_input") or data.get("_direct_input") or ""
        return normalized

    def _node_status(self, node: Dict) -> str:
        data = node.get("data", {}) if isinstance(node.get("data"), dict) else {}
        return node.get("status") or data.get("status") or "idle"

    def _node_name(self, node: Dict) -> str:
        data = node.get("data", {}) if isinstance(node.get("data"), dict) else {}
        return node.get("name") or data.get("label") or data.get("name") or node.get("id", "node")

    def _node_output(self, node: Dict) -> str:
        data = node.get("data", {}) if isinstance(node.get("data"), dict) else {}
        return node.get("lastOutput") or data.get("lastOutput") or ""

    def _set_node_state(self, node: Dict, *, status: str, progress: int, output: str = ""):
        node["status"] = status
        node["progress"] = progress
        if output:
            node["lastOutput"] = output
        if isinstance(node.get("data"), dict):
            node["data"] = {
                **node["data"],
                "status": status,
                "progress": progress,
                "lastOutput": output or node["data"].get("lastOutput", ""),
            }

    async def execute_workflow(self, nodes: List[Dict], edges: List[Dict]):
        """Execute a complete workflow with dependency resolution."""
        self.running = True
        self._cancel = False
        done: Set[str] = set()

        # Pre-normalize all nodes once (avoids O(N²) re-normalization per node)
        normalized_map: Dict[str, Dict] = {n["id"]: self._normalize_node(n) for n in nodes}
        normalized_list = list(normalized_map.values())

        await self.emit("workflow_start", {"total_nodes": len(nodes)})

        try:
            while self.running and not self._cancel:
                ready = [
                    n for n in nodes
                    if n["id"] not in done
                    and self._node_status(n) != "error"
                    and all(
                        self._get_source_node_id(e, edges, nodes) in done
                        for e in self._get_input_edges(n["id"], edges)
                    )
                ]

                if not ready:
                    all_processed = all(n["id"] in done or self._node_status(n) == "error" for n in nodes)
                    if all_processed:
                        await self.emit("workflow_complete", {
                            "completed": len(done),
                            "total": len(nodes)
                        })
                    break

                tasks = [self._execute_node(node, nodes, edges, done, normalized_map, normalized_list) for node in ready]
                await asyncio.gather(*tasks)

        except Exception as e:
            logger.error(f"Workflow execution error: {e}")
            await self.emit("workflow_error", {"error": str(e)})
        finally:
            self.running = False

    async def execute_single(self, node: Dict, input_data: str = "") -> Dict:
        """Execute a single node (for testing or step-by-step execution)."""
        normalized_node = self._normalize_node(node)
        return await self._execute_node_core(normalized_node, input_data)

    async def _execute_node(self, node: Dict, all_nodes: List[Dict], edges: List[Dict], done: Set[str],
                            normalized_map: Dict[str, Dict] = None, normalized_list: List[Dict] = None):
        """Execute a single node within a workflow context."""
        node_id = node["id"]
        normalized_node = normalized_map[node_id] if normalized_map else self._normalize_node(node)
        normalized_nodes = normalized_list if normalized_list else [self._normalize_node(n) for n in all_nodes]

        input_data = self._gather_inputs(node, all_nodes, edges, done)

        await self.emit("node_start", {
            "node_id": node_id,
            "node_type": normalized_node["type"],
            "node_name": normalized_node.get("name", normalized_node["type"])
        })

        # P0-2: Emit phase heartbeat so frontend knows this node is alive
        await self.emit("node_phase", {
            "node_id": node_id,
            "phase": "starting",
            "node_name": normalized_node.get("name", normalized_node["type"]),
        })
        self._set_node_state(node, status="running", progress=5)

        try:
            result = await self._execute_node_core(normalized_node, input_data, normalized_nodes)
            success = result.get("success", False)
            self._set_node_state(
                node,
                status="done" if success else "error",
                progress=100,
                output=result.get("output", ""),
            )
            # Always add to done — even on failure — so downstream nodes aren't stuck forever
            done.add(node_id)

            await self.emit("node_complete", {
                "node_id": node_id,
                "success": success,
                "output_preview": str(result.get("output", ""))[:500],
                "tool_results": result.get("tool_results", [])
            })
            return result

        except Exception as e:
            self._set_node_state(node, status="error", progress=100, output=str(e))
            # Must add to done even on exception — otherwise downstream nodes wait forever
            done.add(node_id)
            await self.emit("node_error", {
                "node_id": node_id,
                "error": str(e)
            })
            return {"success": False, "error": str(e)}

    async def _execute_node_core(self, node: Dict, input_data: str, all_nodes: Optional[List[Dict]] = None) -> Dict:
        """Core node execution: resolve plugins → call AI bridge.
        For planner nodes: auto-retries with degraded prompt on failure."""
        node_type = node.get("type", "")

        enabled_plugins = node.get("plugins") or get_default_plugins_for_node(node_type, config=self.ai_bridge.config)
        plugins = [PluginRegistry.get(p) for p in enabled_plugins if PluginRegistry.get(p)]

        async def on_progress(data):
            await self.emit("node_progress", {
                "node_id": node.get("id"),
                **data
            })

        if node_type in ("localshell", "fileread", "filewrite", "screenshot", "gitops", "browser", "uicontrol"):
            return await self._execute_local_node(node, input_data, plugins)

        result = await self.ai_bridge.execute(
            node=node,
            plugins=plugins,
            input_data=input_data,
            model=node.get("model", "gpt-5.4"),
            on_progress=on_progress
        )

        # P0-4: Planner auto-retry with degraded prompt on failure
        if node_type == "planner" and not result.get("success"):
            logger.warning(
                f"[P0-4] Planner failed (error={result.get('error', '')[:100]}), "
                f"retrying with degraded planner preset..."
            )
            await self.emit("node_phase", {
                "node_id": node.get("id"),
                "phase": "retrying_degraded",
                "node_name": node.get("name", "planner"),
            })

            # Create a degraded copy of the node that uses the emergency planner preset
            degraded_node = dict(node)
            degraded_node["type"] = "planner_degraded"

            degraded_result = await self.ai_bridge.execute(
                node=degraded_node,
                plugins=[],  # No plugins needed for pure JSON skeleton
                input_data=input_data,
                model=node.get("model", "gpt-5.4"),
                on_progress=on_progress
            )

            if degraded_result.get("success"):
                logger.info("[P0-4] Degraded planner succeeded!")
                degraded_result["mode"] = "planner_degraded_retry"
                return degraded_result
            else:
                logger.error(f"[P0-4] Degraded planner also failed: {degraded_result.get('error', '')[:100]}")
                # Return original error for clarity
                result["error"] = (
                    f"Planner failed (original: {result.get('error', 'unknown')}). "
                    f"Degraded retry also failed: {degraded_result.get('error', 'unknown')}"
                )

        if node_type == "router" and result.get("success") and result.get("output") and all_nodes:
            try:
                import json
                parsed = json.loads(result["output"])
                if isinstance(parsed, dict) and "target" in parsed:
                    handoff_result = await self.handoff_mgr.handoff(
                        from_node=node,
                        to_node_type=parsed["target"],
                        task=parsed.get("task", input_data),
                        all_nodes=all_nodes,
                        on_progress=on_progress
                    )
                    result["handoff"] = handoff_result
            except (json.JSONDecodeError, KeyError):
                pass

        return result

    async def _execute_local_node(self, node: Dict, input_data: str, plugins: List[Plugin]) -> Dict:
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
        """Get all edges pointing INTO this node, supporting React Flow and legacy schemas."""
        matched = []
        for edge in edges:
            target = edge.get("target") or edge.get("to") or ""
            # Exact match for React Flow `target`, or legacy port format `nodeId_portName`
            if target == node_id or (target.startswith(node_id) and len(target) > len(node_id) and target[len(node_id)] in ("_", "-", ":", ".")):
                matched.append(edge)
        return matched

    def _get_source_node_id(self, edge: Dict, edges: List[Dict], nodes: List[Dict]) -> str:
        """Get the source node ID for an edge, supporting React Flow and legacy port schemas."""
        if edge.get("source"):
            return edge["source"]

        from_port = edge.get("from")
        if not from_port:
            return ""

        for n in nodes:
            outputs = n.get("outputs") or n.get("data", {}).get("outputs", [])
            for output in outputs:
                if output.get("id") == from_port:
                    return n["id"]
        return ""

    def _gather_inputs(self, node: Dict, all_nodes: List[Dict], edges: List[Dict], done: Set[str]) -> str:
        """Gather and concatenate outputs from upstream nodes."""
        input_edges = self._get_input_edges(node["id"], edges)
        inputs = []
        for edge in input_edges:
            src_id = self._get_source_node_id(edge, edges, all_nodes)
            if src_id and src_id not in done:
                continue
            src_node = next((n for n in all_nodes if n["id"] == src_id), None)
            output = self._node_output(src_node) if src_node else ""
            if src_node and output:
                inputs.append(f"[From {self._node_name(src_node)}]: {output}")
        return "\n\n".join(inputs) if inputs else node.get("_direct_input", "")

    def stop(self):
        """Stop the current workflow execution."""
        self._cancel = True
        self.running = False
