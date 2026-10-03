"""Read-only audit of GB 9706.1 manual-review reduction.

This script compares the baseline and manual-reduction result directories.  It
does not open or modify source PDFs; it reads the derived JSON result files and
emits an auditable JSON/Markdown report containing only result locations and
comparison metadata.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
from pathlib import Path
from typing import Any


SAMPLES = ("1347", "1539", "2948")
MODE = "report-record-9706-1"


def load_result(root: Path, sample: str) -> dict[str, Any]:
    path = root / sample / MODE / "result.json"
    with path.open(encoding="utf-8") as fh:
        result = json.load(fh)
    result["_result_path"] = str(path.resolve())
    return result


def index_ledger(ledger: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    by_source: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    by_target: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for entry in ledger:
        for source_id in entry.get("source_row_ids", []):
            by_source[source_id].append(entry)
        for target_id in entry.get("target_row_ids", []):
            by_target[target_id].append(entry)
    return by_source, by_target


def old_overlaps(
    entry: dict[str, Any],
    old_by_source: dict[str, list[dict[str, Any]]],
    old_by_target: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    overlapped: dict[str, dict[str, Any]] = {}
    for source_id in entry.get("source_row_ids", []):
        for old_entry in old_by_source.get(source_id, []):
            overlapped[old_entry["id"]] = old_entry
    for target_id in entry.get("target_row_ids", []):
        for old_entry in old_by_target.get(target_id, []):
            overlapped[old_entry["id"]] = old_entry
    return list(overlapped.values())


def classify(entry: dict[str, Any], source_counts: collections.Counter[str], target_counts: collections.Counter[str]) -> str:
    rule_id = entry.get("rule_id")
    if rule_id in {"RECORD61-BODY-NUMERIC", "RECORD61-BODY-PERCENT"}:
        return "numeric_ledger"
    if any(source_counts[item] > 1 for item in entry.get("source_row_ids", [])):
        return "one_to_many"
    if any(target_counts[item] > 1 for item in entry.get("target_row_ids", [])):
        return "many_to_one"
    return "one_to_one"


def compact_item(sample: str, entry: dict[str, Any], category: str, old_manual: list[dict[str, Any]]) -> dict[str, Any]:
    record = entry.get("record") or {}
    report = entry.get("report") or {}
    comparison = entry.get("comparison") or {}
    mapping = entry.get("mapping") or {}
    return {
        "sample": sample,
        "id": entry.get("id"),
        "category": category,
        "rule_id": entry.get("rule_id"),
        "reason_code": entry.get("reason_code"),
        "old_manual_ids": [item.get("id") for item in old_manual],
        "old_manual_reason_codes": sorted({item.get("reason_code") for item in old_manual}),
        "source_row_ids": entry.get("source_row_ids", []),
        "target_row_ids": entry.get("target_row_ids", []),
        "mapping_method": mapping.get("method"),
        "clause": record.get("clause"),
        "record_status": record.get("status"),
        "report_result_raw": report.get("result_raw"),
        "report_conclusion_raw": report.get("conclusion_raw"),
        "comparison_expected": comparison.get("expected"),
        "comparison_observed": comparison.get("observed"),
        "report_result_physical": comparison.get("report_result_physical"),
        "report_result_effective": comparison.get("report_result_effective"),
        "aggregation": comparison.get("result_aggregation"),
        "record_location": entry.get("record_location"),
        "report_location": entry.get("report_location"),
        "record_evidence": entry.get("record_evidence"),
        "report_evidence": entry.get("report_evidence"),
        "source_pdf": None,
        "report_pdf": None,
    }


def audit_sample(old: dict[str, Any], new: dict[str, Any], sample: str) -> dict[str, Any]:
    old_by_source, old_by_target = index_ledger(old["ledger"])
    changed: list[dict[str, Any]] = []
    provisional: list[dict[str, Any]] = []
    for entry in new["ledger"]:
        # ``matched`` is the 9706.1 ledger's automatic-consistency disposition.
        # Keep the check explicit so a future schema change cannot silently turn
        # warnings/errors into this audit's change set.
        if entry.get("disposition") != "matched":
            continue
        overlaps = old_overlaps(entry, old_by_source, old_by_target)
        old_manual = [item for item in overlaps if item.get("disposition") == "manual"]
        if not old_manual:
            continue
        provisional.append((entry, old_manual))

    source_counts = collections.Counter(
        source_id
        for entry, _ in provisional
        for source_id in entry.get("source_row_ids", [])
    )
    target_counts = collections.Counter(
        target_id
        for entry, _ in provisional
        for target_id in entry.get("target_row_ids", [])
    )

    files = {item.get("role"): item.get("path") for item in new.get("files", [])}
    for entry, old_manual in provisional:
        category = classify(entry, source_counts, target_counts)
        item = compact_item(sample, entry, category, old_manual)
        item["source_pdf"] = files.get("record_9706_1")
        item["report_pdf"] = files.get("report")
        changed.append(item)

    category_counts = collections.Counter(item["category"] for item in changed)
    return {
        "sample": sample,
        "mode": MODE,
        "baseline_result": old["_result_path"],
        "reduced_result": new["_result_path"],
        "baseline_ledger_count": len(old["ledger"]),
        "reduced_ledger_count": len(new["ledger"]),
        "changed_item_count": len(changed),
        "category_counts": dict(sorted(category_counts.items())),
        "items": sorted(changed, key=lambda item: item["id"]),
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# GB 9706.1 manual reduction audit",
        "",
        f"Generated at: `{report['generated_at']}`",
        "",
        "This is a read-only comparison of derived `result.json` files. Source PDFs are referenced for review and were not modified.",
        "",
        "## Scope and interpretation",
        "",
        "A changed item is a new automatic `matched` ledger entry whose source or target row overlaps at least one baseline `manual` entry. The baseline often had separate source-only and target-only entries; the reduced result binds them into one exact-row comparison.",
        "",
        "Categories are based on the changed set: `one_to_one`, `many_to_one` (several Record rows to one Report row), `one_to_many` (one Record row to several Report rows), and `numeric_ledger` (percentage/numeric ledger rule). The complete machine-readable item list is in `changed_items.json`.",
        "",
        "## Counts",
        "",
        "| Sample | Changed automatic items | 1:1 | many:1 | 1:many | Numeric ledger |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    total = collections.Counter()
    for sample_report in report["samples"]:
        counts = sample_report["category_counts"]
        total.update(counts)
        lines.append(
            f"| {sample_report['sample']} | {sample_report['changed_item_count']} | {counts.get('one_to_one', 0)} | {counts.get('many_to_one', 0)} | {counts.get('one_to_many', 0)} | {counts.get('numeric_ledger', 0)} |"
        )
    lines.append(
        f"| **Total** | **{sum(total.values())}** | **{total.get('one_to_one', 0)}** | **{total.get('many_to_one', 0)}** | **{total.get('one_to_many', 0)}** | **{total.get('numeric_ledger', 0)}** |"
    )
    lines.extend(["", "## Review queue", ""])
    lines.append("The rows below are representative entries for human spot-checking; `changed_items.json` contains every changed entry with both PDF locations and bounding boxes.")
    lines.append("")
    for sample_report in report["samples"]:
        lines.append(f"### Sample {sample_report['sample']}")
        lines.append("")
        # Prefer one representative per category and include the special split
        # aggregation row where it exists.
        reps: list[dict[str, Any]] = []
        seen: set[str] = set()
        for category in ("one_to_one", "many_to_one", "one_to_many", "numeric_ledger"):
            candidates = [item for item in sample_report["items"] if item["category"] == category]
            if candidates:
                item = candidates[0]
                reps.append(item)
                seen.add(item["id"])
        for item in sample_report["items"]:
            if item["aggregation"] == "split_report_rows_aggregated" and item["id"] not in seen:
                reps.append(item)
                seen.add(item["id"])
        lines.append("| Category | ID | Clause | Record page | Report page | Record status | Report raw | Effective result | Mapping |")
        lines.append("|---|---|---|---:|---:|---|---|---|---|")
        for item in reps:
            record_page = (item.get("record_location") or {}).get("pdf_page", "")
            report_page = (item.get("report_location") or {}).get("pdf_page", "")
            lines.append(
                f"| {item['category']} | `{item['id']}` | {item.get('clause') or ''} | {record_page} | {report_page} | {item.get('record_status') or ''} | {item.get('report_result_raw') or ''} | {item.get('report_result_effective') or ''} | {item.get('mapping_method') or ''} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Potential false-positive / review risks",
            "",
            "- A status ink that is geometrically assigned to a printed column can still be misread if the handwriting crosses columns; those cases remain manual in the result and are not in this changed set.",
            "- In `many_to_one` groups, several Record rows intentionally share one Report row. Verify that the shared Report result covers every child requirement and that the statuses are consistent.",
            "- In `one_to_many` groups, one Record status is applied to split Report rows. The effective result follows the configured rule that a conforming/non-placeholder row governs over `——` or `/`; inspect split rows where the physical result differs from the effective result.",
            "- Numeric values remain manual when dual recognition is unresolved. The only newly automatic numeric-style item in these three samples is the 1539 percentage copy (4.11, 66%); it should still be checked against the two source/report locations.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=Path("output/full-records-complete-20260930"))
    parser.add_argument("--reduced", type=Path, default=Path("output/full-records-manual-reduction-20260930-v1"))
    parser.add_argument("--output", type=Path, default=Path("output/audit-61-manual-reduction-20260930"))
    args = parser.parse_args()

    samples = [audit_sample(load_result(args.baseline, sample), load_result(args.reduced, sample), sample) for sample in SAMPLES]
    report = {
        "schema_version": "audit-61-manual-reduction-1.0",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "baseline_root": str(args.baseline.resolve()),
        "reduced_root": str(args.reduced.resolve()),
        "samples": samples,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "changed_items.json").open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    (args.output / "audit.md").write_text(markdown(report), encoding="utf-8")
    print(args.output / "audit.md")
    print(args.output / "changed_items.json")


if __name__ == "__main__":
    main()
