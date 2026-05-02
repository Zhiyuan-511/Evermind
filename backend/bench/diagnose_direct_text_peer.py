#!/usr/bin/env python3
"""Variant of diagnose_direct_text.py that constructs a TWO-builder plan
(peer builder scenario) — this is what the user's real run hit.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import Orchestrator, Plan, SubTask
import task_classifier


def diagnose(goal: str) -> None:
    print(f"\nGOAL: {goal!r}\n{'='*70}")

    # Mimic planner's pro-mode 2-builder plan
    sb1 = SubTask(id="3", agent_type="builder",
                  description="PRIMARY BUILDER — Own the root entry (index.html). "
                              "Build: scene init, renderer, camera, core game loop. "
                              "Tech: Three.js. Output: /tmp/evermind_output/task_3/index.html",
                  depends_on=["1"])
    sb2 = SubTask(id="4", agent_type="builder",
                  description="SUPPORT BUILDER — Build non-overlapping subsystems as "
                              "separate JS modules: advanced weapon system, enemy AI "
                              "behavior trees, particle effects engine. Do NOT touch "
                              "index.html. Output support files.",
                  depends_on=["1"])
    merger = SubTask(id="5", agent_type="builder",
                     description="MERGER/INTEGRATOR — Read builder1 + builder2 and "
                                 "merge into final index.html.",
                     depends_on=["3", "4"])

    plan = Plan(goal=goal, subtasks=[sb1, sb2, merger])
    orch = Orchestrator(ai_bridge=None, executor=None)

    for st in (sb1, sb2, merger):
        print(f"\n── Subtask {st.id} ({st.description[:50]}…) ──")
        can_root = orch._builder_can_write_root_index(plan, st, goal)
        is_merger = orch._builder_is_merger_like_subtask(st)
        multifile_mark = orch._builder_direct_multifile_mode(st)
        targets = orch._builder_bootstrap_targets(plan, st)
        dt = orch._builder_execution_direct_text_mode(plan, st)
        dmf = orch._builder_execution_direct_multifile_mode(plan, st, "kimi-k2.6-code-preview")
        print(f"  can_write_root_index:  {can_root}")
        print(f"  is_merger_like:        {is_merger}")
        print(f"  direct_multifile_mark: {multifile_mark}")
        print(f"  assigned_targets:      {len(targets)}")
        print(f"  direct_text_mode:      {dt}")
        print(f"  direct_multifile_mode: {dmf}")
        if dt:
            print(f"  ✅ direct_text")
        elif dmf:
            print(f"  ✅ direct_multifile")
        else:
            print(f"  ⚠️  tool_call mode (risk: JSON truncation)")


if __name__ == "__main__":
    goal = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
        "创建一个3d射击游戏，要有怪物等等，不同的枪械武器，有关卡，"
        "通过页面，和精美的人物怪物建模，整体是第三人称视角"
    )
    diagnose(goal)
