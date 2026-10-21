from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from mvp.report_front_rules import (
    analyze_basis_and_conclusion,
    analyze_inspection_items,
    expected_cover_version,
    parse_cn_date,
    scope_has_exclusion_or_partial,
)


ROOT = Path(__file__).resolve().parents[1]


def reasons(issues):
    return [issue["reason_code"] for issue in issues]


class InspectionItemsWordingTests(unittest.TestCase):
    def test_comma_inside_exclusion_is_reported(self) -> None:
        issues = analyze_inspection_items("2.1～2.7（除生物相容性，电磁兼容性）")
        self.assertEqual(reasons(issues), ["inspection_items_separator"])

    def test_hyphen_range_is_reported_but_standard_numbers_are_not(self) -> None:
        issues = analyze_inspection_items("2.1-2.7（除生物相容性、电磁兼容性）")
        self.assertEqual(reasons(issues), ["inspection_items_range_symbol"])
        self.assertEqual(issues[0]["expected"], "2.1～2.7")
        clean = analyze_inspection_items(
            "2.6（仅检GB 9706.1-2020 中8.7、GB 9706.202-2021 中201.8.7.1、201.8.7.3）"
        )
        self.assertEqual(clean, [])

    def test_three_consecutive_clauses_must_be_a_range(self) -> None:
        issues = analyze_inspection_items("2.4、2.5、2.6（除生物相容性、电磁兼容性）")
        self.assertEqual(reasons(issues), ["consecutive_clauses_not_ranged"])
        self.assertEqual(issues[0]["expected"], "2.4～2.6")

    def test_two_consecutive_clauses_must_be_listed(self) -> None:
        issues = analyze_inspection_items("2.4～2.5")
        self.assertEqual(reasons(issues), ["two_clause_range"])
        self.assertEqual(issues[0]["expected"], "2.4、2.5")
        self.assertEqual(analyze_inspection_items("2.4、2.5"), [])
        self.assertEqual(analyze_inspection_items("2.1.2～2.1.9、2.1.11、2.1.12、2.1.15、2.7、2.10"), [])

    def test_lettered_items_follow_the_same_rule(self) -> None:
        self.assertEqual(analyze_inspection_items("2.3.13.1a)～c)、f)、g)、2.3.13.12a)"), [])
        self.assertEqual(reasons(analyze_inspection_items("2.3.13.1a)、b)、c)")), ["consecutive_clauses_not_ranged"])
        self.assertEqual(reasons(analyze_inspection_items("2.3.6.6a)～b)")), ["two_clause_range"])

    def test_qualified_items_do_not_join_a_consecutive_run(self) -> None:
        self.assertEqual(analyze_inspection_items("2.1（仅检外观）、2.2、2.3"), [])
        self.assertEqual(analyze_inspection_items("2.3.1.3（除器械的头端长度）、2.3.1.4、2.3.1.5"), [])

    def test_exclusion_order_is_biocompatibility_usability_emc(self) -> None:
        self.assertEqual(analyze_inspection_items("2.1～2.4（除生物相容性、可用性、电磁兼容性）"), [])
        self.assertEqual(analyze_inspection_items("2.1～2.4（除可用性、电磁兼容性）"), [])
        issues = analyze_inspection_items("2.1～2.4（除电磁兼容性、生物相容性）")
        self.assertEqual(reasons(issues), ["exclusion_order"])
        self.assertEqual(issues[0]["expected"], "生物相容性、电磁兼容性")

    def test_full_scope_phrases_are_forbidden(self) -> None:
        self.assertIn("inspection_items_forbidden_phrase", reasons(analyze_inspection_items("全性能")))
        self.assertIn("inspection_items_forbidden_phrase", reasons(analyze_inspection_items("全项目")))
        self.assertIn("inspection_items_forbidden_phrase", reasons(analyze_inspection_items("2.性能")))

    def test_standard_year_requires_hyphen(self) -> None:
        issues = analyze_inspection_items("2.1（除GB 16174.2 2024 中 21.2）")
        self.assertEqual(reasons(issues), ["standard_number_year_hyphen_missing"])
        self.assertEqual(issues[0]["expected"], "GB 16174.2-2024")

    def test_partial_scope_detection(self) -> None:
        self.assertTrue(scope_has_exclusion_or_partial("2.1～2.7（除生物相容性、电磁兼容性）"))
        self.assertTrue(scope_has_exclusion_or_partial("2.3（仅检PVC 反应）"))
        self.assertFalse(scope_has_exclusion_or_partial("2.1～2.7"))


