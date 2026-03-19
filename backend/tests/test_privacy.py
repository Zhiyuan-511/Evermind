"""
Evermind Backend — Privacy Masker Tests
Covers mask/unmask roundtrip, pattern detection, and excluded node types.
"""

import unittest

from privacy import PrivacyMasker, BUILTIN_PATTERNS


class TestPrivacyMaskerBasics(unittest.TestCase):
    def test_disabled_masker_returns_input_unchanged(self):
        masker = PrivacyMasker(enabled=False)
        text = "Call me at 13812345678"
        masked, restore = masker.mask(text)
        self.assertEqual(masked, text)
        self.assertEqual(restore, {})

    def test_empty_text_returns_empty(self):
        masker = PrivacyMasker(enabled=True)
        masked, restore = masker.mask("")
        self.assertEqual(masked, "")
        self.assertEqual(restore, {})


class TestMaskUnmaskRoundtrip(unittest.TestCase):
    def test_phone_cn_roundtrip(self):
        masker = PrivacyMasker(enabled=True, patterns=["phone_cn"])
        original = "联系人手机: 13912345678, 备用: 15098765432"
        masked, restore = masker.mask(original)
        self.assertNotIn("13912345678", masked)
        self.assertNotIn("15098765432", masked)
        self.assertIn("***PHONE***", masked)
        self.assertEqual(len(restore), 2)
        unmasked = masker.unmask(masked, restore)
        self.assertEqual(unmasked, original)

    def test_email_roundtrip(self):
        masker = PrivacyMasker(enabled=True, patterns=["email"])
        original = "Send to admin@evermind.ai and support@test.com"
        masked, restore = masker.mask(original)
        self.assertNotIn("admin@evermind.ai", masked)
        self.assertNotIn("support@test.com", masked)
        self.assertEqual(len(restore), 2)
        unmasked = masker.unmask(masked, restore)
        self.assertEqual(unmasked, original)

    def test_api_key_roundtrip(self):
        masker = PrivacyMasker(enabled=True, patterns=["api_key"])
        original = "export OPENAI_API_KEY=sk-abc123def456ghi789jkl012mno345"
        masked, restore = masker.mask(original)
        self.assertNotIn("sk-abc123def456ghi789jkl012mno345", masked)
        self.assertIn("***API_KEY***", masked)
        unmasked = masker.unmask(masked, restore)
        self.assertEqual(unmasked, original)

    def test_mixed_pii_roundtrip(self):
        masker = PrivacyMasker(enabled=True, patterns=["phone_cn", "email"])
        original = "User: user@test.com, Phone: 13812345678"
        masked, restore = masker.mask(original)
        self.assertNotIn("user@test.com", masked)
        self.assertNotIn("13812345678", masked)
        unmasked = masker.unmask(masked, restore)
        self.assertEqual(unmasked, original)


class TestExcludedNodeTypes(unittest.TestCase):
    def test_excluded_node_type_skips_masking(self):
        masker = PrivacyMasker(enabled=True, patterns=["phone_cn"])
        text = "Phone: 13812345678"
        masked, restore = masker.mask(text, node_type="localshell")
        self.assertEqual(masked, text)
        self.assertEqual(restore, {})

    def test_non_excluded_node_type_masks(self):
        masker = PrivacyMasker(enabled=True, patterns=["phone_cn"])
        text = "Phone: 13812345678"
        masked, restore = masker.mask(text, node_type="builder")
        self.assertNotIn("13812345678", masked)
        self.assertEqual(len(restore), 1)


class TestPatternsInfo(unittest.TestCase):
    def test_get_patterns_info_returns_metadata(self):
        masker = PrivacyMasker(enabled=True, patterns=["phone_cn", "email"])
        info = masker.get_patterns_info()
        self.assertEqual(len(info), 2)
        names = {p["name"] for p in info}
        self.assertIn("phone_cn", names)
        self.assertIn("email", names)

    def test_test_mask_preview(self):
        masker = PrivacyMasker(enabled=True, patterns=["email"])
        result = masker.test_mask("contact: hello@world.com")
        self.assertEqual(result["pii_found"], 1)
        self.assertTrue(result["can_unmask"])


class TestCustomPatterns(unittest.TestCase):
    def test_custom_pattern_works(self):
        masker = PrivacyMasker(
            enabled=True,
            patterns=[],
            custom_patterns=[{
                "name": "order_id",
                "regex": r"ORD-\d{8}",
                "label": "订单号",
                "mask": "***ORDER***",
            }],
        )
        text = "Order: ORD-12345678"
        masked, restore = masker.mask(text)
        self.assertNotIn("ORD-12345678", masked)
        self.assertIn("***ORDER***", masked)
        unmasked = masker.unmask(masked, restore)
        self.assertEqual(unmasked, text)


if __name__ == "__main__":
    unittest.main()
