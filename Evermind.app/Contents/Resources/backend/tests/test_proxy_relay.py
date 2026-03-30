"""
Evermind Backend — Proxy/Relay Tests
Covers circuit-breaker, _safe_cost, retry scoping, and RelayManager CRUD.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from proxy_relay import RelayEndpoint, RelayManager, _safe_cost


class TestSafeCost(unittest.TestCase):
    def test_returns_float_on_success(self):
        mock_resp = MagicMock()
        with patch.dict("sys.modules", {"litellm": MagicMock(completion_cost=MagicMock(return_value=0.0042))}):
            result = _safe_cost(mock_resp, "openai/gpt-4o")
            self.assertAlmostEqual(result, 0.0042)

    def test_returns_zero_on_exception(self):
        mock_resp = MagicMock()
        result = _safe_cost(mock_resp, "bad/model")
        self.assertEqual(result, 0.0)


class TestRelayEndpointCircuitBreaker(unittest.TestCase):
    def _make_endpoint(self) -> RelayEndpoint:
        return RelayEndpoint(
            id="test-1", name="Test", base_url="http://example.com",
            api_key="key", models=["gpt-4o"],
        )

    def test_circuit_closed_initially(self):
        ep = self._make_endpoint()
        self.assertFalse(ep.circuit_open)

    def test_circuit_opens_after_threshold_failures(self):
        ep = self._make_endpoint()
        for _ in range(RelayEndpoint.CIRCUIT_FAILURE_THRESHOLD):
            ep._record_failure()
        self.assertTrue(ep.circuit_open)

    def test_circuit_closes_on_success(self):
        ep = self._make_endpoint()
        for _ in range(RelayEndpoint.CIRCUIT_FAILURE_THRESHOLD):
            ep._record_failure()
        self.assertTrue(ep.circuit_open)
        ep._record_success()
        self.assertFalse(ep.circuit_open)

    def test_circuit_closes_after_recovery_period(self):
        ep = self._make_endpoint()
        for _ in range(RelayEndpoint.CIRCUIT_FAILURE_THRESHOLD):
            ep._record_failure()
        # Simulate time passing beyond recovery
        ep._circuit_open_until = time.time() - 1
        self.assertFalse(ep.circuit_open)


class TestRelayManagerCRUD(unittest.TestCase):
    def test_add_and_list(self):
        mgr = RelayManager()
        ep = mgr.add(name="R1", base_url="http://example.com/v1", api_key="k")
        listed = mgr.list()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "R1")
        # API key should be masked in list output
        self.assertNotEqual(listed[0].get("api_key"), "k")

    def test_remove(self):
        mgr = RelayManager()
        ep = mgr.add(name="R1", base_url="http://example.com/v1")
        self.assertTrue(mgr.remove(ep.id))
        self.assertEqual(len(mgr.list()), 0)

    def test_remove_nonexistent(self):
        mgr = RelayManager()
        self.assertFalse(mgr.remove("does-not-exist"))

    def test_load_exports_roundtrip(self):
        mgr = RelayManager()
        mgr.add(name="A", base_url="http://a.com", api_key="key-a", models=["m1"])
        mgr.add(name="B", base_url="http://b.com", api_key="key-b", models=["m2"])
        exported = mgr.export()

        mgr2 = RelayManager()
        mgr2.load(exported)
        self.assertEqual(len(mgr2.list()), 2)
        # Verify full-fidelity keys survive roundtrip
        configs = mgr2.export()
        keys = {c["name"]: c["api_key"] for c in configs}
        self.assertEqual(keys["A"], "key-a")
        self.assertEqual(keys["B"], "key-b")

    def test_get_all_models_skips_disabled(self):
        mgr = RelayManager()
        ep = mgr.add(name="R1", base_url="http://a.com", models=["m1"])
        ep.enabled = False
        self.assertEqual(len(mgr.get_all_models()), 0)
        ep.enabled = True
        self.assertGreater(len(mgr.get_all_models()), 0)

    def test_empty_models_default(self):
        mgr = RelayManager()
        ep = mgr.add(name="R1", base_url="http://a.com")
        # When no models specified, should have defaults
        self.assertGreater(len(ep.models), 0)


if __name__ == "__main__":
    unittest.main()
