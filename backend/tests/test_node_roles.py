import unittest

from node_roles import normalize_node_role


class TestNodeRoleNormalization(unittest.TestCase):
    def test_unknown_role_does_not_raise_and_round_trips(self):
        self.assertEqual(normalize_node_role("foo_bar"), "foo_bar")

    def test_alpha_prefix_falls_back_to_canonical_role(self):
        self.assertEqual(normalize_node_role("uidesign_secondary"), "uidesign")


if __name__ == "__main__":
    unittest.main()
