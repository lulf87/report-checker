from __future__ import annotations

import unittest
from pathlib import Path

from mvp.checker import SAMPLE_CONFIGS, sha256_file
from mvp.full_record_61 import (
    EXPECTED_ALTERNATE_BOX_TRIPLETS,
    EXPECTED_NATIVE_BOX_TRIPLETS,
    EXPECTED_STATUS_ROWS,
    NUMERIC_TARGETS_BY_SAMPLE,
    _accepted_dual_channel_value,
    _accepted_dual_channel_values,
    _clause_compatible,
    _conclusion_check,
    _effective_report_result,
    _mapping_edges,
    _numeric_manual_comparison,
    _target_source_cells,
    run_record_61_full,
)
from mvp.record_full import ReportRow


ROOT = Path(__file__).resolve().parents[1]


def _valid_location(value: dict) -> bool:
    bbox = value.get("bbox")
    return bool(
        isinstance(value.get("pdf_page"), int)
        and value["pdf_page"] >= 1
        and isinstance(bbox, list)
        and len(bbox) == 4
        and bbox[0] < bbox[2]
        and bbox[1] < bbox[3]
    )


class FullRecord61Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.paths: dict[str, tuple[Path, Path]] = {}
        cls.hashes: dict[str, tuple[str, str]] = {}
        cls.results: dict[str, dict] = {}
        for sample in ("1347", "1539", "2948"):
            config = SAMPLE_CONFIGS[sample]
            assert config.record61 is not None
            report_path = ROOT / config.report
            record_path = ROOT / config.record61
            cls.paths[sample] = (report_path, record_path)
            cls.hashes[sample] = (sha256_file(report_path), sha256_file(record_path))
            cls.results[sample] = run_record_61_full(report_path, record_path)

    def test_complete_status_inventory_includes_the_two_alternate_glyph_rows(self) -> None:
        for sample, result in self.results.items():
            extraction = result["extraction"]
            with self.subTest(sample=sample):
                self.assertEqual(extraction["status_row_count"], EXPECTED_STATUS_ROWS)
                self.assertEqual(
                    extraction["native_box_triplet_count"],
                    EXPECTED_NATIVE_BOX_TRIPLETS,
                )
                self.assertEqual(
                    extraction["alternate_glyph_triplet_count"],
                    EXPECTED_ALTERNATE_BOX_TRIPLETS,
                )
                self.assertEqual(
                    extraction["status_row_count"],
                    extraction["native_box_triplet_count"]
                    + extraction["alternate_glyph_triplet_count"],
                )

    def test_alternate_glyph_rows_are_not_silently_dropped(self) -> None:
        for sample, result in self.results.items():
            alternate = result["extraction"]["alternate_glyph_rows"]
            by_source: dict[str, list[dict]] = {}
            for entry in result["ledger"]:
                by_source.setdefault(entry.get("source_row_id"), []).append(entry)
            with self.subTest(sample=sample):
                self.assertEqual(
                    [(row["pdf_page"], row["clause"]) for row in alternate],
                    [(70, "11.1.3"), (94, "16.9.1")],
                )
                expected_sequences = {70: 85, 94: 117}
                for row in alternate:
                    entries = by_source.get(row["row_id"], [])
                    self.assertTrue(entries)
                    self.assertIn(
                        expected_sequences[row["pdf_page"]],
                        {entry["report"]["sequence"] for entry in entries},
                    )
                    self.assertTrue(all(_valid_location(entry["record_location"]) for entry in entries))
                    self.assertTrue(all(_valid_location(entry["report_location"]) for entry in entries))

    def test_frozen_numeric_target_counts_are_emitted_as_separate_ledger_rows(self) -> None:
        for sample, result in self.results.items():
            numeric = result["coverage"]["numeric_targets"]
            expected = NUMERIC_TARGETS_BY_SAMPLE[int(sample)]
            numeric_entries = [
                entry
                for entry in result["ledger"]
                if entry["rule_id"] in {"RECORD61-BODY-NUMERIC", "RECORD61-BODY-PERCENT"}
            ]
            with self.subTest(sample=sample):
                self.assertEqual(numeric["target_counts"], expected)
                self.assertEqual(numeric["expected_target_counts"], expected)
                self.assertEqual(numeric["total_target_count"], sum(expected.values()))
                self.assertEqual(len(numeric_entries), sum(expected.values()))
                self.assertTrue(all(entry["disposition"] == "manual" for entry in numeric_entries))
                self.assertTrue(all(_valid_location(entry["record_location"]) for entry in numeric_entries))
                self.assertTrue(all(_valid_location(entry["report_location"]) for entry in numeric_entries))

    def test_numeric_targets_are_not_reprocessed_by_the_status_mapper(self) -> None:
        for sample, result in self.results.items():
            numeric_target_ids = {
                entry["target_row_id"]
                for entry in result["ledger"]
                if entry["rule_id"] in {"RECORD61-BODY-NUMERIC", "RECORD61-BODY-PERCENT"}
            }
            status_target_ids = {
                entry["target_row_id"]
                for entry in result["ledger"]
                if entry["rule_id"] == "RECORD61-BODY-STATUS"
                and entry["target_row_id"] is not None
            }
            with self.subTest(sample=sample):
                self.assertTrue(numeric_target_ids)
                self.assertTrue(numeric_target_ids.isdisjoint(status_target_ids))

    def test_duplicate_printed_pages_retain_physical_identity(self) -> None:
        candidates = self.results["2948"]["coverage"]["numeric_targets"]["record_candidates"]
        flattened = [row for rows in candidates.values() for row in rows]
        keys = [row["source_key"] for row in flattened]
        self.assertEqual(len(keys), len(set(keys)))
        page_107 = [row for row in candidates["8.7"] if row["printed_page"] == 107]
        page_108 = [row for row in candidates["8.7"] if row["printed_page"] == 108]
        page_111 = [row for row in candidates["8.7"] if row["printed_page"] == 111]
        self.assertEqual(
            {(row["physical_page"], row["occurrence_index"]) for row in page_107},
            {(108, 1), (109, 2)},
        )
        self.assertEqual(
            {(row["physical_page"], row["occurrence_index"]) for row in page_108},
            {(110, 1), (111, 2)},
        )
        self.assertEqual(
            {(row["physical_page"], row["occurrence_index"]) for row in page_111},
            {(114, 1), (115, 2)},
        )

    def test_identity_body_numeric_and_sequence_118_share_one_conserved_ledger(self) -> None:
        for sample, result in self.results.items():
            coverage = result["coverage"]
            ledger = result["ledger"]
            source_ids = set(coverage["source_rows"]["row_ids"])
            target_ids = set(coverage["report_rows"]["row_ids"])
            seen_source = {
                source_id
                for entry in ledger
                for source_id in entry.get("source_row_ids", [])
            }
            seen_target = {
                target_id
                for entry in ledger
                for target_id in entry.get("target_row_ids", [])
            }
            with self.subTest(sample=sample):
                self.assertEqual(coverage["source_rows"]["eligible"], len(source_ids))
                self.assertEqual(coverage["report_rows"]["eligible"], len(target_ids))
                self.assertEqual(coverage["source_rows"]["accounted"], len(source_ids))
                self.assertEqual(coverage["report_rows"]["accounted"], len(target_ids))
                self.assertTrue(coverage["source_rows"]["conserved"])
                self.assertTrue(coverage["report_rows"]["conserved"])
                self.assertEqual(source_ids, seen_source)
                self.assertEqual(target_ids, seen_target)
                identity = [entry for entry in ledger if entry["rule_id"] == "RECORD61-IDENTITY"]
                self.assertEqual(len(identity), 5)
                self.assertEqual(
                    [round(entry["record"]["baseline_y"], 2) for entry in identity],
                    [225.45, 250.65, 275.60, 300.45, 325.65],
                )
                self.assertTrue(all(entry["record"]["ink_bboxes"] for entry in identity))
                scope = [entry for entry in ledger if entry["entry_id"] == "RECORD61-SCOPE-REPORT-118"]
                self.assertEqual(len(scope), 1)
                self.assertEqual(scope[0]["disposition"], "not_applicable")
                self.assertIn("no_chapter_17", scope[0]["reason_code"])

    def test_1347_long_sample_name_evidence_is_not_clipped_to_template_line(self) -> None:
        entry = next(
            item
            for item in self.results["1347"]["ledger"]
            if item["entry_id"] == "RECORD61-IDENTITY-SAMPLE_NAME"
        )
        self.assertGreater(entry["record_location"]["bbox"][2], 547.0)

    def test_sequence_results_are_independent_and_use_result_cells(self) -> None:
        for sample, result in self.results.items():
            conclusions = [
                entry
                for entry in result["ledger"]
                if entry["rule_id"] == "RECORD61-SEQUENCE-CONCLUSION"
            ]
            with self.subTest(sample=sample):
                self.assertEqual(len(conclusions), 117)
                self.assertEqual(
                    {entry["source_row_id"] for entry in conclusions},
                    {f"record61:sequence-conclusion:s{sequence:03d}" for sequence in range(1, 118)},
                )
                self.assertTrue(all(entry["report_evidence"] for entry in conclusions))
                self.assertTrue(all("result" in entry["report"] for entry in conclusions))

    def test_sequence_result_check_does_not_reuse_report_single_conclusion(self) -> None:
        row = ReportRow(
            row_id="report:test:s077",
            sequence=77,
            row_ordinal=1,
            pdf_page=53,
            project_raw="",
            clause_raw="9.7",
            requirement_raw="",
            result_raw="符合要求",
            conclusion_raw="/",
            unit_context=None,
            condition_tokens=(),
            requirement_rect=None,
            result_rect=(1.0, 1.0, 2.0, 2.0),
            conclusion_rect=(2.0, 2.0, 3.0, 3.0),
        )
        comparison = _conclusion_check([{"status": "符合"}], [row])
        self.assertEqual(comparison["decision"], "match")
        self.assertEqual(comparison["observed"], "符合")

    def test_numeric_sources_are_physical_cells_and_fixed_blocks_are_label_aligned(self) -> None:
        for sample, result in self.results.items():
            entries = [
                entry
                for entry in result["ledger"]
                if entry["rule_id"] in {"RECORD61-BODY-NUMERIC", "RECORD61-BODY-PERCENT"}
            ]
            for entry in entries:
                for source_id in entry["source_row_ids"]:
                    self.assertRegex(
                        source_id,
                        r"^record61:measure:p\d{3}:printed\d{3}:occ\d+:t\d{2}:r\d{3}:c\d{3}:",
                    )
            if sample == "1539":
                positions = {
                    entry["record"]["source_cells"][0]["semantic"].get("measurement_position")
                    for entry in entries
                    if entry["record"]["block_type"] == "8.6"
                }
                self.assertEqual(positions, {"plug_pe", "inlet_pe"})
                positions_by_row = {
                    entry["record"]["source_cells"][0]["row_index"]: entry["record"]["source_cells"][0]["semantic"].get("measurement_position")
                    for entry in entries
                    if entry["record"]["block_type"] == "8.6"
                }
                self.assertEqual(positions_by_row, {2: "plug_pe", 3: "inlet_pe"})

    def test_known_864_false_positive_is_removed_and_known_2948_differences_remain(self) -> None:
        mismatches_by_sample = {
            sample: {
                entry["entry_id"]
                for entry in result["ledger"]
                if entry["disposition"] == "mismatch"
            }
            for sample, result in self.results.items()
        }
        self.assertEqual(mismatches_by_sample["1347"], set())
        self.assertEqual(mismatches_by_sample["1539"], set())
        self.assertEqual(
            mismatches_by_sample["2948"],
            {
                "RECORD61-BODY-S052-E037",
                "RECORD61-BODY-S052-E038",
                "RECORD61-BODY-S061-E008",
            },
        )

    def test_nonpassing_rows_have_real_two_sided_locations_and_no_report_rules(self) -> None:
        for sample, result in self.results.items():
            with self.subTest(sample=sample):
                self.assertEqual(result["mode"], "report_record_9706_1")
                self.assertTrue(result["findings"])
                self.assertTrue(all(entry["rule_id"].startswith("RECORD61-") for entry in result["ledger"]))
                self.assertTrue(all(item["rule_id"].startswith("RECORD61-") for item in result["findings"]))
                for entry in result["ledger"]:
                    if entry["disposition"] not in {"manual", "mismatch"}:
                        continue
                    self.assertTrue(_valid_location(entry["record_location"]))
                    self.assertTrue(_valid_location(entry["report_location"]))
                for finding in result["findings"]:
                    if finding["status"] not in {"manual", "error"}:
                        continue
                    self.assertEqual(
                        {location["role"] for location in finding["evidence_locations"]},
                        {"report", "record_9706_1"},
                    )
                    self.assertTrue(all(_valid_location(location) for location in finding["evidence_locations"]))

    def test_source_pdf_hashes_remain_unchanged(self) -> None:
        for sample, paths in self.paths.items():
            with self.subTest(sample=sample):
                self.assertEqual(
                    (sha256_file(paths[0]), sha256_file(paths[1])),
                    self.hashes[sample],
                )
                self.assertTrue(self.results[sample]["source_integrity"]["unchanged"])


