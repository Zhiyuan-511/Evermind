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
        self.assertIn("./_evermind_runtime/three/three.min.js", prompt)
        self.assertNotIn("cdn.jsdelivr.net/npm/three", prompt)
        self.assertIn("before the first HUD update, render() call, or requestAnimationFrame loop", prompt)
        self.assertIn("null-guard optional runtime state", prompt)
        self.assertIn("Primitive Box/Sphere/Cylinder/Cone/Capsule meshes are acceptable for greybox props", prompt)
        self.assertNotIn("Build all game objects with THREE.Mesh + THREE.BoxGeometry/SphereGeometry/CylinderGeometry.", prompt)

    def test_builder_system_prompt_for_premium_3d_game_bans_primitive_only_hero_assets(self):
        prompt = task_classifier.builder_system_prompt(
            "创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。"
        )
        self.assertIn("Premium 3D hero asset rule", prompt)
        self.assertIn("THREE.Shape/ExtrudeGeometry", prompt)
        self.assertIn("player/enemy/weapon construction paths themselves", prompt)
        self.assertIn("Primitive-only Box/Cone/Cylinder/Sphere/Torus/Capsule", prompt)
        self.assertNotIn("Build all game objects with THREE.Mesh + THREE.BoxGeometry/SphereGeometry/CylinderGeometry.", prompt)

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

    def test_voxel_3d_commercial_game_with_modeling_language_triggers_3d_asset_pipeline(self):
        goal = "创建一个我的世界风格的像素设计游戏（3d),地图丰富，要有怪物，机制等等，这款游戏要达到商业级水准，建模之类的都要有"
        self.assertTrue(task_classifier.wants_generated_assets(goal))
        self.assertEqual(task_classifier.game_asset_pipeline_mode(goal), "3d")

    def test_generic_3d_game_without_asset_request_does_not_trigger_asset_pipeline(self):
        goal = "做一个 3D 枪战网页游戏，要有完整玩法循环和高级 UI"
        self.assertFalse(task_classifier.wants_generated_assets(goal))
        self.assertEqual(task_classifier.game_asset_pipeline_mode(goal), "")

    def test_commercial_3d_shooter_with_many_asset_domains_triggers_3d_asset_pipeline(self):
        goal = "创建一个我的世界一样的3d像素版射击游戏，要有怪物等等，不同的枪械武器，有关口，和通关胜利页面等等，是一个可以商业用途的3d小游戏"
        self.assertTrue(task_classifier.wants_generated_assets(goal))
        self.assertEqual(task_classifier.game_asset_pipeline_mode(goal), "3d")

    def test_explicit_3d_asset_pack_request_triggers_3d_asset_pipeline(self):
        goal = "做一个3d射击游戏，并生成角色模型、武器模型、怪物模型、贴图和asset pack"
        self.assertTrue(task_classifier.wants_generated_assets(goal))
        self.assertEqual(task_classifier.game_asset_pipeline_mode(goal), "3d")

    def test_explicit_3d_concept_asset_pack_request_triggers_3d_asset_pipeline(self):
        goal = "创建一个第三人称3D射击游戏，必须先生成角色、怪物、步枪和场景的3D概念资产包，再生成可玩的HTML成品。"
        self.assertTrue(task_classifier.wants_generated_assets(goal))
        self.assertEqual(task_classifier.game_asset_pipeline_mode(goal), "3d")

    def test_placeholder_first_pass_game_request_disables_asset_pipeline(self):
        goal = "做一个第三人称3d射击游戏原型，先用占位几何体和程序化材质，不要生成素材包，只要单页 index.html。"
        self.assertFalse(task_classifier.wants_generated_assets(goal))
        self.assertEqual(task_classifier.game_asset_pipeline_mode(goal), "")

    def test_game_runtime_mode_prefers_engine_free_for_simple_arcade_briefs(self):
        self.assertEqual(task_classifier.game_runtime_mode("做一个贪吃蛇小游戏"), "none")

    def test_game_runtime_mode_uses_3d_engine_for_webgl_briefs(self):
        self.assertEqual(task_classifier.game_runtime_mode("做一个 3D WebGL 赛车游戏"), "3d_engine")

    def test_game_runtime_mode_uses_2d_engine_for_platformer_briefs(self):
        self.assertEqual(task_classifier.game_runtime_mode("做一个横版平台跳跃游戏，带 tilemap 和 boss"), "2d_engine")

    def test_game_explicit_single_file_delivery_detects_explicit_single_page_brief(self):
        self.assertTrue(
            task_classifier.game_explicit_single_file_delivery(
                "创建一个我的世界风格的 3D 像素射击游戏，单页 index.html 即可。"
            )
        )

    def test_game_explicit_single_file_delivery_rejects_implicit_game_brief(self):
        self.assertFalse(
            task_classifier.game_explicit_single_file_delivery(
                "创建一个我的世界风格的 3D 像素射击游戏，包含开始界面、暂停和结算体验。"
            )
        )

    def test_game_direct_text_delivery_mode_allows_single_page_lightweight_browser_games(self):
        self.assertTrue(task_classifier.game_direct_text_delivery_mode("做一个贪吃蛇网页游戏，包含开始界面和结算。"))
        self.assertTrue(task_classifier.game_direct_text_delivery_mode("做一个 3D 第三人称迷宫冒险游戏，带开始界面、通关和结算。"))
        self.assertTrue(task_classifier.game_direct_text_delivery_mode("做一个横版平台跳跃游戏，带 tilemap 和 boss。"))

    def test_game_direct_text_delivery_mode_rejects_premium_3d_asset_heavy_games(self):
        self.assertFalse(
            task_classifier.game_direct_text_delivery_mode(
                "创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。"
            )
        )

    def test_premium_3d_builder_patch_preferred_for_large_single_page_game(self):
        goal = "创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。"
        self.assertTrue(task_classifier.premium_3d_builder_patch_preferred(goal))
        self.assertTrue(task_classifier.premium_3d_builder_direct_text_first_pass(goal))

    def test_builder_task_description_for_premium_3d_game_adds_model_contract(self):
        desc = task_classifier.builder_task_description(
            "创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。"
        )
        self.assertIn("PREMIUM 3D MODEL CONTRACT", desc)
        self.assertIn("THREE.Shape/ExtrudeGeometry", desc)
        self.assertIn("custom BufferGeometry", desc)
        self.assertIn("player/enemy/weapon construction paths themselves", desc)
        self.assertIn("Primitive-only Box/Cone/Cylinder/Sphere/Torus/Capsule", desc)
        self.assertIn("sphere/box/cylinder-dominated silhouette", desc)

    def test_builder_task_description_for_tps_game_adds_combat_fairness_contract(self):
        desc = task_classifier.builder_task_description(
            "创建一个第三人称 3D 射击游戏，带怪物、不同枪械和鼠标拖拽视角。"
        )
        self.assertIn("COMBAT FAIRNESS CONTRACT", desc)
        self.assertIn("fair opening window", desc)
        self.assertIn("true radial distance checks", desc)

    def test_game_direct_text_delivery_mode_rejects_existing_repo_patch_requests(self):
        self.assertFalse(
            task_classifier.game_direct_text_delivery_mode(
                "继续修复当前 3D 射击游戏仓库里的 index.html 和 src/game.js，别重做。"
            )
        )

    def test_builder_task_description_for_game_stays_game_focused(self):
        desc = task_classifier.builder_task_description("做一个 3D 第三人称射击游戏，带怪物、武器和大地图。")
        self.assertIn("FIRST PLAYABLE SLICE CONTRACT", desc)
        self.assertIn("DELIVERY CONTRACT: output one complete playable file", desc)
        self.assertNotIn("IMAGE SIZING:", desc)
        self.assertNotIn("PAGE VISUAL CONTRACT:", desc)

    def test_builder_task_description_for_stage_clear_game_adds_progression_contract(self):
        desc = task_classifier.builder_task_description(
            "做一个第三人称 3D 射击游戏，要有关卡、通过页面和胜利结算。"
        )
        self.assertIn("PROGRESSION CONTRACT", desc)
        self.assertIn("victory / mission-complete / pass screen", desc)
        self.assertIn("currentStage/stage/wave/currentWave/maxStages", desc)

    def test_builder_system_prompt_for_drag_camera_goal_adds_camera_contract(self):
        prompt = task_classifier.builder_system_prompt(
            "做一个第三人称 3D 射击游戏，鼠标长按屏幕后可以拉动转动视角。"
        )
        self.assertIn("drag-to-rotate camera control", prompt)
        self.assertIn("mouse/pointer drag or pointer-lock look", prompt)
        self.assertIn("dragging right must yaw the camera right", prompt.lower())
        self.assertIn("camera-facing forward vector", prompt)

    def test_builder_system_prompt_for_tps_game_adds_spawn_safety_contract(self):
        prompt = task_classifier.builder_system_prompt(
            "做一个第三人称 3D 射击游戏，带怪物、枪械和战斗。"
        )
        self.assertIn("spawn-kill openings", prompt)
        self.assertIn("true radial distance", prompt)

    def test_builder_system_prompt_for_tps_game_adds_foundation_contract_and_js_guard(self):
        prompt = task_classifier.builder_system_prompt(
            "做一个第三人称 3D 射击游戏，鼠标长按屏幕后可以拉动转动视角。"
        )
        self.assertIn("GAMEPLAY FOUNDATION CONTRACT", prompt)
        self.assertIn("position.y: 1.2", prompt)
        self.assertIn(
            "forward = new THREE.Vector3(Math.sin(yaw), 0, Math.cos(yaw)).normalize()",
            prompt,
        )
        self.assertIn("right = new THREE.Vector3(forward.z, 0, -forward.x).normalize()", prompt)
        self.assertIn("yaw += deltaX * sensitivity;", prompt)
        self.assertIn("pitch -= deltaY * sensitivity;", prompt)

    def test_builder_system_prompt_for_game_adds_runtime_perf_contract(self):
        prompt = task_classifier.builder_system_prompt(
            "做一个第三人称 3D 射击游戏，要稳定流畅，不要卡顿。"
        )
        self.assertIn("renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5))", prompt)
        self.assertIn("Math.min(clock.getDelta(), 0.05)", prompt)
        self.assertIn("document.hidden", prompt)
        self.assertIn("Pool or recycle bullets", prompt)

    def test_analyst_description_for_game_requires_control_frame_and_asset_sourcing_plan(self):
        desc = task_classifier.analyst_description(
            "创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图、鼠标拖拽视角和精美建模。"
        )
        self.assertIn("source_fetch first", desc)
        self.assertIn("Crawl4AI-backed", desc)
        self.assertIn("anti-mirror acceptance", desc)
        self.assertIn("startup survivability checks", desc)
        self.assertIn("<control_frame_contract>", desc)
        self.assertIn("<asset_sourcing_plan>", desc)
        self.assertIn("exact source URLs", desc)

    def test_analyst_description_for_game_adds_runtime_stability_checks(self):
        desc = task_classifier.analyst_description(
            "创建一个第三人称 3D 射击游戏，要流畅稳定，不要卡住。"
        )
        self.assertIn("cap renderer pixel ratio", desc)
        self.assertIn("visibilitychange", desc)
        self.assertIn("bounded projectile/FX pools", desc)

    def test_builder_task_description_for_tps_game_adds_foundation_summary(self):
        desc = task_classifier.builder_task_description(
            "做一个第三人称 3D 射击游戏，鼠标拖动视角，不要镜像控制。"
        )
        self.assertIn("FOUNDATION CONTRACT", desc)
        self.assertIn("position.y: 1.2", desc)

    def test_builder_task_description_for_game_adds_runtime_performance_contract(self):
        desc = task_classifier.builder_task_description(
            "做一个第三人称 3D 射击游戏，要流畅稳定，不要卡顿。"
        )
        self.assertIn("RUNTIME PERFORMANCE CONTRACT", desc)
        self.assertIn("visibilitychange", desc)
        self.assertIn("pool bullets/projectiles/impact FX", desc)
        self.assertIn(
            "forward = new THREE.Vector3(Math.sin(yaw), 0, Math.cos(yaw)).normalize()",
            desc,
        )
        self.assertIn("yaw += deltaX * sensitivity", desc)
        self.assertIn("pitch -= deltaY * sensitivity", desc)

    def test_builder_task_description_for_shooter_adds_crosshair_and_tracer_contract(self):
        desc = task_classifier.builder_task_description(
            "做一个第三人称 3D 射击游戏，要有准心、清晰弹道、不同枪械和怪物。"
        )
        self.assertIn("AIM/HUD CONTRACT", desc)
        self.assertIn("visible centered crosshair/reticle", desc)
        self.assertIn("readable tracer/projectile core", desc)


if __name__ == "__main__":
    unittest.main()