class BasisConclusionWordingTests(unittest.TestCase):
    CLIENT = "甲医疗器械（上海）有限公司"
    PRODUCER = "乙医疗器械有限公司"
    ITEMS = "2.1～2.7（除生物相容性、电磁兼容性）"

    def analyze(self, basis: str, conclusion: str, *, producer: str | None = None, items: str | None = None):
        return analyze_basis_and_conclusion(
            basis,
            conclusion,
            client=self.CLIENT,
            producer=self.PRODUCER if producer is None else producer,
            inspection_items=self.ITEMS if items is None else items,
        )

    def test_canonical_wording_with_provide(self) -> None:
        issues = self.analyze(
            f"{self.CLIENT}提供的《某设备》产品技术要求及变更对比表",
            f"被检样品所检项目符合{self.CLIENT}提供的《某设备》产品技术要求及变更对比表",
        )
        self.assertEqual(issues, [])

    def test_provide_depends_on_client_and_producer(self) -> None:
        missing = self.analyze(
            f"{self.CLIENT}的《某设备》产品技术要求",
            f"被检样品所检项目符合{self.CLIENT}的《某设备》产品技术要求",
        )
        self.assertEqual(reasons(missing), ["provide_wording", "provide_wording"])
        extra = self.analyze(
            f"{self.CLIENT}提供的《某设备》产品技术要求",
            f"被检样品所检项目符合{self.CLIENT}提供的《某设备》产品技术要求",
            producer=self.CLIENT,
        )
        self.assertEqual(reasons(extra), ["provide_wording", "provide_wording"])

    def test_short_names_and_change_document_wording(self) -> None:
        issues = self.analyze(
            f"{self.CLIENT}提供的《某设备》技术要求",
            f"被检样品所检项目符合{self.CLIENT}提供的《某设备》技术要求",
        )
        self.assertEqual(reasons(issues), ["product_technical_requirement_name", "product_technical_requirement_name"])
        issues = self.analyze(
            f"{self.CLIENT}提供的《某设备》产品技术要求和技术要求变更对比表",
            f"被检样品所检项目符合{self.CLIENT}提供的《某设备》产品技术要求及变更对比表",
        )
        self.assertEqual(reasons(issues), ["change_document_name"])
        issues = self.analyze(
            f"{self.CLIENT}提供的《某设备》产品技术要求及变更单",
            f"被检样品所检项目符合{self.CLIENT}提供的《某设备》产品技术要求及变更单",
        )
        self.assertEqual(reasons(issues), ["change_document_name", "change_document_name"])

    def test_scope_wording_in_conclusion(self) -> None:
        issues = self.analyze(
            f"{self.CLIENT}提供的《某设备》产品技术要求",
            f"被检样品符合{self.CLIENT}提供的《某设备》产品技术要求",
        )
        self.assertEqual(reasons(issues), ["conclusion_scope_wording"])
        issues = self.analyze(
            f"{self.CLIENT}提供的《某设备》产品技术要求",
            f"被检样品符合{self.CLIENT}提供的《某设备》产品技术要求",
            items="2.3（仅检PVC 反应）",
        )
        self.assertEqual(reasons(issues), ["conclusion_scope_wording"])

    def test_bracket_width_and_company_consistency(self) -> None:
        issues = self.analyze(
            "甲医疗器械(上海)有限公司提供的《某设备》产品技术要求",
            "被检样品所检项目符合甲医疗器械(上海)有限公司提供的《某设备》产品技术要求",
        )
        self.assertEqual(reasons(issues), ["name_bracket_width_inconsistent", "name_bracket_width_inconsistent"])

    def test_basis_and_conclusion_must_cite_the_same_document(self) -> None:
        issues = self.analyze(
            f"{self.CLIENT}提供的《某设备》产品技术要求",
            f"被检样品所检项目符合{self.CLIENT}提供的《另一设备》产品技术要求",
        )
        self.assertEqual(reasons(issues), ["basis_conclusion_inconsistent"])

    def test_standard_citations_need_the_word_standard(self) -> None:
        issues = self.analyze(
            "GB 9706.1-2020《医用电气设备 第1部分：基本安全和基本性能的通用要求》、"
            f"{self.CLIENT}提供的《某设备》产品技术要求",
            f"被检样品所检项目符合{self.CLIENT}提供的《某设备》产品技术要求和GB 9706.1-2020的要求",
        )
        self.assertIn("standard_title_without_suffix", reasons(issues))
        self.assertIn("standard_requirement_without_suffix", reasons(issues))


