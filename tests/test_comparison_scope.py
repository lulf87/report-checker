from __future__ import annotations

import re
import unittest
from pathlib import Path

from mvp.capabilities import MODE_CATALOG, RULE_CATALOG


ROOT = Path(__file__).resolve().parents[1]
SCOPE_PATH = ROOT / "docs" / "COMPARISON_SCOPE.md"


class ComparisonScopeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scope = SCOPE_PATH.read_text(encoding="utf-8")

    def test_confirmed_scope_has_47_included_items_and_one_phase1_exclusion(self) -> None:
        for number in range(1, 48):
            with self.subTest(number=number):
                self.assertRegex(self.scope, rf"\bS{number:02d}\b")
        self.assertRegex(self.scope, r"S48\b.*excluded_phase1")
        self.assertIn("S01–S47", self.scope)
        self.assertIn("S48", self.scope)

    def test_scope_mentions_every_mode_and_current_rule(self) -> None:
        for mode_id in MODE_CATALOG:
            with self.subTest(mode=mode_id):
                self.assertIn(f"`{mode_id}`", self.scope)
        for rule_id in RULE_CATALOG:
            with self.subTest(rule=rule_id):
                self.assertIn(f"`{rule_id}`", self.scope)

    def test_current_mode_rule_plans_are_traceable_to_scope_document(self) -> None:
        # Every enabled rule is explicitly listed in the matrix or in the
        # current-rule traceability paragraph. This catches catalog additions
        # that would otherwise have no declared scope or implementation note.
        for mode_id, mode in MODE_CATALOG.items():
            for rule_id in mode["rule_ids"]:
                with self.subTest(mode=mode_id, rule=rule_id):
                    self.assertIn(f"`{rule_id}`", self.scope)

    def test_scope_preserves_explicit_phase1_table3_boundary(self) -> None:
        excluded_line = next(
            line for line in self.scope.splitlines() if line.startswith("| S48 ")
        )
        self.assertIn("excluded_phase1", excluded_line)
        self.assertIn("表 3", excluded_line)
        self.assertRegex(excluded_line, r"第一阶段.*暂不实现")


if __name__ == "__main__":
    unittest.main()
