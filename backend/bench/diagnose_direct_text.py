#!/usr/bin/env python3
"""Diagnose whether a given goal/task will activate direct_text mode.

Usage::

    python3 bench/diagnose_direct_text.py "3D 第三人称射击游戏，有武器系统和关卡"

Reports which gate conditions fire/miss so the maintainer can tell why his task
ended up in tool_call mode instead of the faster direct_text path.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import Orchestrator, Plan, SubTask
import task_classifier


def diagnose(goal: str, *, difficulty: str = "pro") -> None:
    print(f"\n{'='*70}\nGOAL: {goal!r}\nDIFFICULTY: {difficulty}\n{'='*70}")

    # Build a representative plan + subtask manually (the real planner is
    # async + hits the LLM; this mimics the shape of a peer builder task)
    subtask = SubTask(
        id="2",
        agent_type="builder",
        description=(
            "Build: scene initialization, renderer setup, camera system, "
            "player controller with input mapping, core game loop, HUD. "
            "Tech: Three.js. Output: /tmp/evermind_output/task_2/index.html."
        ),
        depends_on=["1"],
    )
    plan = Plan(goal=goal, subtasks=[subtask])

    orch = Orchestrator(ai_bridge=None, executor=None)

    # Classify
    try:
        profile = task_classifier.classify(goal)
    except Exception as exc:
        profile = None
        print(f"  !! task_classifier failed: {exc}")

    print("\n── Classification ──")
    if profile is not None:
        print(f"  task_type:          {getattr(profile, 'task_type', '?')}")
    print(f"  premium_3d_first_pass: {task_classifier.premium_3d_builder_direct_text_first_pass(goal)}")
    print(f"  game_direct_text:      {task_classifier.game_direct_text_delivery_mode(goal)}")
    print(f"  multi_page_website:    {task_classifier.wants_multi_page(goal)}")

    print("\n── Gate evaluation ──")
    try:
        can_root = orch._builder_can_write_root_index(plan, subtask, goal)
    except Exception as exc:
        can_root = f"ERROR: {exc}"
    print(f"  can_write_root_index:  {can_root}")
    try:
        is_merger = orch._builder_is_merger_like_subtask(subtask)
    except Exception:
        is_merger = False
    print(f"  is_merger_like:        {is_merger}")
    try:
        is_multifile = orch._builder_direct_multifile_mode(subtask)
    except Exception:
        is_multifile = False
    print(f"  direct_multifile_mark: {is_multifile}")
    try:
        targets = orch._builder_bootstrap_targets(plan, subtask)
    except Exception:
        targets = []
    print(f"  assigned_targets:      {len(targets)}")

    try:
        is_specific = orch._planner_task_specific_enough_for_direct_text(subtask)
    except Exception:
        is_specific = False
    print(f"  planner_brief_specific: {is_specific}")

    print("\n── Final decision ──")
    try:
        dt_mode = orch._builder_execution_direct_text_mode(plan, subtask)
    except Exception as exc:
        dt_mode = f"ERROR: {exc}"
    try:
        dmf_mode = orch._builder_execution_direct_multifile_mode(
            plan, subtask, model="kimi-k2.6-code-preview",
        )
    except Exception as exc:
        dmf_mode = f"ERROR: {exc}"
    print(f"  direct_text_mode:      {dt_mode}")
    print(f"  direct_multifile_mode: {dmf_mode}")

    print("\n── Env overrides ──")
    print(f"  EVERMIND_BUILDER_FORCE_DIRECT_TEXT: {os.getenv('EVERMIND_BUILDER_FORCE_DIRECT_TEXT', '(unset)')}")
    print(f"  EVERMIND_GLM_FORCE_NONSTREAM:       {os.getenv('EVERMIND_GLM_FORCE_NONSTREAM', '(unset)')}")
    print(f"  EVERMIND_STREAM_TO_DISK:            {os.getenv('EVERMIND_STREAM_TO_DISK', '(default=1)')}")

    print("\n── Verdict ──")
    if dt_mode is True:
        print("  ✅ direct_text WILL activate — fastest path, bypasses JSON wrapper.")
    elif dmf_mode is True:
        print("  ✅ direct_multifile WILL activate — streams <file> tags + parser.")
    else:
        print("  ⚠️  Neither direct_text nor direct_multifile will fire.")
        print("     Builder falls back to tool_call mode (Kimi/MiniMax JSON-wrap → risk truncation).")
        print("     To force direct_text: EVERMIND_BUILDER_FORCE_DIRECT_TEXT=1")
    print()


if __name__ == "__main__":
    goal = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
        "创建一个3d射击游戏，要有怪物等等，不同的枪械武器，有关卡，"
        "通过页面，和精美的人物怪物建模，整体是第三人称视角，画面面精美"
    )
    diagnose(goal)
