#!/usr/bin/env python3

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_skills import list_available_skill_names, resolve_skill_names_for_goal


CASES = [
    ("builder", "做一个带动画和插画 hero 的品牌官网"),
    ("builder", "做一个融资路演 PPT slides"),
    ("scribe", "写一份 API documentation 和 README"),
    ("imagegen", "生成一张品牌海报和封面图片"),
    ("spritesheet", "生成 pixel art 游戏素材和 spritesheet"),
    ("reviewer", "做一个可以玩的贪吃蛇小游戏"),
]


def main() -> None:
    skills = list_available_skill_names()
    print(f"available_skills={len(skills)}")
    for name in skills:
        print(f"  - {name}")
    print("\nselection_samples:")
    for node_type, goal in CASES:
        selected = resolve_skill_names_for_goal(node_type, goal)
        print(f"[{node_type}] {goal}")
        print(f"  -> {', '.join(selected) if selected else '(none)'}")


if __name__ == "__main__":
    main()
