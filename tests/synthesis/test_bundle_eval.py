"""Tests for BundleEval — the gate after bundle assembly."""
from __future__ import annotations

import unittest

from spectrum_systems_core.synthesis.bundle_eval import BundleEval

from ._fixtures import make_bundle, make_bundle_item


class BundleEvalTests(unittest.TestCase):
    def test_candidate_item_blocked_by_eval(self) -> None:
        items = [
            make_bundle_item(promoted_status="candidate"),
            make_bundle_item(),
            make_bundle_item(),
        ]
        bundle = make_bundle(items=items)
        result = BundleEval().run(bundle)
        self.assertEqual(result["decision"], "block")
        self.assertTrue(
            any(rc.startswith("EVAL-CTX-002") for rc in result["reason_codes"])
        )

    def test_token_budget_exceeded_blocked(self) -> None:
        items = [make_bundle_item(token_estimate=100) for _ in range(3)]
        bundle = make_bundle(items=items)
        bundle["total_token_estimate"] = 999_999
        result = BundleEval().run(bundle)
        self.assertEqual(result["decision"], "block")
        self.assertTrue(
            any(rc.startswith("EVAL-CTX-003") for rc in result["reason_codes"])
        )

    def test_minimum_items_enforced(self) -> None:
        items = [make_bundle_item()]
        bundle = make_bundle(items=items)
        result = BundleEval().run(bundle)
        self.assertEqual(result["decision"], "block")
        self.assertTrue(
            any(rc.startswith("EVAL-CTX-004") for rc in result["reason_codes"])
        )

    def test_invalid_audience_blocked(self) -> None:
        items = [make_bundle_item() for _ in range(3)]
        bundle = make_bundle(items=items, audience="technical")
        bundle["audience"] = "investor"
        result = BundleEval().run(bundle)
        self.assertEqual(result["decision"], "block")
        # EVAL-CTX-001 (schema enum) and EVAL-CTX-005 will both fire.
        self.assertTrue(
            any("EVAL-CTX-005" in rc for rc in result["reason_codes"])
            or any("EVAL-CTX-001" in rc for rc in result["reason_codes"])
        )

    def test_clean_bundle_allowed(self) -> None:
        items = [make_bundle_item() for _ in range(3)]
        bundle = make_bundle(items=items)
        result = BundleEval().run(bundle)
        self.assertEqual(result["decision"], "allow", result["reason_codes"])


if __name__ == "__main__":
    unittest.main()
