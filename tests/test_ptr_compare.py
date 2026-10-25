from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from pathlib import Path

import pymupdf as fitz

from mvp.ptr_compare import (
    EXCLUDED,
    MANUAL,
    MISMATCH,
    MATCHED,
    apply_llm_review,
    compare_ptr_report,
    extract_ptr_clauses,
    extract_report_clauses,
    normalize_comparison_text,
    run_llm_reviews,
)
from mvp.ptr_compare import (
    _clause_parent,
    _compare_text,
    _numeric_signature,
    _raw_numeric_signature,
    _reference_cycle_candidates,
    _unit_labels,
)
from mvp.ptr_layout import _reference_bindings, _split_inline_clauses
from mvp.ptr_llm import PtrLlmReviewError, build_review_task, validate_llm_review
from mvp.ptr_llm_http import OpenAICompatiblePtrLlmProvider, PtrLlmHttpError
from mvp.ptr_ocr import iter_ptr_ocr_lines


ROOT = Path(__file__).resolve().parents[1]
MATERIALS_AVAILABLE = (ROOT / "素材" / "ptr").is_dir() and (ROOT / "素材" / "report").is_dir()


def sample_paths(sample: str) -> tuple[Path, Path]:
    ptr = next((ROOT / "素材" / "ptr" / sample).glob("*.pdf"))
    report = next((ROOT / "素材" / "report" / sample).glob("*.pdf"))
    return report, ptr


