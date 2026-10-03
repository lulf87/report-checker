from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote


MODE_SPECS: dict[str, dict[str, str]] = {
    "report-record-9706-1": {
        "result_mode": "report_record_9706_1",
        "label": "Report + GB 9706.1 Record",
    },
    "report-record-9706-202": {
        "result_mode": "report_record_9706_202",
        "label": "Report + GB 9706.202 Record",
    },
}

# These are the Report/Record pairs for which source material exists in this project.
DEFAULT_EXPECTED_RUNS: tuple[tuple[str, str], ...] = (
    ("1347", "report-record-9706-1"),
    ("1539", "report-record-9706-1"),
    ("1539", "report-record-9706-202"),
    ("2795", "report-record-9706-202"),
    ("2948", "report-record-9706-1"),
    ("2948", "report-record-9706-202"),
)

CATEGORY_SPECS: tuple[tuple[str, str, str | None, str | None], ...] = (
    ("automatic_match", "自动一致", "pass", "matched"),
    ("confirmed_mismatch", "确定不一致", "error", "mismatch"),
    ("warning", "警示", "warning", None),
    ("manual_review", "待人工复核", "manual", "manual"),
    ("not_applicable", "不适用", None, "not_applicable"),
    ("excluded", "排除", None, "excluded"),
)

FINDING_STATUSES = frozenset(
    finding_status
    for _, _, finding_status, _ in CATEGORY_SPECS
    if finding_status is not None
)
LEDGER_DISPOSITIONS = frozenset(
    ledger_disposition
    for _, _, _, ledger_disposition in CATEGORY_SPECS
    if ledger_disposition is not None
)
MODE_ORDER = {mode: index for index, mode in enumerate(MODE_SPECS)}
RUN_STATUS_LABELS = {
    "pass": "通过",
    "warning": "警示",
    "manual": "待人工复核",
    "error": "确定不一致",
    "missing": "结果缺失",
    "invalid": "结果无效",
}


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _safe_relative_href(sample: str, mode: str) -> str:
    return "/".join(quote(part, safe="-._~") for part in (sample, mode, "index.html"))


def _validate_expected_run(sample: str, mode: str) -> tuple[str, str]:
    if not sample or sample in {".", ".."} or "/" in sample or "\\" in sample:
        raise ValueError(f"invalid sample name: {sample!r}")
    if mode not in MODE_SPECS:
        raise ValueError(f"unsupported overview mode: {mode!r}")
    return sample, mode


def _normalise_expected_runs(
    expected_runs: Sequence[tuple[str, str]],
) -> list[tuple[str, str]]:
    normalised: set[tuple[str, str]] = set()
    for sample, mode in expected_runs:
        normalised.add(_validate_expected_run(str(sample), str(mode)))
    return sorted(normalised, key=lambda item: (item[0], MODE_ORDER[item[1]]))


def _discover_runs(root: Path) -> set[tuple[str, str]]:
    discovered: set[tuple[str, str]] = set()
    if not root.is_dir():
        return discovered
    for result_path in root.glob("*/*/result.json"):
        sample = result_path.parent.parent.name
        mode = result_path.parent.name
        if mode not in MODE_SPECS:
            continue
        try:
            discovered.add(_validate_expected_run(sample, mode))
        except ValueError:
            continue
    return discovered


def _empty_categories(value: int | None = None) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "label": label,
            "finding_count": value,
            "ledger_count": value,
        }
        for key, label, _, _ in CATEGORY_SPECS
    }


def _empty_coverage() -> dict[str, dict[str, Any]]:
    return {
        "source_rows": {
            "label": "Record",
            "eligible": None,
            "accounted": None,
            "conserved": None,
        },
        "report_rows": {
            "label": "Report",
            "eligible": None,
            "accounted": None,
            "conserved": None,
        },
    }


def _base_run(root: Path, sample: str, mode: str) -> dict[str, Any]:
    mode_root = root / sample / mode
    result_path = mode_root / "result.json"
    detail_path = mode_root / "index.html"
    return {
        "sample": sample,
        "mode": mode,
        "result_mode": MODE_SPECS[mode]["result_mode"],
        "mode_label": MODE_SPECS[mode]["label"],
        "result_path": str(result_path.relative_to(root)),
        "detail_href": _safe_relative_href(sample, mode),
        "detail_available": detail_path.is_file(),
        "run_state": "missing",
        "state_message": "result.json 缺失",
        "machine_overall_status": "missing",
        "machine_overall_label": RUN_STATUS_LABELS["missing"],
        "reported_machine_overall_status": None,
        "status_consistent": None,
        "coverage_complete": False,
        "is_pass": False,
        "categories": _empty_categories(),
        "coverage": _empty_coverage(),
    }