class DateAndVersionTests(unittest.TestCase):
    def test_dates_are_not_zero_padded(self) -> None:
        self.assertEqual(parse_cn_date("2025 年7 月9 日"), (date(2025, 7, 9), None))
        parsed, issue = parse_cn_date("2025年07月09日")
        self.assertEqual(parsed, date(2025, 7, 9))
        self.assertEqual(issue["reason_code"], "date_zero_padded")
        self.assertEqual(issue["expected"], "2025年7月9日")
        self.assertEqual(parse_cn_date("2025-07-09")[1]["reason_code"], "date_format")

    def test_cover_version_follows_issue_date(self) -> None:
        self.assertEqual(expected_cover_version(date(2026, 1, 19))[0], "5/2")
        self.assertEqual(expected_cover_version(date(2026, 4, 7))[0], "5/3")
        self.assertIsNone(expected_cover_version(date(2025, 1, 1)))


@unittest.skipUnless((ROOT / "素材/report").is_dir(), "本地 Report 素材不存在")
class FrontMatterSampleTests(unittest.TestCase):
    """Known findings in the local sample Reports (素材 is not published)."""

    @staticmethod
    def run_sample(sample: str):
        try:
            import pymupdf as fitz
        except ImportError:  # pragma: no cover
            import fitz
        from mvp.report_front_rules import front_matter_findings

        path = next((ROOT / "素材/report" / sample).glob("*.pdf"))
        with fitz.open(path) as document:
            return front_matter_findings(document)

    @staticmethod
    def failing(findings, rule_id):
        return [item["reason_code"] for item in findings[rule_id]["details"]["checks"] if item["status"] == "error"]

    def test_2795_inspection_items_comma(self) -> None:
        findings = self.run_sample("2795")
        self.assertEqual(self.failing(findings, "REPORT-R12"), ["inspection_items_separator"])
        self.assertEqual(findings["REPORT-R13"]["status"], "pass")

    def test_5332_bracket_width(self) -> None:
        findings = self.run_sample("5332")
        self.assertEqual(
            self.failing(findings, "REPORT-R13"),
            ["name_bracket_width_inconsistent", "name_bracket_width_inconsistent"],
        )

    def test_2948_front_matter_is_clean(self) -> None:
        findings = self.run_sample("2948")
        self.assertEqual({rule_id: item["status"] for rule_id, item in findings.items()}, {
            "REPORT-R12": "pass",
            "REPORT-R13": "pass",
            "REPORT-R14": "pass",
            "REPORT-R15": "pass",
            "REPORT-R16": "pass",
        })
        version = next(item for item in findings["REPORT-R14"]["details"]["checks"] if item["check_id"] == "R14-version")
        self.assertEqual(version["reason_code"], "cover_version_ok")


if __name__ == "__main__":
    unittest.main()
