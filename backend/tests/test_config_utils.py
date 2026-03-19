import unittest

from config_utils import coerce_bool, coerce_int


class TestConfigUtils(unittest.TestCase):
    def test_coerce_bool_handles_common_string_values(self):
        self.assertTrue(coerce_bool("true"))
        self.assertTrue(coerce_bool("YES"))
        self.assertFalse(coerce_bool("false", default=True))
        self.assertFalse(coerce_bool("0", default=True))

    def test_coerce_bool_falls_back_to_default_for_unknown_strings(self):
        self.assertTrue(coerce_bool("not-a-bool", default=True))
        self.assertFalse(coerce_bool("not-a-bool", default=False))

    def test_coerce_int_applies_default_and_clamp(self):
        self.assertEqual(coerce_int("5", 3, minimum=1, maximum=8), 5)
        self.assertEqual(coerce_int("99", 3, minimum=1, maximum=8), 8)
        self.assertEqual(coerce_int("-2", 3, minimum=1, maximum=8), 1)
        self.assertEqual(coerce_int("oops", 3, minimum=1, maximum=8), 3)


if __name__ == "__main__":
    unittest.main()