def _require_list(payload: Mapping[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _count_categories(
    findings: Sequence[Any],
    ledger: Sequence[Any],
) -> dict[str, dict[str, Any]]:
    ledger_dispositions: list[str] = []
    ledger_by_id: dict[str, str] = {}
    for index, entry in enumerate(ledger):
        if not isinstance(entry, Mapping):
            raise ValueError(f"ledger[{index}] must be an object")
        disposition = entry.get("disposition")
        if disposition not in LEDGER_DISPOSITIONS:
            raise ValueError(
                f"ledger[{index}].disposition is invalid: {disposition!r}"
            )
        ledger_dispositions.append(str(disposition))
        entry_id = entry.get("entry_id") or entry.get("id")
        if isinstance(entry_id, str) and entry_id:
            ledger_by_id[entry_id] = str(disposition)

    finding_categories: list[str] = []
    status_categories = {
        "pass": "automatic_match",
        "error": "confirmed_mismatch",
        "warning": "warning",
        "manual": "manual_review",
    }
    for index, finding in enumerate(findings):
        if not isinstance(finding, Mapping):
            raise ValueError(f"findings[{index}] must be an object")
        status = finding.get("status")
        if status not in FINDING_STATUSES:
            raise ValueError(f"findings[{index}].status is invalid: {status!r}")
        category = status_categories[str(status)]
        if status == "pass":
            details = finding.get("details")
            details = details if isinstance(details, Mapping) else {}
            linked_id = details.get("ledger_entry_id") or finding.get("id")
            disposition = details.get("disposition")
            if disposition is None and isinstance(linked_id, str):
                disposition = ledger_by_id.get(linked_id)
            if disposition == "not_applicable":
                category = "not_applicable"
            elif disposition == "excluded":
                category = "excluded"
        finding_categories.append(category)

    return {
        key: {
            "label": label,
            "finding_count": finding_categories.count(key),
            "ledger_count": (
                ledger_dispositions.count(ledger_disposition)
                if ledger_disposition is not None
                else 0
            ),
        }
        for key, label, finding_status, ledger_disposition in CATEGORY_SPECS
    }


def _nonnegative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _coverage_summary(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    coverage = payload.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ValueError("coverage must be an object")
    summary: dict[str, dict[str, Any]] = {}
    for key, label in (("source_rows", "Record"), ("report_rows", "Report")):
        side = coverage.get(key)
        if not isinstance(side, Mapping):
            raise ValueError(f"coverage.{key} must be an object")
        conserved = side.get("conserved")
        if not isinstance(conserved, bool):
            raise ValueError(f"coverage.{key}.conserved must be a boolean")
        summary[key] = {
            "label": label,
            "eligible": _nonnegative_int(
                side.get("eligible"), f"coverage.{key}.eligible"
            ),
            "accounted": _nonnegative_int(
                side.get("accounted"), f"coverage.{key}.accounted"
            ),
            "conserved": conserved,
        }
    return summary


def _machine_status(categories: Mapping[str, Mapping[str, Any]]) -> str:
    if any(categories["confirmed_mismatch"][key] for key in ("finding_count", "ledger_count")):
        return "error"
    if any(categories["manual_review"][key] for key in ("finding_count", "ledger_count")):
        return "manual"
    if any(categories["warning"][key] for key in ("finding_count", "ledger_count")):
        return "warning"
    return "pass"


def _coverage_is_complete(coverage: Mapping[str, Mapping[str, Any]]) -> bool:
    source = coverage["source_rows"]
    report = coverage["report_rows"]
    return bool(
        (source["eligible"] or report["eligible"])
        and source["conserved"] is True
        and report["conserved"] is True
        and source["accounted"] == source["eligible"]
        and report["accounted"] == report["eligible"]
    )


def _load_run(root: Path, sample: str, mode: str) -> dict[str, Any]:
    run = _base_run(root, sample, mode)
    result_path = root / run["result_path"]
    if not result_path.is_file():
        return run

    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("result root must be an object")
        expected_result_mode = MODE_SPECS[mode]["result_mode"]
        if payload.get("mode") != expected_result_mode:
            raise ValueError(
                f"mode must be {expected_result_mode!r}, got {payload.get('mode')!r}"
            )
        findings = _require_list(payload, "findings")
        ledger = _require_list(payload, "ledger")
        categories = _count_categories(findings, ledger)
        coverage = _coverage_summary(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        run.update(
            {
                "run_state": "invalid",
                "state_message": str(exc),
                "machine_overall_status": "invalid",
                "machine_overall_label": RUN_STATUS_LABELS["invalid"],
            }
        )
        return run

    machine_status = _machine_status(categories)
    reported_status = payload.get("machine_overall_status")
    status_consistent = reported_status in {None, machine_status}
    coverage_complete = _coverage_is_complete(coverage)
    detail_available = bool(run["detail_available"])
    state_messages: list[str] = []
    if not detail_available:
        state_messages.append("index.html 缺失")
    if not coverage_complete:
        state_messages.append("coverage 未完整守恒")
    if not status_consistent:
        state_messages.append("机器状态与明细计数不一致")
    run_state = "complete" if not state_messages else "incomplete"
    run.update(
        {
            "run_state": run_state,
            "state_message": "结果与明细页完整" if not state_messages else "；".join(state_messages),
            "machine_overall_status": machine_status,
            "machine_overall_label": RUN_STATUS_LABELS[machine_status],
            "reported_machine_overall_status": reported_status,
            "status_consistent": status_consistent,
            "coverage_complete": coverage_complete,
            "is_pass": bool(
                run_state == "complete"
                and coverage_complete
                and status_consistent
                and machine_status == "pass"
            ),
            "categories": categories,
            "coverage": coverage,
        }
    )
    return run


def _category_totals(runs: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    totals = _empty_categories(0)
    for run in runs:
        if run["run_state"] in {"missing", "invalid"}:
            continue
        for key in totals:
            totals[key]["finding_count"] += run["categories"][key]["finding_count"]
            totals[key]["ledger_count"] += run["categories"][key]["ledger_count"]
    return totals


def _run_status_counts(runs: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in RUN_STATUS_LABELS}
    for run in runs:
        status = str(run["machine_overall_status"])
        counts[status] += 1
    return counts


def build_overview(
    root: str | Path,
    *,
    expected_runs: Sequence[tuple[str, str]] = DEFAULT_EXPECTED_RUNS,
    include_discovered: bool = True,
    generated_at: str | None = None,
) -> dict[str, Any]:
    output_root = Path(root).expanduser().resolve()
    expected = set(_normalise_expected_runs(expected_runs))
    if include_discovered:
        expected.update(_discover_runs(output_root))
    ordered = _normalise_expected_runs(tuple(expected))
    runs = [_load_run(output_root, sample, mode) for sample, mode in ordered]

    state_counts = {
        state: sum(run["run_state"] == state for run in runs)
        for state in ("complete", "incomplete", "missing", "invalid")
    }
    passed = sum(bool(run["is_pass"]) for run in runs)
    coverage_incomplete = sum(
        run["run_state"] not in {"missing", "invalid"}
        and not run["coverage_complete"]
        for run in runs
    )
    detail_missing = sum(
        run["run_state"] not in {"missing", "invalid"}
        and not run["detail_available"]
        for run in runs
    )
    overview = {
        "schema_version": "record-overview-1.0",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "root": str(output_root),
        "overall_state": (
            "complete"
            if state_counts["missing"] == 0
            and state_counts["invalid"] == 0
            and state_counts["incomplete"] == 0
            else "incomplete"
        ),
        "totals": {
            "expected_runs": len(runs),
            "complete_runs": state_counts["complete"],
            "incomplete_runs": state_counts["incomplete"],
            "missing_runs": state_counts["missing"],
            "invalid_runs": state_counts["invalid"],
            "passed_runs": passed,
            "non_passed_runs": len(runs) - passed,
            "coverage_incomplete_runs": coverage_incomplete,
            "detail_missing_runs": detail_missing,
        },
        "run_status_counts": _run_status_counts(runs),
        "category_totals": _category_totals(runs),
        "runs": runs,
    }
    return overview


def _display_count(value: Any) -> str:
    return "—" if value is None else str(value)


def _coverage_text(side: Mapping[str, Any]) -> str:
    eligible = side.get("eligible")
    accounted = side.get("accounted")
    if eligible is None or accounted is None:
        return "—"
    conserved = "守恒" if side.get("conserved") is True else "未守恒"
    return f"{eligible} / {accounted}（{conserved}）"


def _markdown_escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def render_summary_markdown(summary: Mapping[str, Any]) -> str:
    totals = summary["totals"]
    lines = [
        "# Report + GB 9706.1 / 9706.202 全量运行总览",
        "",
        f"生成时间：{summary['generated_at']}",
        "",
        (
            f"预期运行 {totals['expected_runs']}；完整 {totals['complete_runs']}；"
            f"不完整 {totals['incomplete_runs']}；缺失 {totals['missing_runs']}；"
            f"无效 {totals['invalid_runs']}；通过 {totals['passed_runs']}；"
            f"未通过（含缺失/无效）{totals['non_passed_runs']}。"
        ),
        "",
        "计数格式为 `Finding / Ledger`；Coverage 格式为 `eligible / accounted`。",
        "",
        "| 样本 | 模式 | 运行状态 | 自动一致 | 确定不一致 | 警示 | 待人工复核 | 不适用 | 排除 | Record Coverage | Report Coverage | 明细 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for run in summary["runs"]:
        category_cells = []
        for key, _, _, _ in CATEGORY_SPECS:
            item = run["categories"][key]
            category_cells.append(
                f"{_display_count(item['finding_count'])} / {_display_count(item['ledger_count'])}"
            )
        if run["detail_available"]:
            detail = f"[打开 index.html]({run['detail_href']})"
        elif run["run_state"] == "missing":
            detail = "结果缺失"
        else:
            detail = "index.html 缺失"
        state = run["machine_overall_label"]
        if run["run_state"] == "incomplete":
            state = f"{state}；{run['state_message']}"
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_escape(run["sample"]),
                    _markdown_escape(run["mode_label"]),
                    _markdown_escape(state),
                    *category_cells,
                    _coverage_text(run["coverage"]["source_rows"]),
                    _coverage_text(run["coverage"]["report_rows"]),
                    detail,
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "缺失或无效的运行不计为通过；结果存在但明细页、Coverage 或机器状态不完整时同样不计为通过。",
            "",
        ]
    )
    return "\n".join(lines)


def _html_count_pair(category: Mapping[str, Any]) -> str:
    finding = html.escape(_display_count(category["finding_count"]))
    ledger = html.escape(_display_count(category["ledger_count"]))
    return (
        f'<span class="count"><b>Finding</b> {finding}</span>'
        f'<span class="count"><b>Ledger</b> {ledger}</span>'
    )


def render_summary_html(summary: Mapping[str, Any]) -> str:
    totals = summary["totals"]
    rows: list[str] = []
    for run in summary["runs"]:
        categories = "".join(
            f"<td>{_html_count_pair(run['categories'][key])}</td>"
            for key, _, _, _ in CATEGORY_SPECS
        )
        if run["detail_available"]:
            detail = (
                f'<a href="{html.escape(str(run["detail_href"]), quote=True)}">'
                "打开 index.html</a>"
            )
        elif run["run_state"] == "missing":
            detail = '<span class="missing-text">结果缺失</span>'
        else:
            detail = '<span class="missing-text">index.html 缺失</span>'
        status_class = html.escape(str(run["machine_overall_status"]))
        state_message = html.escape(str(run["state_message"]))
        rows.append(
            f'<tr class="{status_class}">'
            f"<td><strong>{html.escape(str(run['sample']))}</strong></td>"
            f"<td>{html.escape(str(run['mode_label']))}</td>"
            f'<td><span class="badge {status_class}">{html.escape(str(run["machine_overall_label"]))}</span>'
            f'<small>{state_message}</small></td>'
            f"{categories}"
            f"<td>{html.escape(_coverage_text(run['coverage']['source_rows']))}</td>"
            f"<td>{html.escape(_coverage_text(run['coverage']['report_rows']))}</td>"
            f"<td>{detail}</td></tr>"
        )

    category_headers = "".join(
        f"<th>{html.escape(label)}<small>Finding / Ledger</small></th>"
        for _, label, _, _ in CATEGORY_SPECS
    )
    overall_label = "运行结果齐全" if summary["overall_state"] == "complete" else "运行结果不完整"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Report + GB 9706.1 / 9706.202 全量运行总览</title>
  <style>
    :root{{--ink:#17202a;--muted:#667085;--line:#dfe3e8;--bg:#f5f7fa;--pass:#027a48;--warning:#b54708;--error:#b42318;--manual:#9a6700;--missing:#475467}}
    *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}}
    main{{width:min(1800px,calc(100% - 36px));margin:28px auto 60px}} header{{padding:24px;border-radius:16px;background:#132238;color:#fff}}
    h1{{margin:4px 0 8px;font-size:28px}} header p{{margin:0;color:#d0d5dd}} .summary{{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:10px;margin:16px 0}}
    .metric{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px}} .metric strong{{display:block;font-size:24px}} .metric span,small{{display:block;color:var(--muted);margin-top:4px}}
    section{{background:#fff;border:1px solid var(--line);border-radius:14px;overflow:auto}} table{{width:100%;min-width:1500px;border-collapse:collapse;font-size:13px}}
    th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}} th{{position:sticky;top:0;background:#f9fafb;white-space:nowrap}} th small{{font-weight:400}}
    tr.error td{{background:#fff7f6}} tr.manual td{{background:#fffbeb}} tr.warning td{{background:#fffaf0}} tr.missing td,tr.invalid td{{background:#f2f4f7}}
    .count{{display:block;white-space:nowrap}} .badge{{display:inline-block;padding:3px 8px;border-radius:999px;background:#eef2f6;font-weight:700}}
    .badge.pass{{color:var(--pass);background:#ecfdf3}} .badge.error{{color:var(--error);background:#fef3f2}} .badge.manual,.badge.warning{{color:var(--warning);background:#fffaeb}}
    .badge.missing,.badge.invalid{{color:var(--missing);background:#eaecf0}} a{{color:#175cd3}} .missing-text{{color:var(--error);font-weight:700}}
    @media(max-width:900px){{.summary{{grid-template-columns:repeat(2,1fr)}}}}
  </style>
</head>
<body><main>
  <header><small>FULL RECORD COMPARISON OVERVIEW</small><h1>Report + GB 9706.1 / 9706.202 全量运行总览</h1>
    <p>{html.escape(overall_label)}。缺失或无效结果不计为通过；计数按 Finding 与 Coverage Ledger 分列。</p></header>
  <div class="summary">
    <div class="metric"><strong>{totals['expected_runs']}</strong><span>预期运行</span></div>
    <div class="metric"><strong>{totals['complete_runs']}</strong><span>完整结果</span></div>
    <div class="metric"><strong>{totals['missing_runs']}</strong><span>结果缺失</span></div>
    <div class="metric"><strong>{totals['invalid_runs']}</strong><span>结果无效</span></div>
    <div class="metric"><strong>{totals['passed_runs']}</strong><span>通过</span></div>
    <div class="metric"><strong>{totals['non_passed_runs']}</strong><span>未通过（含缺失/无效）</span></div>
  </div>
  <section><table><thead><tr><th>样本</th><th>模式</th><th>运行状态</th>{category_headers}<th>Record Coverage<small>eligible / accounted</small></th><th>Report Coverage<small>eligible / accounted</small></th><th>明细</th></tr></thead>
    <tbody>{''.join(rows)}</tbody></table></section>
</main></body></html>"""


def generate_overview(
    root: str | Path,
    *,
    expected_runs: Sequence[tuple[str, str]] = DEFAULT_EXPECTED_RUNS,
    include_discovered: bool = True,
    generated_at: str | None = None,
) -> dict[str, Any]:
    output_root = Path(root).expanduser().resolve()
    summary = build_overview(
        output_root,
        expected_runs=expected_runs,
        include_discovered=include_discovered,
        generated_at=generated_at,
    )
    _write_atomic(
        output_root / "summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
    )
    _write_atomic(output_root / "summary.md", render_summary_markdown(summary))
    _write_atomic(output_root / "index.html", render_summary_html(summary))
    return summary


def _parse_expected(value: str) -> tuple[str, str]:
    try:
        sample, mode = value.split(":", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected format is SAMPLE:MODE") from exc
    try:
        return _validate_expected_run(sample, mode)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate JSON, Markdown, and HTML overviews for full Record runs."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("output/full-records-complete-20260930"),
        help="root containing <sample>/<mode>/result.json",
    )
    parser.add_argument(
        "--expect",
        action="append",
        type=_parse_expected,
        metavar="SAMPLE:MODE",
        help="replace the default expected-run matrix; may be repeated",
    )
    parser.add_argument(
        "--no-discover",
        action="store_true",
        help="do not add valid result paths found outside the expected matrix",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = generate_overview(
        args.root,
        expected_runs=args.expect or DEFAULT_EXPECTED_RUNS,
        include_discovered=not args.no_discover,
    )
    print(
        json.dumps(
            {
                "overall_state": summary["overall_state"],
                "totals": summary["totals"],
                "summary_json": str(args.root.resolve() / "summary.json"),
                "summary_markdown": str(args.root.resolve() / "summary.md"),
                "html": str(args.root.resolve() / "index.html"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
