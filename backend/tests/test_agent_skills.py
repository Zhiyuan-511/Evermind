import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import agent_skills
from agent_skills import (
    build_skill_context,
    list_available_skill_names,
    list_skill_catalog,
    resolve_skill_names_for_goal,
)


class TestAgentSkills(unittest.TestCase):
    def test_skill_catalog_includes_new_skill_families(self):
        names = set(list_available_skill_names())
        self.assertIn("slides-story-arc", names)
        self.assertIn("docs-clarity-architecture", names)
        self.assertIn("image-prompt-director", names)
        self.assertIn("motion-choreography-system", names)
        self.assertIn("pixel-asset-pipeline", names)
        self.assertIn("pptx-export-bridge", names)
        self.assertIn("comfyui-pipeline-brief", names)
        self.assertIn("remotion-scene-composer", names)
        self.assertIn("ltx-cinematic-video-blueprint", names)
        self.assertIn("godogen-playable-loop", names)

    def test_builder_presentation_goal_loads_slide_skills(self):
        names = resolve_skill_names_for_goal("builder", "做一个融资路演PPT和产品发布 slides")
        self.assertIn("slides-story-arc", names)
        self.assertIn("diagram-driven-explainer", names)
        self.assertIn("pptx-export-bridge", names)

    def test_scribe_doc_goal_loads_doc_skills(self):
        names = resolve_skill_names_for_goal("scribe", "写一份 API documentation 和 README 手册")
        self.assertIn("docs-clarity-architecture", names)
        self.assertIn("diagram-driven-explainer", names)

    def test_image_goal_loads_image_direction_skills(self):
        names = resolve_skill_names_for_goal("imagegen", "生成一张品牌海报和封面图片")
        self.assertIn("image-prompt-director", names)
        self.assertIn("visual-storyboard-shotlist", names)
        self.assertIn("comfyui-pipeline-brief", names)

    def test_motion_goal_loads_animation_skills(self):
        names = resolve_skill_names_for_goal("builder", "做一个带 loading animation 和 Lottie 风格动效的官网")
        self.assertIn("motion-choreography-system", names)
        self.assertIn("lottie-readiness", names)

    def test_game_asset_goal_loads_pixel_pipeline_skills(self):
        names = resolve_skill_names_for_goal("spritesheet", "生成 pixel art 游戏素材和 spritesheet")
        self.assertIn("pixel-asset-pipeline", names)
        self.assertIn("asset-pipeline-packaging", names)

    def test_reviewer_game_goal_includes_escalation_skill(self):
        names = resolve_skill_names_for_goal("reviewer", "做一个可以玩的飞机大战游戏")
        self.assertIn("gameplay-qa-gate", names)
        self.assertIn("review-escalation-computer-use", names)
        self.assertIn("godogen-playable-loop", names)

    def test_video_goal_loads_video_skills(self):
        names = resolve_skill_names_for_goal("builder", "做一个产品宣传短片 video storyboard 和镜头脚本")
        self.assertIn("remotion-scene-composer", names)
        self.assertIn("ltx-cinematic-video-blueprint", names)

    def test_build_skill_context_renders_skill_blocks(self):
        context = build_skill_context("builder", "做一个带动画的品牌官网，需要插画 hero")
        self.assertIn("[Skill: motion-choreography-system]", context)
        self.assertIn("[Skill: svg-illustration-system]", context)
        self.assertIn("[Skill: image-prompt-director]", context)

    def test_skill_catalog_exposes_source_metadata_for_new_skills(self):
        catalog = {item["name"]: item for item in list_skill_catalog()}
        self.assertEqual(catalog["remotion-scene-composer"]["source_name"], "Remotion")
        self.assertIn("LTX", catalog["ltx-cinematic-video-blueprint"]["source_name"])
        self.assertEqual(catalog["godogen-playable-loop"]["category"], "game")

    def test_community_skill_can_be_keyword_triggered(self):
        with TemporaryDirectory() as tmp_dir:
            user_dir = Path(tmp_dir)
            skill_dir = user_dir / "community-video-toolkit"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text("COMMUNITY VIDEO TOOLKIT\n\n- Community video workflow.\n", encoding="utf-8")
            (skill_dir / "evermind_skill.json").write_text(
                '{"title":"Community Video Toolkit","summary":"Community video helper","node_types":["builder"],"keywords":["kinetic ad","video brief"],"tags":["video"]}',
                encoding="utf-8",
            )
            with patch.object(agent_skills, "USER_SKILLS_DIR", user_dir):
                agent_skills.list_skill_catalog.cache_clear()
                agent_skills._load_skill.cache_clear()
                names = resolve_skill_names_for_goal("builder", "做一个 kinetic ad video brief")
                self.assertIn("community-video-toolkit", names)
                catalog = {item["name"]: item for item in list_skill_catalog()}
                self.assertEqual(catalog["community-video-toolkit"]["origin"], "community")
                agent_skills.list_skill_catalog.cache_clear()
                agent_skills._load_skill.cache_clear()


if __name__ == "__main__":
    unittest.main()