class Record61NumericDecisionTests(unittest.TestCase):
    @staticmethod
    def _target(result: str, unit: str | None = "mA") -> ReportRow:
        return ReportRow(
            row_id="report:test",
            sequence=1,
            row_ordinal=1,
            pdf_page=1,
            project_raw="",
            clause_raw="8.7",
            requirement_raw="",
            result_raw=result,
            conclusion_raw="符合",
            unit_context=unit,
            condition_tokens=(),
            requirement_rect=(1, 1, 2, 2),
            result_rect=(2, 1, 3, 2),
            conclusion_rect=(3, 1, 4, 2),
        )

    def test_dual_channel_requires_one_exact_agreement(self) -> None:
        agreed = {
            "apple_vision": [{"text": "509"}],
            "tesseract": [{"text": "509"}],
        }
        disputed = {
            "apple_vision": [{"text": "509"}],
            "tesseract": [{"text": "590"}],
        }
        self.assertEqual(_accepted_dual_channel_value(agreed), "509")
        self.assertIsNone(_accepted_dual_channel_value(disputed))

    def test_unit_conversion_mismatch_interval_and_polarity_paths(self) -> None:
        self.assertEqual(
            _numeric_manual_comparison(
                "8.7", self._target("0.5"), [{"accepted_value": "509", "unit": "uA"}]
            )["decision"],
            "match",
        )
        self.assertEqual(
            _numeric_manual_comparison(
                "8.7", self._target("0.3"), [{"accepted_value": "509", "unit": "uA"}]
            )["decision"],
            "mismatch",
        )
        self.assertEqual(
            _numeric_manual_comparison(
                "8.7", self._target("<0.01"), [{"accepted_value": "<1", "unit": "uA"}]
            )["decision"],
            "match",
        )
        self.assertEqual(
            _numeric_manual_comparison(
                "8.7", self._target("0.5 (−)"), [{"accepted_value": "509", "unit": "uA"}]
            )["reason_code"],
            "report_polarity_has_no_uniquely_attributed_record_evidence",
        )

    def test_two_percentages_in_one_cell_are_mapped_by_occurrence(self) -> None:
        candidates = {
            "apple_vision": [{"text": "0.720A → 36%"}, {"text": "0.503A → 33%"}],
            "tesseract": [
                {"psm": 7, "text": "0.720 36% 0.503 33%"},
                {"psm": 8, "text": "36% 33%"},
            ],
        }
        accepted = _accepted_dual_channel_values(candidates, expected_count=2)
        self.assertEqual(accepted, ["36%", "33%"])
        self.assertIsNone(_accepted_dual_channel_values(candidates, expected_count=1))
        cell = {"accepted_value": None, "accepted_values": ["36%", "33%"], "unit": "%"}
        first = _numeric_manual_comparison(
            "4.11", self._target("36%", "%"), [cell], target_ordinal=1, target_count=2
        )
        second = _numeric_manual_comparison(
            "4.11", self._target("33%", "%"), [cell], target_ordinal=2, target_count=2
        )
        wrong = _numeric_manual_comparison(
            "4.11", self._target("32%", "%"), [cell], target_ordinal=2, target_count=2
        )
        self.assertEqual(first["decision"], "match")
        self.assertEqual(second["decision"], "match")
        self.assertEqual(wrong["decision"], "mismatch")

    def test_crossed_out_cells_are_not_numeric_sources(self) -> None:
        target = self._target("<0.01")
        target = ReportRow(
            **{
                **target.to_dict(),
                "requirement_path": ("接触电流", "正常状态"),
            }
        )
        common = {
            "semantic": {
                "metric": "touch",
                "phase": "before",
                "state": "NC",
                "current": None,
            }
        }
        selected = _target_source_cells(
            "8.7",
            {"row": target, "wet_phase": "before"},
            {
                "8.7": [
                    {**common, "row_id": "void", "void_or_crossed_out": True},
                    {**common, "row_id": "value", "void_or_crossed_out": False},
                ]
            },
        )
        self.assertEqual([cell["row_id"] for cell in selected], ["value"])

    def test_unresolved_requirements_remain_one_sided_edges(self) -> None:
        def report(row_id: str, requirement: str) -> ReportRow:
            return ReportRow(
                row_id=row_id,
                sequence=1,
                row_ordinal=1,
                pdf_page=1,
                project_raw="",
                clause_raw="8.1",
                requirement_raw=requirement,
                result_raw="符合要求",
                conclusion_raw="符合",
                unit_context=None,
                condition_tokens=(),
                requirement_rect=(1, 1, 2, 2),
                result_rect=(2, 1, 3, 2),
                conclusion_rect=(3, 1, 4, 2),
            )

        sources = [
            {"row_id": "source:matched", "requirement": "唯一对应的完整技术要求文本"},
            {"row_id": "source:unmatched", "requirement": "仅存在于原始记录的另一条要求"},
        ]
        reports = [
            report("report:matched", "唯一对应的完整技术要求文本"),
            report("report:unmatched", "仅存在报告中的其他完整要求文本"),
        ]
        edges = _mapping_edges(sources, reports)
        self.assertEqual(
            [
                (
                    source["row_id"] if source is not None else None,
                    target.row_id if target is not None else None,
                    automatic,
                    method,
                )
                for source, target, automatic, method in edges
            ],
            [
                (
                    "source:matched",
                    "report:matched",
                    True,
                    "clause_range_and_unique_requirement_text",
                ),
                (
                    "source:unmatched",
                    None,
                    False,
                    "record_requirement_not_uniquely_mapped",
                ),
                (
                    None,
                    "report:unmatched",
                    False,
                    "report_requirement_not_uniquely_mapped",
                ),
            ],
        )

    def test_short_requirement_text_maps_only_by_strict_normalized_equality(self) -> None:
        source = {
            "row_id": "source:short",
            "clause": "7.1.1",
            "requirement": "见12.2。",
            "status": "符合",
        }
        exact = self._target("符合要求")
        exact = ReportRow(**{**exact.to_dict(), "row_id": "report:exact", "requirement_raw": "见12.2。"})
        different = ReportRow(
            **{**exact.to_dict(), "row_id": "report:different", "requirement_raw": "见12.3。"}
        )

        exact_edges = _mapping_edges([source], [exact])
        self.assertEqual(
            [(item[0]["row_id"], item[1].row_id, item[2]) for item in exact_edges],
            [("source:short", "report:exact", True)],
        )
        different_edges = _mapping_edges([source], [different])
        self.assertEqual(
            [
                (
                    item[0]["row_id"] if item[0] is not None else None,
                    item[1].row_id if item[1] is not None else None,
                    item[2],
                )
                for item in different_edges
            ],
            [
                ("source:short", None, False),
                (None, "report:different", False),
            ],
        )

    def test_explicit_clause_and_clause_range_are_bounded_mapping_evidence(self) -> None:
        self.assertTrue(_clause_compatible("7.3.4", "7.3.1～7.3.8"))
        self.assertFalse(_clause_compatible("7.3.9", "7.3.1～7.3.8"))

        source = {
            "row_id": "source:clause",
            "clause": "7.3.4",
            "requirement": "应采用浅蓝色绝缘",
            "status": "符合",
        }
        target = self._target("符合要求")
        target = ReportRow(
            **{
                **target.to_dict(),
                "row_id": "report:clause-range",
                "requirement_raw": "7.3.1～7.3.8 中性线应釆用蓝色标识",
            }
        )
        edges = _mapping_edges([source], [target])
        self.assertEqual(
            [(item[0]["row_id"], item[1].row_id, item[2], item[3]) for item in edges],
            [
                (
                    "source:clause",
                    "report:clause-range",
                    True,
                    "clause_range_and_unique_explicit_clause",
                )
            ],
        )

    def test_uniquely_contained_rows_support_bounded_many_to_one_and_one_to_many(self) -> None:
        def target(row_id: str, requirement: str) -> ReportRow:
            row = self._target("符合要求")
            return ReportRow(
                **{**row.to_dict(), "row_id": row_id, "requirement_raw": requirement}
            )

        first_text = "应标记制造商的名称或商标以及联系信息"
        second_text = "应标记序列号或批号或批次标识"
        many_sources = [
            {
                "row_id": "source:first",
                "clause": "7.2.4",
                "requirement": first_text,
                "status": "符合",
            },
            {
                "row_id": "source:second",
                "clause": "7.2.4",
                "requirement": second_text,
                "status": "符合",
            },
        ]
        combined_target = target("report:combined", f"{first_text}；{second_text}。")
        many_to_one = _mapping_edges(many_sources, [combined_target])
        self.assertEqual(
            [
                (item[0]["row_id"], item[1].row_id, item[2], item[3])
                for item in many_to_one
            ],
            [
                (
                    "source:first",
                    "report:combined",
                    True,
                    "clause_range_and_unique_many_record_rows_to_one_report_row",
                ),
                (
                    "source:second",
                    "report:combined",
                    True,
                    "clause_range_and_unique_many_record_rows_to_one_report_row",
                ),
            ],
        )

        combined_source = {
            "row_id": "source:combined",
            "clause": "16.9.2.1",
            "requirement": f"{first_text}；{second_text}。",
            "status": "符合",
        }
        one_to_many = _mapping_edges(
            [combined_source],
            [target("report:first", first_text), target("report:second", second_text)],
        )
        self.assertEqual(
            [
                (item[0]["row_id"], item[1].row_id, item[2], item[3])
                for item in one_to_many
            ],
            [
                (
                    "source:combined",
                    "report:first",
                    True,
                    "clause_range_and_unique_one_record_row_to_many_report_rows",
                ),
                (
                    "source:combined",
                    "report:second",
                    True,
                    "clause_range_and_unique_one_record_row_to_many_report_rows",
                ),
            ],
        )

    def test_duplicate_group_members_remain_manual_instead_of_being_forced(self) -> None:
        text = "应标记型号或类型参考号"
        sources = [
            {"row_id": "source:a", "clause": "7.2.2", "requirement": text, "status": "符合"},
            {"row_id": "source:b", "clause": "7.2.2", "requirement": text, "status": "符合"},
        ]
        row = self._target("符合要求")
        report = ReportRow(
            **{**row.to_dict(), "row_id": "report:single", "requirement_raw": text}
        )
        edges = _mapping_edges(sources, [report])
        self.assertEqual(sum(item[2] for item in edges), 0)
        self.assertEqual(len(edges), 3)

    def test_parent_placeholder_aggregates_children_for_non_applicable_record(self) -> None:
        parent = ReportRow(
            row_id="report:parent",
            sequence=1,
            row_ordinal=1,
            pdf_page=1,
            project_raw="",
            clause_raw="8.6",
            requirement_raw="8.6.4 a) 保护接地连接",
            result_raw="——",
            conclusion_raw="符合",
            unit_context=None,
            condition_tokens=(),
            requirement_rect=(1, 1, 2, 2),
            result_rect=(2, 1, 3, 2),
            conclusion_rect=(3, 1, 4, 2),
        )
        child = ReportRow(
            row_id="report:child",
            sequence=1,
            row_ordinal=2,
            pdf_page=1,
            project_raw="",
            clause_raw="8.6",
            requirement_raw="实测阻抗",
            result_raw="0.02",
            conclusion_raw="符合",
            unit_context="Ω",
            condition_tokens=(),
            requirement_rect=(1, 2, 2, 3),
            result_rect=(2, 2, 3, 3),
            conclusion_rect=(3, 2, 4, 3),
        )
        effective, rows, method = _effective_report_result(
            {"status": "不适用", "clause": "8.6.4"},
            parent,
            [parent, child],
        )
        self.assertEqual(effective, "0.02")
        self.assertEqual(rows, ["report:child"])
        self.assertEqual(method, "parent_result_aggregated_from_child_rows")

        stopped, stopped_rows, stopped_method = _effective_report_result(
            {"status": "不适用", "clause": "8.6.4"},
            parent,
            [parent, child],
            stop_target_row_ids={"report:child"},
        )
        self.assertEqual(stopped, "——")
        self.assertEqual(stopped_rows, ["report:parent"])
        self.assertEqual(stopped_method, "physical_report_result")


if __name__ == "__main__":
    unittest.main()
