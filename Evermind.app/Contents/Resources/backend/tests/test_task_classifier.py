import unittest

import task_classifier


class TestTaskClassifierMultiPage(unittest.TestCase):
    def test_requested_page_count_handles_digits_and_chinese_numbers(self):
        self.assertEqual(task_classifier.requested_page_count("做一个 3 页面官网"), 3)
        self.assertEqual(task_classifier.requested_page_count("做一个三页面官网"), 3)
        self.assertEqual(task_classifier.requested_page_count("做一个十二页站点"), 12)

    def test_wants_multi_page_detects_route_and_sitemap_hints(self):
        self.assertTrue(task_classifier.wants_multi_page("Build a multi-route marketing site with site map"))
        self.assertTrue(task_classifier.wants_multi_page("做一个独立页面的企业官网"))
        self.assertFalse(task_classifier.wants_multi_page("做一个单页产品官网"))

    def test_builder_task_description_requires_real_multi_page_delivery(self):
        desc = task_classifier.builder_task_description("做一个三页面官网，包含首页、定价页和联系页")
        self.assertIn("multi-page", desc)
        self.assertIn("index.html plus at least 2 additional linked HTML page", desc)
        self.assertIn("do NOT fake it as one long landing page", desc)

    def test_builder_task_description_for_game_requires_visible_first_save(self):
        desc = task_classifier.builder_task_description("做一个 3D 像素射击游戏")
        self.assertIn("visible start screen", desc)
        self.assertIn("gameplay viewport", desc)
        self.assertIn("do not spend the whole first pass on CSS alone", desc)

    def test_builder_system_prompt_adds_multi_page_contract(self):
        prompt = task_classifier.builder_system_prompt("做一个多页面官网，带站点地图")
        self.assertIn("MULTI-PAGE CONTRACT", prompt)
        self.assertIn("do NOT compress this into one scrolling page", prompt)
        self.assertIn("navigation links/buttons must actually open", prompt.lower())

    def test_builder_system_prompt_for_game_skips_large_css_dump(self):
        prompt = task_classifier.builder_system_prompt("做一个我的世界风格的 3D 像素射击游戏")
        self.assertNotIn("=== PRE-BUILT CSS DESIGN SYSTEM (MUST USE) ===", prompt)
        self.assertIn("playable shell", prompt)
        self.assertIn("Keep game UI CSS lightweight", prompt)

    def test_delivery_contract_counts_additional_pages_not_total_pages(self):
        contract = task_classifier.delivery_contract("做一个介绍奢侈品的 8 页面官网")
        self.assertIn("index.html plus at least 7 additional linked HTML page", contract)
        self.assertNotIn("plus at least 8 additional", contract)

    def test_classify_animation_website_as_website_not_creative(self):
        profile = task_classifier.classify("做一个带高级动画和 WebGL 效果的品牌官网")
        self.assertEqual(profile.task_type, "website")

    def test_classify_tourism_website_with_fun_language_as_website(self):
        goal = "创建一个介绍美国旅游景点的一个网站，详细介绍加州所有比较好玩的景点"
        profile = task_classifier.classify(goal)
        self.assertEqual(profile.task_type, "website")

    def test_suggested_multi_page_route_filenames_uses_travel_routes_for_tourism_site(self):
        routes = task_classifier.suggested_multi_page_route_filenames(
            "创建一个介绍美国旅游景点的 8 页网站，详细介绍加州所有比较好玩的景点和旅行攻略"
        )
        self.assertGreaterEqual(len(routes), 6)
        self.assertEqual(routes[:4], ["attractions.html", "cities.html", "nature.html", "coast.html"])
        self.assertNotIn("pricing.html", routes[:6])

    def test_motion_contract_detects_premium_motion_brief(self):
        goal = "做一个像苹果一样高级的 8 页奢侈品官网，要有高级动画和页面过渡"
        self.assertTrue(task_classifier.wants_motion_rich_experience(goal))
        contract = task_classifier.motion_contract(goal)
        self.assertIn("MOTION CONTRACT", contract)
        self.assertIn("page-to-page transition treatment", contract)

    def test_requested_output_language_and_contract(self):
        goal = "做一个介绍奢侈品的 8 页网站，要用英文"
        self.assertEqual(task_classifier.requested_output_language(goal), "en")
        contract = task_classifier.language_contract(goal)
        self.assertIn("must be in English", contract)

    def test_design_requirements_include_motion_contract_when_requested(self):
        req = task_classifier.design_requirements("做一个苹果风高级品牌官网，带动画和过渡")
        self.assertIn("MOTION CONTRACT", req)
        self.assertIn("hero or focal object", req)

    def test_explicit_sprite_pipeline_goal_triggers_generated_assets(self):
        goal = "做一个像素风平台跳跃游戏，包含角色素材和 spritesheet"
        self.assertTrue(task_classifier.wants_generated_assets(goal))
        self.assertEqual(task_classifier.game_asset_pipeline_mode(goal), "2d")

    def test_voxel_3d_game_with_modeling_request_triggers_3d_asset_pipeline(self):
        goal = "创建一个我的世界风格的像素设计游戏（3d),地图丰富，要有怪物，机制等等，这款游戏要达到商业级水准，建模之类的都要有"
        self.assertTrue(task_classifier.wants_generated_assets(goal))
        self.assertEqual(task_classifier.game_asset_pipeline_mode(goal), "3d")

    def test_generic_3d_game_without_asset_request_does_not_trigger_asset_pipeline(self):
        goal = "做一个 3D 枪战网页游戏，要有完整玩法循环和高级 UI"
        self.assertFalse(task_classifier.wants_generated_assets(goal))
        self.assertEqual(task_classifier.game_asset_pipeline_mode(goal), "")


if __name__ == "__main__":
    unittest.main()
