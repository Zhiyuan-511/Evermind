"""
Evermind Backend — Autonomous Orchestrator
The brain of the multi-agent system.

Flow: User Goal → Plan → Distribute → Execute → Test → Retry/Complete

Inspired by:
  - Dify workflow engine (visual DAG execution)
  - CrewAI (role-based agent collaboration)
  - OpenAI Agents SDK (handoffs + guardrails)
  - Cursor/Antigravity (code → test → fix loop)
"""

import asyncio
import json
import logging
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger("evermind.orchestrator")


class TaskStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    IN_PROGRESS = "in_progress"
    TESTING = "testing"
    FAILED = "failed"
    RETRYING = "retrying"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class SubTask:
    id: str
    agent_type: str  # builder, tester, reviewer, deployer, etc.
    description: str
    depends_on: List[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    output: str = ""
    error: str = ""
    retries: int = 0
    max_retries: int = 3
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0


@dataclass
class Plan:
    goal: str
    subtasks: List[SubTask] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    current_phase: int = 0
    total_retries: int = 0
    max_total_retries: int = 10
    created_at: float = field(default_factory=time.time)


class Orchestrator:
    """
    Autonomous multi-agent orchestrator.

    User sends a goal → Orchestrator:
    1. PLAN: AI breaks goal into subtasks with agent assignments
    2. DISTRIBUTE: Resolve dependencies, find ready subtasks
    3. EXECUTE: Run subtasks through appropriate agents (Builder, Tester, etc.)
    4. TEST: Run tester agent to verify results
    5. RETRY: If tests fail, feed errors back to builder, retry
    6. COMPLETE: All subtasks done → report results
    """

    def __init__(self, ai_bridge, executor, on_event: Callable = None):
        self.ai_bridge = ai_bridge
        self.executor = executor
        self.on_event = on_event
        self.active_plan: Optional[Plan] = None
        self._cancel = False

    async def emit(self, event_type: str, data: Dict):
        if self.on_event:
            await self.on_event({"type": event_type, **data})

    # ═══════════════════════════════════════════
    # Main entry point
    # ═══════════════════════════════════════════
    async def run(self, goal: str, model: str = "gpt-5.4") -> Dict:
        """
        Execute a user goal autonomously.
        Returns full execution report.
        """
        self._cancel = False
        logger.info(f"Orchestrator starting: {goal[:80]}...")
        await self.emit("orchestrator_start", {"goal": goal})

        try:
            # ── Phase 1: PLAN ──
            plan = await self._plan(goal, model)
            self.active_plan = plan

            if not plan.subtasks:
                return {"success": False, "error": "Failed to create plan", "plan": None}

            await self.emit("plan_created", {
                "subtasks": [{"id": st.id, "agent": st.agent_type, "task": st.description,
                              "depends_on": st.depends_on} for st in plan.subtasks],
                "total": len(plan.subtasks)
            })

            # ── Phase 2-5: EXECUTE loop ──
            result = await self._execute_plan(plan, model)

            # ── Phase 6: REPORT ──
            report = self._build_report(plan, result)
            await self.emit("orchestrator_complete", report)
            return report

        except Exception as e:
            logger.error(f"Orchestrator error: {e}")
            await self.emit("orchestrator_error", {"error": str(e)})
            return {"success": False, "error": str(e)}

    # ═══════════════════════════════════════════
    # Phase 1: PLAN — AI decomposes the goal
    # ═══════════════════════════════════════════
    async def _plan(self, goal: str, model: str) -> Plan:
        """Use AI to break down the goal into subtasks."""
        await self.emit("phase_change", {"phase": "planning", "message": "AI is analyzing the goal..."})

        planner_node = {
            "type": "router",
            "prompt": (
                "You are a task planner for a multi-agent software development system.\n"
                "Break down the user's goal into concrete subtasks.\n"
                "Available agent types: builder (writes code), tester (tests code), reviewer (reviews code), "
                "deployer (deploys), debugger (fixes bugs), scribe (writes docs).\n\n"
                "Output ONLY a valid JSON object:\n"
                '{"subtasks": [\n'
                '  {"id": "1", "agent": "builder", "task": "description...", "depends_on": []},\n'
                '  {"id": "2", "agent": "tester", "task": "test the code from task 1", "depends_on": ["1"]},\n'
                '  {"id": "3", "agent": "reviewer", "task": "review code quality", "depends_on": ["1"]}\n'
                "]}\n\n"
                "Rules:\n"
                "- Every plan MUST end with a tester task to verify the result\n"
                "- Use depends_on to set execution order\n"
                "- Be specific in task descriptions\n"
                "- Keep it practical (3-8 subtasks usually)"
            ),
            "model": model
        }

        result = await self.ai_bridge.execute(
            node=planner_node, plugins=[], input_data=f"Goal: {goal}",
            model=model, on_progress=lambda d: self.emit("planning_progress", d)
        )

        plan = Plan(goal=goal)

        if result.get("success") and result.get("output"):
            try:
                # Extract JSON from response
                raw = result["output"]
                # Try to find JSON in the response
                json_start = raw.find("{")
                json_end = raw.rfind("}") + 1
                if json_start >= 0 and json_end > json_start:
                    parsed = json.loads(raw[json_start:json_end])
                    for st in parsed.get("subtasks", []):
                        plan.subtasks.append(SubTask(
                            id=str(st.get("id", len(plan.subtasks) + 1)),
                            agent_type=st.get("agent", "builder"),
                            description=st.get("task", ""),
                            depends_on=[str(d) for d in st.get("depends_on", [])]
                        ))
            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"Plan parsing error: {e}")
                # Fallback: single builder task + single tester task
                plan.subtasks = [
                    SubTask(id="1", agent_type="builder", description=goal),
                    SubTask(id="2", agent_type="tester", description=f"Test the result of: {goal}", depends_on=["1"]),
                ]

        if not plan.subtasks:
            plan.subtasks = [
                SubTask(id="1", agent_type="builder", description=goal),
                SubTask(id="2", agent_type="tester", description=f"Test: {goal}", depends_on=["1"]),
            ]

        plan.status = TaskStatus.IN_PROGRESS
        return plan

    # ═══════════════════════════════════════════
    # Phase 2-5: EXECUTE with retry loop
    # ═══════════════════════════════════════════
    async def _execute_plan(self, plan: Plan, model: str) -> Dict:
        """Execute all subtasks with dependency resolution and retry on test failure."""
        results = {}
        completed = set()

        while not self._cancel:
            # Find subtasks ready to execute (all deps satisfied)
            ready = [
                st for st in plan.subtasks
                if st.id not in completed
                and st.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)
                and all(d in completed for d in st.depends_on)
            ]

            if not ready:
                # Check if done or stuck
                all_done = all(
                    st.id in completed or st.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)
                    for st in plan.subtasks
                )
                if all_done:
                    break
                # Stuck — dependencies can't be satisfied
                stuck = [st for st in plan.subtasks if st.id not in completed and st.status != TaskStatus.CANCELLED]
                if stuck:
                    logger.warning(f"Stuck subtasks: {[s.id for s in stuck]}")
                break

            # Execute ready subtasks (parallel when no deps between them)
            await self.emit("phase_change", {
                "phase": "executing",
                "message": f"Running {len(ready)} subtask(s)...",
                "subtasks": [st.id for st in ready]
            })

            tasks = [self._execute_subtask(st, plan, model, results) for st in ready]
            subtask_results = await asyncio.gather(*tasks, return_exceptions=True)

            for st, result in zip(ready, subtask_results):
                if isinstance(result, Exception):
                    st.status = TaskStatus.FAILED
                    st.error = str(result)
                    results[st.id] = {"success": False, "error": str(result)}
                    completed.add(st.id)
                elif result.get("success"):
                    st.status = TaskStatus.COMPLETED
                    st.output = result.get("output", "")
                    st.completed_at = time.time()
                    results[st.id] = result
                    completed.add(st.id)
                else:
                    # Failed — attempt retry
                    retry_ok = await self._handle_failure(st, plan, model, results)
                    if retry_ok:
                        results[st.id] = {"success": True, "output": st.output, "retried": True}
                        completed.add(st.id)
                    else:
                        results[st.id] = {"success": False, "error": st.error}
                        completed.add(st.id)

            # Check for test failures → trigger retry loop
            for st in plan.subtasks:
                if st.agent_type == "tester" and st.status == TaskStatus.COMPLETED:
                    test_result = self._parse_test_result(st.output)
                    if test_result.get("status") == "fail":
                        await self._retry_from_failure(plan, st, test_result, model, results, completed)

        return results

    async def _execute_subtask(self, subtask: SubTask, plan: Plan, model: str, prev_results: Dict) -> Dict:
        """Execute a single subtask through the appropriate agent."""
        subtask.status = TaskStatus.IN_PROGRESS

        await self.emit("subtask_start", {
            "subtask_id": subtask.id,
            "agent": subtask.agent_type,
            "task": subtask.description[:200]
        })

        # Build context from dependency outputs
        context_parts = []
        for dep_id in subtask.depends_on:
            dep_result = prev_results.get(dep_id, {})
            dep_task = next((s for s in plan.subtasks if s.id == dep_id), None)
            if dep_task and dep_result.get("output"):
                context_parts.append(f"[Result from {dep_task.agent_type} #{dep_id}]:\n{dep_result['output'][:2000]}")

        context = "\n\n".join(context_parts)
        full_input = f"{subtask.description}\n\n{context}" if context else subtask.description

        # Create a virtual node for the agent
        from plugins.base import NODE_DEFAULT_PLUGINS
        agent_node = {
            "type": subtask.agent_type,
            "model": model,
            "id": f"auto_{subtask.id}",
            "name": f"{subtask.agent_type.title()} #{subtask.id}",
        }

        enabled = NODE_DEFAULT_PLUGINS.get(subtask.agent_type, [])
        plugins = [PluginRegistry.get(p) for p in enabled if PluginRegistry.get(p)]

        async def on_progress(data):
            await self.emit("subtask_progress", {"subtask_id": subtask.id, **data})

        result = await self.ai_bridge.execute(
            node=agent_node, plugins=plugins, input_data=full_input,
            model=model, on_progress=on_progress
        )

        await self.emit("subtask_complete", {
            "subtask_id": subtask.id,
            "success": result.get("success", False),
            "output_preview": str(result.get("output", ""))[:500]
        })

        return result

    # ═══════════════════════════════════════════
    # Retry Logic — the key differentiator
    # ═══════════════════════════════════════════
    async def _handle_failure(self, subtask: SubTask, plan: Plan, model: str, results: Dict) -> bool:
        """Handle a failed subtask — retry with error context."""
        if subtask.retries >= subtask.max_retries:
            logger.warning(f"Subtask {subtask.id} exceeded max retries ({subtask.max_retries})")
            subtask.status = TaskStatus.FAILED
            return False

        subtask.retries += 1
        plan.total_retries += 1
        subtask.status = TaskStatus.RETRYING

        await self.emit("subtask_retry", {
            "subtask_id": subtask.id,
            "retry": subtask.retries,
            "max_retries": subtask.max_retries,
            "error": subtask.error[:200]
        })

        # Re-execute with error context
        enhanced_input = (
            f"{subtask.description}\n\n"
            f"⚠️ PREVIOUS ATTEMPT FAILED (retry {subtask.retries}/{subtask.max_retries}):\n"
            f"Error: {subtask.error}\n\n"
            f"Please fix the issue and try again. Be more careful this time."
        )

        from plugins.base import NODE_DEFAULT_PLUGINS, PluginRegistry
        agent_node = {"type": subtask.agent_type, "model": model, "id": f"auto_{subtask.id}_r{subtask.retries}",
                      "name": f"{subtask.agent_type.title()} #{subtask.id} (retry {subtask.retries})"}
        enabled = NODE_DEFAULT_PLUGINS.get(subtask.agent_type, [])
        plugins = [PluginRegistry.get(p) for p in enabled if PluginRegistry.get(p)]

        result = await self.ai_bridge.execute(
            node=agent_node, plugins=plugins, input_data=enhanced_input,
            model=model, on_progress=lambda d: self.emit("subtask_progress", {"subtask_id": subtask.id, **d})
        )

        if result.get("success"):
            subtask.status = TaskStatus.COMPLETED
            subtask.output = result.get("output", "")
            subtask.completed_at = time.time()
            return True
        else:
            subtask.error = result.get("error", "Unknown error")
            return await self._handle_failure(subtask, plan, model, results)  # Recursive retry

    async def _retry_from_failure(self, plan: Plan, test_task: SubTask, test_result: Dict,
                                  model: str, results: Dict, completed: set):
        """When a test fails, go back and re-run the builder that produced the code."""
        if plan.total_retries >= plan.max_total_retries:
            await self.emit("orchestrator_max_retries", {"total_retries": plan.total_retries})
            return

        await self.emit("test_failed_retrying", {
            "test_task_id": test_task.id,
            "errors": test_result.get("errors", []),
            "suggestion": test_result.get("suggestion", "")
        })

        # Find the builder tasks that the test depends on
        builder_deps = [
            st for st in plan.subtasks
            if st.id in test_task.depends_on and st.agent_type in ("builder", "debugger")
        ]

        for builder_task in builder_deps:
            if builder_task.retries >= builder_task.max_retries:
                continue

            # Re-run the builder with test failure context
            builder_task.retries += 1
            plan.total_retries += 1
            builder_task.status = TaskStatus.RETRYING
            completed.discard(builder_task.id)
            completed.discard(test_task.id)

            await self.emit("subtask_retry", {
                "subtask_id": builder_task.id,
                "retry": builder_task.retries,
                "reason": "test_failed",
                "test_errors": test_result.get("errors", [])[:3]
            })

            enhanced_input = (
                f"{builder_task.description}\n\n"
                f"🔴 THE TESTS FAILED! Here's what went wrong:\n"
                f"Errors: {json.dumps(test_result.get('errors', []))}\n"
                f"Suggestion: {test_result.get('suggestion', 'Review and fix the code')}\n\n"
                f"Previous output:\n{builder_task.output[:1500]}\n\n"
                f"Fix the issues and provide the corrected code."
            )

            from plugins.base import NODE_DEFAULT_PLUGINS
            agent_node = {"type": builder_task.agent_type, "model": model,
                          "id": f"auto_{builder_task.id}_fix{builder_task.retries}",
                          "name": f"Debugger #{builder_task.id} (fix attempt {builder_task.retries})"}
            enabled = NODE_DEFAULT_PLUGINS.get(builder_task.agent_type, [])
            plugins = [PluginRegistry.get(p) for p in enabled if PluginRegistry.get(p)]

            result = await self.ai_bridge.execute(
                node=agent_node, plugins=plugins, input_data=enhanced_input,
                model=model, on_progress=lambda d: self.emit("subtask_progress", {"subtask_id": builder_task.id, **d})
            )

            if result.get("success"):
                builder_task.status = TaskStatus.COMPLETED
                builder_task.output = result.get("output", "")
                results[builder_task.id] = result
                completed.add(builder_task.id)

                # Re-run the test
                test_task.status = TaskStatus.PENDING
                test_task.output = ""

    def _parse_test_result(self, output: str) -> Dict:
        """Parse tester agent output for pass/fail status."""
        try:
            json_start = output.find("{")
            json_end = output.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                return json.loads(output[json_start:json_end])
        except (json.JSONDecodeError, ValueError):
            pass
        # Heuristic check
        lower = output.lower()
        if any(w in lower for w in ["fail", "error", "bug", "broken", "exception"]):
            return {"status": "fail", "errors": [output[:500]], "suggestion": "Review output"}
        return {"status": "pass", "details": output[:500]}

    # ═══════════════════════════════════════════
    # Report
    # ═══════════════════════════════════════════
    def _build_report(self, plan: Plan, results: Dict) -> Dict:
        success_count = sum(1 for st in plan.subtasks if st.status == TaskStatus.COMPLETED)
        fail_count = sum(1 for st in plan.subtasks if st.status == TaskStatus.FAILED)
        return {
            "success": fail_count == 0,
            "goal": plan.goal,
            "total_subtasks": len(plan.subtasks),
            "completed": success_count,
            "failed": fail_count,
            "total_retries": plan.total_retries,
            "duration_seconds": round(time.time() - plan.created_at, 1),
            "subtasks": [
                {
                    "id": st.id, "agent": st.agent_type, "task": st.description,
                    "status": st.status.value, "retries": st.retries,
                    "output_preview": st.output[:300] if st.output else "",
                    "error": st.error[:200] if st.error else ""
                }
                for st in plan.subtasks
            ],
            "results": {k: {"success": v.get("success"), "output_len": len(str(v.get("output", "")))}
                        for k, v in results.items()}
        }

    def stop(self):
        """Cancel the current execution."""
        self._cancel = True