class PtrCompareNormalizationTests(unittest.TestCase):
    def test_layout_and_controlled_unit_spelling_are_normalized(self) -> None:
        self.assertEqual(normalize_comparison_text("10ug/g"), "10μg/g")
        self.assertEqual(normalize_comparison_text("10 μg／g"), "10μg/g")
        self.assertEqual(
            normalize_comparison_text("细菌内毒素： 应不超过 20 EU/件"),
            normalize_comparison_text("细菌内毒素 应不超过20EU/件"),
        )

    def test_numbers_and_decimal_versions_are_not_erased(self) -> None:
        self.assertIn("2.76μs", normalize_comparison_text("2.76 μs"))
        self.assertIn("9706.202-2021", normalize_comparison_text("GB 9706.202-2021"))

    def test_controlled_si_prefixes_compare_in_base_units(self) -> None:
        self.assertEqual(_numeric_signature("0.5mA"), _numeric_signature("500μA"))
        self.assertEqual(_numeric_signature("1N·m"), _numeric_signature("100N·cm"))
        self.assertEqual(_numeric_signature("0.06米"), _numeric_signature("0.06m"))
        self.assertNotEqual(_numeric_signature("5MΩ"), _numeric_signature("5mΩ"))

    def test_numeric_parser_keeps_dimensions_and_drops_layout_numbers(self) -> None:
        self.assertEqual(_numeric_signature("0.06m³"), [("", "0.06", "m^3")])
        self.assertEqual(_numeric_signature("60000000mm3"), [("", "0.06", "m^3")])
        self.assertEqual(
            _raw_numeric_signature("0.88毫米≤A≤0.89毫米\n0.96毫米≤B≤1毫米"),
            [("", "0.88", "mm"), ("≤", "0.89", "mm"), ("", "0.96", "mm"), ("≤", "1", "mm")],
        )
        self.assertEqual(_numeric_signature("第2/17页 图2-1"), [])

    def test_report_auxiliary_lines_are_not_acceptance_facts(self) -> None:
        from mvp.ptr_compare import _split_report_auxiliary_lines

        acceptance, auxiliary = _split_report_auxiliary_lines("设置性能应顺利\n型号：471006")
        self.assertEqual(acceptance, "设置性能应顺利")
        self.assertEqual(auxiliary, ["型号：471006"])
        ptr = {
            "role": "ptr",
            "text": "距离准确度：≤5mm",
            "normalized_text": "距离准确度:≤5mm",
            "numeric_signature": _numeric_signature("距离准确度：≤5mm"),
            "raw_numeric_signature": _raw_numeric_signature("距离准确度：≤5mm"),
            "references": {"standards": [], "tables": []},
            "reference_bindings": [],
            "unit_labels": [],
            "is_heading_only": False,
            "table_rows": [],
        }
        report_text = "距离准确度：≤5mm\n单位：mm\n运动距离缩放：Quick (1.5:1)\n型号：471006"
        report = {
            "role": "report",
            "text": report_text,
            "normalized_text": "距离准确度:≤5mm",
            "numeric_signature": _numeric_signature(report_text, role="report"),
            "raw_numeric_signature": _raw_numeric_signature(report_text, role="report"),
            "references": {"standards": [], "tables": []},
            "reference_bindings": [],
            "unit_labels": _unit_labels(report_text),
            "auxiliary_lines": ["运动距离缩放：Quick (1.5:1)", "型号：471006"],
            "is_heading_only": False,
            "table_rows": [],
        }
        comparison = _compare_text(ptr, report, has_children=False)
        self.assertEqual(comparison["decision"], MATCHED)
        self.assertEqual(comparison["numeric_comparison"]["status"], MATCHED)
        self.assertEqual(comparison["unit_comparison"]["status"], MATCHED)

    def test_unit_equivalence_is_safe_and_scale_mismatch_stays_hard(self) -> None:
        ptr = {
            "role": "ptr",
            "text": "输出：3333V（峰值）57A（峰值）",
            "normalized_text": "输出:3333V(峰值)57A(峰值)",
            "numeric_signature": _numeric_signature("3333V（峰值）57A（峰值）"),
            "raw_numeric_signature": _raw_numeric_signature("3333V（峰值）57A（峰值）"),
            "references": {"standards": [], "tables": []},
            "reference_bindings": [],
            "unit_labels": [],
            "is_heading_only": False,
            "table_rows": [],
        }
        peak_report = dict(ptr)
        peak_report.update(
            role="report",
            text="输出：3333V（峰值）单位：Vp 57A（峰值）单位：Ap",
            normalized_text="输出:3333V(峰值)57A(峰值)",
            numeric_signature=_numeric_signature("3333V（峰值）单位：Vp 57A（峰值）单位：Ap", role="report"),
            raw_numeric_signature=_raw_numeric_signature("3333V（峰值）单位：Vp 57A（峰值）单位：Ap", role="report"),
            unit_labels=_unit_labels("单位：Vp 单位：Ap"),
        )
        comparison = _compare_text(ptr, peak_report, has_children=False)
        self.assertEqual(comparison["unit_comparison"]["status"], MATCHED)
        self.assertEqual(comparison["unit_comparison"]["reason_code"], "UNIT_PEAK_SUFFIX_EQUIVALENT")

        scale_report = dict(peak_report)
        scale_report.update(
            text="输出：5MΩ 单位：GΩ",
            normalized_text="输出:5MΩ",
            numeric_signature=_numeric_signature("5MΩ", role="report"),
            raw_numeric_signature=_raw_numeric_signature("5MΩ", role="report"),
            unit_labels=_unit_labels("单位：GΩ"),
        )
        scale_ptr = dict(ptr, text="输出：5MΩ", normalized_text="输出:5MΩ",
                         numeric_signature=_numeric_signature("5MΩ"),
                         raw_numeric_signature=_raw_numeric_signature("5MΩ"))
        mismatch = _compare_text(scale_ptr, scale_report, has_children=False)
        self.assertEqual(mismatch["unit_comparison"]["status"], MISMATCH)
        self.assertEqual(mismatch["unit_comparison"]["reason_code"], "UNIT_SCALE_MISMATCH")

    def test_inline_clause_split_accepts_punctuation_but_rejects_references(self) -> None:
        node = {
            "node_id": "ptr:clause:2.10.1.2",
            "role": "ptr",
            "clause_id": "2.10.1.2",
            "parent_clause_id": "2.10.1",
            "title": "主从操作距离准确度和距离重复性",
            "parts": [
                "主从操作距离准确度和距离重复性",
                "2.10.1.2.1，距离准确度：≤ 5mm",
                "2.10.1.2.2. 距离重复性：≤ 5mm",
                "测试结果符合：2.1.4 的要求",
            ],
            "evidence": [],
            "table_rows": [],
            "figures": [],
            "extraction_issues": [],
        }
        split = _split_inline_clauses([node])
        self.assertEqual([item["clause_id"] for item in split], ["2.10.1.2", "2.10.1.2.1", "2.10.1.2.2"])
        self.assertEqual(split[1]["title"], "距离准确度：≤ 5mm")
        self.assertEqual(split[2]["title"], "距离重复性：≤ 5mm")
        self.assertIn("2.1.4", split[-1]["parts"][0])

    def test_inline_clause_split_preserves_report_identity(self) -> None:
        node = {
            "node_id": "report:item:7:clause:2.10.2",
            "role": "report",
            "sequence": 7,
            "clause_id": "2.10.2",
            "parent_clause_id": "2.10",
            "title": "有效工作空间",
            "parts": ["有效工作空间", "2.10.2.1，MTM 工作空间 ≥ 0.06m3"],
            "evidence": [],
            "table_rows": [],
            "figures": [],
            "extraction_issues": [],
        }
        split = _split_inline_clauses([node])
        self.assertEqual(split[1]["node_id"], "report:item:7:clause:2.10.2.1")
        self.assertEqual(split[1]["role"], "report")

    def test_lettered_subitems_get_stable_parent_ids(self) -> None:
        node = {
            "node_id": "ptr:clause:2.3.6.6",
            "role": "ptr",
            "clause_id": "2.3.6.6",
            "title": "使用性能",
            "parts": ["使用性能", "a) 开合性能应顺利。", "b) 夹持力应符合表1。"],
            "evidence": [],
            "table_rows": [],
            "figures": [],
            "extraction_issues": [],
        }
        split = _split_inline_clauses([node])
        self.assertEqual([item["clause_id"] for item in split], ["2.3.6.6", "2.3.6.6a", "2.3.6.6b"])
        self.assertEqual(split[1]["parent_clause_id"], "2.3.6.6")
        self.assertEqual(_clause_parent("2.3.6.6a"), "2.3.6.6")

    def test_reference_binding_keeps_unresolved_tokens_manual(self) -> None:
        node = {"references": {"tables": ["表99"]}, "table_rows": []}
        self.assertEqual(_reference_bindings(node), [{"label": "表99", "status": "unresolved", "table_ids": []}])

    def test_coverage_ledger_keeps_report_only_clause_as_missing(self) -> None:
        def node(role: str, clause_id: str, text: str) -> dict:
            return {
                "node_id": f"{role}:clause:{clause_id}",
                "role": role,
                "clause_id": clause_id,
                "parent_clause_id": clause_id.rsplit(".", 1)[0],
                "title": text,
                "text": text,
                "normalized_text": text,
                "numeric_signature": [],
                "raw_numeric_signature": [],
                "references": {"standards": [], "tables": []},
                "reference_bindings": [],
                "unit_labels": [],
                "is_heading_only": False,
                "pages": [1],
                "evidence": [{"role": role, "pdf_page": 1, "bbox": [1, 1, 10, 10], "source_sha256": role}],
                "table_rows": [],
                "source_sha256": role,
            }

        report_node = node("report", "2.1", "同一要求")
        report_only = node("report", "2.2", "Report 额外要求")
        ptr_node = node("ptr", "2.1", "同一要求")
        report = {
            "source_sha256": "report",
            "nodes": [report_node, report_only],
            "nodes_by_clause": {"2.1": [report_node], "2.2": [report_only]},
            "scope": {"status": "parsed", "included_clause_ids": ["2.1", "2.2"], "excluded_clause_ids": []},
            "diagnostics": {"status": "parsed"},
        }
        ptr = {
            "source_sha256": "ptr",
            "nodes": [ptr_node],
            "diagnostics": {"status": "parsed", "ocr_used": False},
        }
        with patch("mvp.ptr_compare.extract_report_clauses", return_value=report), patch(
            "mvp.ptr_compare.extract_ptr_clauses", return_value=ptr
        ):
            result = compare_ptr_report("report.pdf", "ptr.pdf", comparison_mode="rules")
        missing = next(entry for entry in result["coverage_ledger"] if entry["clause_id"] == "2.2")
        self.assertEqual(missing["decision"], MISMATCH)
        self.assertEqual(missing["reason_code"], "PTR_CLAUSE_MISSING")
        self.assertTrue(result["summary"]["conserved"])

    def test_declared_scope_hole_is_explicitly_unaccounted(self) -> None:
        report = {
            "source_sha256": "report",
            "nodes": [],
            "nodes_by_clause": {},
            "scope": {
                "status": "parsed",
                "included_clause_ids": ["2.9"],
                "excluded_clause_ids": [],
            },
            "diagnostics": {"status": "parsed"},
        }
        ptr = {
            "source_sha256": "ptr",
            "nodes": [],
            "diagnostics": {"status": "parsed", "ocr_used": False},
        }
        with patch("mvp.ptr_compare.extract_report_clauses", return_value=report), patch(
            "mvp.ptr_compare.extract_ptr_clauses", return_value=ptr
        ):
            result = compare_ptr_report("report.pdf", "ptr.pdf", comparison_mode="rules")
        hole = next(entry for entry in result["coverage_ledger"] if entry["clause_id"] == "2.9")
        self.assertEqual(hole["decision"], MISMATCH)
        self.assertEqual(hole["reason_code"], "SCOPE_ITEM_UNACCOUNTED")
        self.assertEqual(result["summary"]["eligible"], 1)
        self.assertEqual(result["summary"]["accounted"], 0)
        self.assertFalse(result["summary"]["conserved"])
        self.assertEqual(result["coverage_audit"]["missing_scope_items"], ["2.9"])

    def test_identical_extracted_table_can_leave_manual_state(self) -> None:
        ptr = {
            "text": "表1 参数",
            "normalized_text": "表1参数",
            "numeric_signature": [],
            "references": {"standards": [], "tables": ["表1"]},
            "unit_labels": [],
            "is_heading_only": True,
            "table_rows": [{"rows": [["参数", "值"], ["频率", "100"]]}],
        }
        report = {
            "text": "表1 参数",
            "normalized_text": "表1参数",
            "numeric_signature": [],
            "references": {"standards": [], "tables": ["表1"]},
            "unit_labels": [],
            "is_heading_only": True,
            "table_rows": [{"rows": [["参数", "值"], ["频率", "100"]]}],
        }
        comparison = _compare_text(ptr, report, has_children=False)
        self.assertEqual(comparison["table_comparison"]["status"], MATCHED)

    def test_ambiguous_reference_binding_cannot_auto_match_identical_tables(self) -> None:
        ptr = {
            "text": "见表1",
            "normalized_text": "见表1",
            "numeric_signature": [],
            "references": {"standards": [], "tables": ["表1"]},
            "reference_bindings": [{"label": "表1", "status": "ambiguous", "table_ids": ["a", "b"]}],
            "unit_labels": [],
            "is_heading_only": False,
            "table_rows": [{"rows": [["参数", "值"], ["频率", "100"]]}],
        }
        report = {
            **ptr,
            "reference_bindings": [{"label": "表1", "status": "bound", "table_ids": ["report-table"]}],
        }
        comparison = _compare_text(ptr, report, has_children=False)
        self.assertEqual(comparison["table_comparison"]["status"], MATCHED)
        self.assertEqual(comparison["reference_comparison"]["binding_status"], MANUAL)
        self.assertEqual(comparison["decision"], MANUAL)
        self.assertEqual(comparison["reason_code"], "REFERENCE_BINDING_UNRESOLVED")

    def test_reference_cycle_is_a_manual_candidate(self) -> None:
        nodes = [
            {
                "role": "ptr",
                "node_id": "ptr:clause:2.1",
                "table_rows": [
                    {"table_id": "table-1", "label": "表1", "rows": [["见表2"]]},
                    {"table_id": "table-2", "label": "表2", "rows": [["见表1"]]},
                ],
            }
        ]
        cycles = _reference_cycle_candidates(nodes)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["labels"], ["表1", "表2"])
        self.assertEqual(cycles[0]["reason_code"], "REFERENCE_CYCLE_CANDIDATE")

    def test_reference_cycle_keeps_comparison_row_manual(self) -> None:
        def node(role: str) -> dict:
            return {
                "node_id": f"{role}:clause:2.1",
                "role": role,
                "clause_id": "2.1",
                "parent_clause_id": "2",
                "title": "见表1",
                "text": "见表1",
                "normalized_text": "见表1",
                "numeric_signature": [],
                "raw_numeric_signature": [],
                "references": {"standards": [], "tables": ["表1"]},
                "reference_bindings": [{"label": "表1", "status": "bound", "table_ids": [f"{role}-table"]}],
                "unit_labels": [],
                "is_heading_only": False,
                "pages": [1],
                "evidence": [{"role": role, "pdf_page": 1, "bbox": [1, 1, 10, 10], "source_sha256": role}],
                "table_rows": [{"table_id": f"{role}-table", "label": "表1", "rows": [["见表1"]]}],
                "source_sha256": role,
            }

        report_node = node("report")
        ptr_node = node("ptr")
        report = {
            "source_sha256": "report",
            "nodes": [report_node],
            "nodes_by_clause": {"2.1": [report_node]},
            "scope": {"status": "parsed", "included_clause_ids": ["2.1"], "excluded_clause_ids": []},
            "diagnostics": {"status": "parsed"},
        }
        ptr = {"source_sha256": "ptr", "nodes": [ptr_node], "diagnostics": {"status": "parsed", "ocr_used": False}}
        with patch("mvp.ptr_compare.extract_report_clauses", return_value=report), patch(
            "mvp.ptr_compare.extract_ptr_clauses", return_value=ptr
        ):
            result = compare_ptr_report("report.pdf", "ptr.pdf", comparison_mode="rules")
        entry = next(item for item in result["coverage_ledger"] if item["clause_id"] == "2.1")
        self.assertEqual(result["reference_audit"]["status"], MANUAL)
        self.assertEqual(entry["decision"], MANUAL)
        self.assertEqual(entry["reason_code"], "REFERENCE_CYCLE_CANDIDATE")

    def test_llm_quote_must_be_found_in_the_original_side(self) -> None:
        payload = {
            "decision": "equivalent",
            "missing_concepts": [],
            "extra_concepts": [],
            "numeric_claims": [],
            "text_evidence": [{"side": "ptr", "quote": "输出应稳定", "page": 2}],
            "reason": "换行不同",
        }
        validated = validate_llm_review(payload, ptr_text="条款：输出应稳定。", report_text="输出应稳定")
        self.assertTrue(validated["validated"])
        self.assertEqual(validated["schema_version"], "ptr-llm-review-0.1")
        broken = dict(payload)
        broken["text_evidence"] = [{"side": "ptr", "quote": "模型猜测", "page": 2}]
        with self.assertRaises(PtrLlmReviewError):
            validate_llm_review(broken, ptr_text="条款：输出应稳定。", report_text="输出应稳定")

    def test_llm_task_is_bounded_and_does_not_include_a_provider_call(self) -> None:
        task = build_review_task(
            clause_id="2.6",
            reason_code="TEXT_MISMATCH",
            ptr_text="A" * 5000,
            report_text="B" * 5000,
            ptr_pages=[3],
            report_pages=[100],
            max_chars=200,
        )
        self.assertLessEqual(len(task["ptr"]["text"]), 200)
        self.assertLessEqual(len(task["report"]["text"]), 200)
        self.assertEqual(len(task["input_sha256"]), 64)
        self.assertTrue(task["prompt_version"])
        self.assertNotIn("model", task)

    def test_openai_compatible_provider_is_explicit_and_json_only(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"{\\"decision\\":\\"uncertain\\"}"}}]}'

        provider = OpenAICompatiblePtrLlmProvider(
            endpoint="https://llm.example.test/v1/chat/completions",
            api_key="secret-key",
            model_id="test-model",
        )
        with patch("mvp.ptr_llm_http.urlopen", return_value=Response()) as request:
            payload = provider.review({"instructions": ["返回 JSON"], "clause_id": "2.1"})
        self.assertEqual(payload["decision"], "uncertain")
        sent = json.loads(request.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(sent["model"], "test-model")
        self.assertNotIn("secret-key", json.dumps(sent, ensure_ascii=False))

    def test_openai_compatible_provider_rejects_malformed_response(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"not-json"

        provider = OpenAICompatiblePtrLlmProvider(
            endpoint="https://llm.example.test/v1/chat/completions",
            api_key="secret-key",
            model_id="test-model",
        )
        with patch("mvp.ptr_llm_http.urlopen", return_value=Response()), self.assertRaises(PtrLlmHttpError):
            provider.review({"instructions": []})

    def test_validated_llm_equivalence_can_only_clear_text_manual(self) -> None:
        result = {
            "comparison_mode": "rules+llm",
            "publishable": False,
            "llm": {
                "review_queue": [
                    build_review_task(
                        clause_id="2.1",
                        reason_code="LLM_REVIEW_PENDING",
                        ptr_text="产品应具备稳定输出",
                        report_text="产品应具备稳定输出",
                        ptr_pages=[2],
                        report_pages=[10],
                    )
                ]
            },
            "coverage_ledger": [
                {
                    "clause_id": "2.1",
                    "decision": MANUAL,
                    "reason_code": "LLM_REVIEW_PENDING",
                    "numeric_comparison": {"status": MATCHED},
                    "unit_comparison": {"status": MATCHED},
                    "reference_comparison": {"status": MATCHED},
                    "table_comparison": {"status": MATCHED},
                }
            ],
            "summary": {"counts": {MATCHED: 0, MISMATCH: 0, MANUAL: 1, EXCLUDED: 0}},
        }
        updated = apply_llm_review(
            result,
            "2.1",
            {
                "decision": "equivalent",
                "missing_concepts": [],
                "extra_concepts": [],
                "numeric_claims": [],
                "text_evidence": [{"side": "ptr", "quote": "稳定输出", "page": 2}],
                "reason": "仅有排版差异",
            },
        )
        self.assertEqual(updated["coverage_ledger"][0]["decision"], MATCHED)
        self.assertEqual(updated["coverage_ledger"][0]["reason_code"], "LLM_VERIFIED_TEXT_EQUIVALENCE")
        self.assertTrue(updated["coverage_ledger"][0]["llm_review"]["task_input_sha256"])
        self.assertFalse(updated["publishable"])

    def test_provider_is_explicit_and_cannot_publish_the_draft(self) -> None:
        result = {
            "comparison_mode": "rules+llm",
            "publishable": False,
            "llm": {"review_queue": [build_review_task(
                clause_id="2.1", reason_code="LLM_REVIEW_PENDING",
                ptr_text="稳定输出", report_text="稳定输出", ptr_pages=[1], report_pages=[2]
            )]},
            "coverage_ledger": [{
                "clause_id": "2.1", "decision": MANUAL, "reason_code": "LLM_REVIEW_PENDING",
                "numeric_comparison": {"status": MATCHED}, "unit_comparison": {"status": MATCHED},
                "reference_comparison": {"status": MATCHED}, "table_comparison": {"status": MATCHED},
            }],
            "summary": {"counts": {MATCHED: 0, MISMATCH: 0, MANUAL: 1, EXCLUDED: 0}},
        }

        class FakeProvider:
            provider_id = "test-provider"
            model_id = "test-model"

            def review(self, task):
                return {"decision": "equivalent", "missing_concepts": [], "extra_concepts": [],
                        "numeric_claims": [], "text_evidence": [{"side": "ptr", "quote": "稳定输出", "page": 1}],
                        "reason": "等价"}

        updated = run_llm_reviews(result, FakeProvider())
        self.assertTrue(updated["llm"]["used"])
        self.assertEqual(updated["llm"]["provider_id"], "test-provider")
        self.assertEqual(updated["coverage_ledger"][0]["decision"], MATCHED)
        self.assertEqual(updated["llm"]["audit_log"][0]["action"], "cleared_text_manual")
        self.assertEqual(updated["llm"]["audit_log"][0]["input_sha256"], updated["llm"]["validated_responses"][0]["input_sha256"])
        self.assertFalse(updated["publishable"])

    def test_llm_provider_cannot_clear_a_hard_numeric_difference(self) -> None:
        result = {
            "comparison_mode": "rules+llm",
            "publishable": False,
            "llm": {"review_queue": [build_review_task(
                clause_id="2.5", reason_code="TEXT_REVIEW_REQUIRED",
                ptr_text="最大值 5MΩ", report_text="最大值 5GΩ", ptr_pages=[1], report_pages=[2]
            )]},
            "coverage_ledger": [{
                "clause_id": "2.5", "decision": MISMATCH, "reason_code": "UNIT_MISMATCH",
                "numeric_comparison": {"status": MATCHED}, "unit_comparison": {"status": MISMATCH},
                "reference_comparison": {"status": MATCHED}, "table_comparison": {"status": MATCHED},
            }],
            "summary": {"counts": {MATCHED: 0, MISMATCH: 1, MANUAL: 0, EXCLUDED: 0}},
        }

        class FakeProvider:
            provider_id = "test-provider"
            model_id = "test-model"

            def review(self, task):
                return {
                    "decision": "equivalent",
                    "missing_concepts": [],
                    "extra_concepts": [],
                    "numeric_claims": [],
                    "text_evidence": [{"side": "ptr", "quote": "最大值 5MΩ", "page": 1}],
                    "reason": "模型认为只是措辞差异",
                }

        updated = run_llm_reviews(result, FakeProvider())
        self.assertEqual(updated["coverage_ledger"][0]["decision"], MISMATCH)
        self.assertEqual(updated["coverage_ledger"][0]["reason_code"], "UNIT_MISMATCH")
        self.assertEqual(updated["llm"]["audit_log"][0]["action"], "retained_manual_or_hard_difference")

    def test_ocr_adapter_keeps_pdf_point_evidence(self) -> None:
        with fitz.open() as document:
            document.new_page(width=100, height=100)
            with patch("mvp.ptr_ocr.available_ptr_ocr_backends", return_value=["fake"]), patch(
                "mvp.ptr_ocr._run_backend",
                return_value=[{"text": "2.1 测试要求", "confidence": 0.91, "bbox": [10, 20, 80, 40]}],
            ):
                # The adapter takes a path so use a temporary one only for this
                # small geometry/contract test.
                import tempfile

                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "blank.pdf"
                    document.save(path)
                    lines, diagnostics = iter_ptr_ocr_lines(path)
        self.assertEqual(diagnostics["status"], "observed")
        self.assertEqual(diagnostics["used_backends"], ["fake"])
        self.assertEqual(lines[0][0], 1)
        self.assertEqual(lines[0][3], "ocr:fake")
        self.assertEqual(lines[0][4], 0.91)
        self.assertEqual(len(lines[0][1]), 4)
        self.assertGreater(lines[0][1][2], lines[0][1][0])


@unittest.skipUnless(MATERIALS_AVAILABLE, "本地 PTR/Report 素材不存在")
class PtrCompareSampleTests(unittest.TestCase):
    def test_all_material_samples_keep_hard_fields_identical_with_optional_llm_mode(self) -> None:
        samples = sorted(
            sample_dir.name
            for sample_dir in (ROOT / "素材" / "ptr").iterdir()
            if sample_dir.is_dir() and list(sample_dir.glob("*.pdf")) and list((ROOT / "素材" / "report" / sample_dir.name).glob("*.pdf"))
        )
        self.assertEqual(len(samples), 12)
        hard_fields = ("numeric_comparison", "unit_comparison", "reference_comparison", "table_comparison")
        for sample in samples:
            with self.subTest(sample=sample):
                report_path, ptr_path = sample_paths(sample)
                rules = compare_ptr_report(report_path, ptr_path, comparison_mode="rules")
                with_llm = compare_ptr_report(report_path, ptr_path, comparison_mode="rules+llm")
                self.assertFalse(with_llm["publishable"])
                self.assertFalse(with_llm["llm"]["used"])
                rules_by_id = {entry["id"]: entry for entry in rules["coverage_ledger"]}
                llm_by_id = {entry["id"]: entry for entry in with_llm["coverage_ledger"]}
                self.assertEqual(set(rules_by_id), set(llm_by_id))
                for ledger_id in rules_by_id:
                    for field in hard_fields:
                        self.assertEqual(
                            rules_by_id[ledger_id].get(field),
                            llm_by_id[ledger_id].get(field),
                            f"{sample}/{ledger_id}/{field}",
                        )

    def test_0596_keeps_deep_clause_tree_and_evidence(self) -> None:
        _, ptr_path = sample_paths("0596")
        result = extract_ptr_clauses(ptr_path)
        self.assertEqual(result["diagnostics"]["status"], "parsed")
        self.assertEqual(result["diagnostics"]["candidate_count"], 65)
        ids = {node["clause_id"] for node in result["nodes"]}
        self.assertIn("2.8.1.2", ids)
        self.assertNotIn("2.76", ids)
        self.assertNotIn("2.0", ids)
        node = next(node for node in result["nodes"] if node["clause_id"] == "2.8.1.2")
        self.assertEqual(node["parent_clause_id"], "2.8.1")
        self.assertTrue(node["evidence"][0]["source_sha256"])
        self.assertEqual(node["evidence"][0]["role"], "ptr")

    def test_0775_recovers_punctuation_separated_children(self) -> None:
        report_path, ptr_path = sample_paths("0775")
        ptr = extract_ptr_clauses(ptr_path)
        report = extract_report_clauses(report_path)
        ptr_ids = {node["clause_id"] for node in ptr["nodes"]}
        report_ids = {node["clause_id"] for node in report["nodes"]}
        for clause_id in ("2.10.1.2.1", "2.10.1.2.2", "2.10.2.1", "2.10.2.2"):
            self.assertIn(clause_id, ptr_ids)
            self.assertIn(clause_id, report_ids)

    def test_2948_joins_split_clause_number_and_title_columns(self) -> None:
        report_path, ptr_path = sample_paths("2948")
        ptr = extract_ptr_clauses(ptr_path)
        result = compare_ptr_report(report_path, ptr_path, comparison_mode="rules")
        nodes = {node["clause_id"]: node for node in ptr["nodes"]}
        self.assertEqual(ptr["diagnostics"]["duplicates"], [])
        self.assertEqual(nodes["2.4.1"]["title"], "安全通用要求")
        self.assertEqual(nodes["2.4.2"]["title"], "电磁兼容要求")
        self.assertIn("50-350", nodes["2.1.13"]["text"])
        self.assertIn("e) 具有心电R 波识别功能", nodes["2.1.14"]["text"])
        by_clause = {entry["clause_id"]: entry for entry in result["coverage_ledger"]}
        self.assertEqual(by_clause["2.4.1"]["decision"], MATCHED)
        self.assertEqual(by_clause["2.4.2"]["decision"], MATCHED)
        self.assertEqual(result["summary"]["counts"][MISMATCH], 0)

    def test_1539_known_numeric_differences_remain_hard_mismatches(self) -> None:
        report_path, ptr_path = sample_paths("1539")
        rules = compare_ptr_report(report_path, ptr_path, comparison_mode="rules")
        by_clause = {entry["clause_id"]: entry for entry in rules["coverage_ledger"]}
        self.assertEqual(rules["scope"]["included_clause_ids"], ["2.2", "2.5", "2.6"])
        self.assertEqual(by_clause["2.2.2"]["decision"], MANUAL)
        self.assertEqual(by_clause["2.2.2"]["reason_code"], "TABLE_CONTENT_NOT_EXTRACTED")
        self.assertEqual(by_clause["2.2.6"]["decision"], MATCHED)
        self.assertEqual(by_clause["2.6"]["decision"], MATCHED)
        self.assertEqual(by_clause["2.6"]["table_comparison"]["reason_code"], "TABLE_SEMANTIC_NORMALIZED_MATCH")
        self.assertEqual(by_clause["2.6"]["reference_comparison"]["ptr_bindings"][0]["status"], "bound")
        self.assertEqual(by_clause["2.1"]["decision"], EXCLUDED)
        self.assertTrue(rules["summary"]["conserved"])
        self.assertFalse(rules["publishable"])

    def test_rules_plus_llm_only_reviews_text_uncertainty(self) -> None:
        report_path, ptr_path = sample_paths("1539")
        result = compare_ptr_report(report_path, ptr_path, comparison_mode="rules+llm")
        by_clause = {entry["clause_id"]: entry for entry in result["coverage_ledger"]}
        self.assertEqual(by_clause["2.2.2"]["decision"], MANUAL)
        self.assertEqual(by_clause["2.2.6"]["decision"], MATCHED)
        self.assertEqual(by_clause["2.6"]["decision"], MATCHED)
        self.assertEqual(by_clause["2.6"]["reason_code"], "EXACT_NORMALIZED_MATCH")
        self.assertFalse(result["llm"]["used"])
        self.assertEqual(result["llm"]["queue_count"], 0)
        self.assertEqual(result["llm"]["review_queue"], [])
        self.assertFalse(result["publishable"])

    def test_2795_reports_unit_conflict_and_table_manual_queue(self) -> None:
        report_path, ptr_path = sample_paths("2795")
        result = compare_ptr_report(report_path, ptr_path)
        by_clause = {entry["clause_id"]: entry for entry in result["coverage_ledger"]}
        self.assertEqual(
            result["scope"]["included_clause_ids"],
            ["2.1", "2.2", "2.3", "2.4", "2.5", "2.6", "2.7"],
        )
        self.assertEqual(by_clause["2.5"]["decision"], MISMATCH)
        self.assertEqual(by_clause["2.5"]["reason_code"], "UNIT_MISMATCH")
        self.assertEqual(by_clause["2.6.1"]["decision"], MANUAL)
        # The report-side Appendix A binding is unresolved, so the ledger
        # keeps the more specific reference reason while retaining the table
        # extraction evidence as a manual sub-check.
        self.assertEqual(by_clause["2.6.1"]["reason_code"], "REFERENCE_BINDING_UNRESOLVED")
        self.assertEqual(by_clause["2.6.1"]["table_comparison"]["reason_code"], "TABLE_CONTENT_NOT_EXTRACTED")
        self.assertEqual(result["summary"]["eligible"], result["summary"]["accounted"])
        self.assertTrue(all(entry["evidence"] for entry in result["coverage_ledger"]))

    def test_1539_cross_page_table_rows_are_reconstructed(self) -> None:
        report_path, _ = sample_paths("1539")
        report = extract_report_clauses(report_path)
        node = next(node for node in report["nodes"] if node.get("clause_id") == "2.6")
        rows = node["table_rows"]
        self.assertGreaterEqual(len(rows), 20)
        self.assertEqual({row["table_id"] for row in rows}, {"report:table:item:159:clause:2.6"})
        self.assertEqual(node["text"], "软件功能\n射频脉冲电场消融系统软件应具备以下功能：\n表6 软件功能")
        self.assertTrue(any(row["matrix_row"][0] == "" and row["matrix_row"][1] == "显示消融图" for row in rows))

    def test_0780_deep_lettered_scope_keeps_ledger_ids_unique(self) -> None:
        report_path, ptr_path = sample_paths("0780")
        result = compare_ptr_report(report_path, ptr_path)
        ledger = result["coverage_ledger"]
        self.assertTrue(result["summary"]["conserved"])
        self.assertEqual(len(ledger), len({entry["id"] for entry in ledger}))
        self.assertTrue(any(entry["clause_id"] == "2.3.6.6" for entry in ledger))
        self.assertTrue(any(entry["clause_id"] == "2.3.13.1" for entry in ledger))

    def test_5782_without_text_layer_is_manual_instead_of_missing_or_mismatch(self) -> None:
        report_path, ptr_path = sample_paths("5782")
        result = compare_ptr_report(report_path, ptr_path)
        self.assertIn(
            result["ptr"]["diagnostics"]["reason_code"],
            {"PTR_TEXT_LAYER_MISSING", "PTR_OCR_CANDIDATES_MANUAL"},
        )
        if result["ptr"]["diagnostics"]["reason_code"] == "PTR_OCR_CANDIDATES_MANUAL":
            self.assertEqual(result["ptr"]["diagnostics"]["ocr"]["status"], "observed")
        eligible = [entry for entry in result["coverage_ledger"] if entry["decision"] != EXCLUDED]
        self.assertTrue(eligible)
        self.assertTrue(all(entry["decision"] == MANUAL for entry in eligible))
        self.assertEqual(result["summary"]["counts"][MISMATCH], 0)
        self.assertEqual(result["summary"]["decision"], MANUAL)
        self.assertFalse(result["publishable"])


if __name__ == "__main__":
    unittest.main()
