"""PTR ↔ Report 标准答案（gold）：起草、复核页、导入复核结果、对答案。

标准答案文件放在 ``素材/gold/<编号>.json``（git 忽略，含客户资料派生内容）。
复核页生成在 ``output/gold-review/index.html``（git 忽略，只在本机打开）。

用法::

    .venv/bin/python -m mvp.ptr_gold run                 # 跑全部样本，缓存到 output/gold-review/run.json
    .venv/bin/python -m mvp.ptr_gold draft               # 用缓存结果起草/补充 gold（不覆盖已确认条目）
    .venv/bin/python -m mvp.ptr_gold review-page         # 生成复核页
    .venv/bin/python -m mvp.ptr_gold merge 导出文件.json   # 导入复核页导出的结果
    .venv/bin/python -m mvp.ptr_gold claude 判断.jsonl     # 写入 Claude 的逐条判断（不覆盖人工确认）
    .venv/bin/python -m mvp.ptr_gold check [--run 文件]   # 当前结果对答案
    .venv/bin/python -m mvp.ptr_gold diff [提交]          # 改规则后的回归：某个提交（默认 HEAD）↔ 当前代码

``expected`` 取值与 ledger 的 ``decision`` 相同：matched / mismatch / manual / excluded。
``status``：``draft``（工具结果预填、未复核）、``claude``（Claude 逐条判断，带把握程度与理由，
未经人工确认）、``confirmed``（人工确认）。``claude`` 与 ``confirmed`` 都算答案，对答案时分开统计；
``draft`` 条目只用于发现结果漂移。人工确认永远优先，Claude 的判断不会覆盖它。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
MATERIAL = ROOT / "素材"
GOLD_DIR = MATERIAL / "gold"
REVIEW_DIR = ROOT / "output" / "gold-review"
RUN_CACHE = REVIEW_DIR / "run.json"
SCHEMA = "ptr-gold/1"
SAMPLES = "0596 0775 0778 0780 1539 2170 2795 2948 3940 4788 5332 5780 5782".split()
DECISIONS = ("matched", "mismatch", "manual", "excluded")
LABELS = {"matched": "一致", "mismatch": "不一致", "manual": "待确认", "excluded": "排除"}
RISK_LABELS = {
    "A": "工具判不一致",
    "B": "工具判待确认",
    "C": "按规则判一致（非原文相同）",
    "D": "原文相同判一致",
    "E": "排除",
}
EXACT_UNIT_REASONS = {"EXACT_UNIT_MATCH", "NO_CONTRADICTORY_UNIT_LABEL", None}


# ---------------------------------------------------------------------------
# Running the comparison
# ---------------------------------------------------------------------------

def _sample_paths(sample: str) -> tuple[Path, Path]:
    report = next((MATERIAL / "report" / sample).glob("*.pdf"))
    ptr = next((MATERIAL / "ptr" / sample).glob("*.pdf"))
    return report, ptr


def _run_one(sample: str) -> tuple[str, dict[str, Any]]:
    os.chdir(ROOT)
    from mvp.ptr_compare import compare_ptr_report

    report, ptr = _sample_paths(sample)
    result = compare_ptr_report(report, ptr, comparison_mode="rules", ocr_mode="auto")
    return sample, {
        "rule_version": (result.get("rule") or {}).get("version"),
        "inputs": {
            "report_sha256": result["inputs"]["report"]["source_sha256"],
            "ptr_sha256": result["inputs"]["ptr"]["source_sha256"],
        },
        "coverage_ledger": result["coverage_ledger"],
    }


def run_samples(samples: Sequence[str] = SAMPLES, workers: int = 6) -> dict[str, Any]:
    with ProcessPoolExecutor(workers) as pool:
        results = dict(pool.map(_run_one, samples))
    return {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "git_head": _git_head(),
        "samples": results,
    }


def _git_head() -> str:
    import subprocess

    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "mvp"], cwd=ROOT, capture_output=True,
                               text=True, check=True).stdout.strip()
        return head + ("+工作区改动" if dirty else "")
    except Exception:  # pragma: no cover - git unavailable
        return "unknown"


def load_run(path: Path | None = None) -> dict[str, Any]:
    data = json.loads(Path(path or RUN_CACHE).read_text(encoding="utf-8"))
    if "samples" not in data:  # plain {sample: ledger} dump
        data = {"generated_at": None, "git_head": None,
                "samples": {s: {"coverage_ledger": led, "inputs": {}} for s, led in data.items()}}
    return data


# ---------------------------------------------------------------------------
# Keys and risk
# ---------------------------------------------------------------------------

def keyed_entries(ledger: Iterable[Mapping[str, Any]]) -> list[tuple[str, Mapping[str, Any]]]:
    """``(key, entry)``; the key is the clause number, made unique when repeated."""

    entries = list(ledger)
    counts = Counter(str(e.get("clause_id")) for e in entries)
    seen: Counter[str] = Counter()
    keyed = []
    for entry in entries:
        clause = str(entry.get("clause_id"))
        seen[clause] += 1
        keyed.append((clause if counts[clause] == 1 else f"{clause}#{seen[clause]}", entry))
    return keyed


def risk_of(entry: Mapping[str, Any]) -> str:
    decision = entry.get("decision")
    if decision == "mismatch":
        return "A"
    if decision == "manual":
        return "B"
    if decision == "excluded":
        return "E"
    text = entry.get("text_comparison") or {}
    table = entry.get("table_comparison") or {}
    unit = entry.get("unit_comparison") or {}
    exact = (
        entry.get("reason_code") == "EXACT_NORMALIZED_MATCH"
        and not text.get("equivalence_rules")
        and not table.get("reason_code")
        and unit.get("reason_code") in EXACT_UNIT_REASONS
    )
    return "D" if exact else "C"


def _tool_summary(entry: Mapping[str, Any], rule_version: str | None) -> dict[str, Any]:
    return {
        "decision": entry.get("decision"),
        "reason_code": entry.get("reason_code"),
        "rule_version": rule_version,
    }


# ---------------------------------------------------------------------------
# Gold files
# ---------------------------------------------------------------------------

def gold_path(sample: str) -> Path:
    return GOLD_DIR / f"{sample}.json"


def load_gold(sample: str) -> dict[str, Any] | None:
    path = gold_path(sample)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_gold(gold: Mapping[str, Any]) -> None:
    GOLD_DIR.mkdir(parents=True, exist_ok=True)
    path = gold_path(str(gold["sample"]))
    path.write_text(json.dumps(gold, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def draft(run: Mapping[str, Any]) -> dict[str, Counter]:
    """Create or extend gold files from a run; confirmed entries are never changed."""

    stats: dict[str, Counter] = {}
    today = _dt.date.today().isoformat()
    for sample, payload in run["samples"].items():
        gold = load_gold(sample) or {
            "schema": SCHEMA,
            "sample": sample,
            "inputs": payload.get("inputs") or {},
            "known_issues": [],
            "entries": [],
        }
        by_key = {e["key"]: e for e in gold["entries"]}
        counter: Counter = Counter()
        rule_version = payload.get("rule_version")
        for key, entry in keyed_entries(payload["coverage_ledger"]):
            current = by_key.get(key)
            tool = _tool_summary(entry, rule_version)
            if current is None:
                gold["entries"].append({
                    "key": key,
                    "clause_id": str(entry.get("clause_id")),
                    "expected": entry.get("decision"),
                    "acceptable": [],
                    "status": "draft",
                    "issue": "",
                    "note": "",
                    "risk": risk_of(entry),
                    "tool_at_draft": tool,
                    "drafted_at": today,
                    "reviewed_at": None,
                })
                counter["added"] += 1
            elif current.get("status") == "draft":
                current["risk"] = risk_of(entry)
                counter["kept_draft"] += 1
            else:
                counter["kept_confirmed"] += 1
        if payload.get("inputs"):
            gold["inputs"] = payload["inputs"]
        save_gold(gold)
        stats[sample] = counter
    return stats


def merge(export: Mapping[str, Any]) -> Counter:
    """Apply review verdicts exported by the review page."""

    counter: Counter = Counter()
    by_sample: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in export.get("verdicts") or []:
        by_sample[str(item["sample"])].append(item)
    for sample, items in by_sample.items():
        gold = load_gold(sample)
        if gold is None:
            raise SystemExit(f"{sample}: 没有 gold 文件，先运行 draft")
        entries = {e["key"]: e for e in gold["entries"]}
        for item in items:
            entry = entries.get(str(item["key"]))
            if entry is None:
                counter["unknown_key"] += 1
                print(f"  跳过：{sample} {item['key']} 不在 gold 中", file=sys.stderr)
                continue
            expected = item.get("expected")
            if expected not in DECISIONS:
                counter["invalid"] += 1
                continue
            entry["expected"] = expected
            entry["acceptable"] = [d for d in item.get("acceptable") or [] if d in DECISIONS and d != expected]
            entry["issue"] = str(item.get("issue") or "")
            entry["note"] = str(item.get("note") or "")
            entry["status"] = "confirmed"
            entry["reviewed_at"] = item.get("reviewed_at") or _dt.date.today().isoformat()
            counter["confirmed"] += 1
        if "known_issues" in (export.get("samples") or {}).get(sample, {}):
            gold["known_issues"] = list(export["samples"][sample]["known_issues"])
        save_gold(gold)
    return counter


def apply_claude(verdicts: Iterable[Mapping[str, Any]]) -> Counter:
    """Record Claude's per-entry judgement; human-confirmed entries are kept."""

    counter: Counter = Counter()
    by_sample: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in verdicts:
        by_sample[str(item["sample"])].append(item)
    today = _dt.date.today().isoformat()
    for sample, items in by_sample.items():
        gold = load_gold(sample)
        if gold is None:
            raise SystemExit(f"{sample}: 没有 gold 文件，先运行 draft")
        entries = {e["key"]: e for e in gold["entries"]}
        for item in items:
            entry = entries.get(str(item["key"]))
            if entry is None:
                counter["unknown_key"] += 1
                print(f"  跳过：{sample} {item['key']} 不在 gold 中", file=sys.stderr)
                continue
            if item.get("expected") not in DECISIONS or item.get("confidence") not in {"high", "low"}:
                raise SystemExit(f"{sample} {item['key']}: expected/confidence 无效")
            entry["claude"] = {
                "expected": item["expected"],
                "acceptable": [d for d in item.get("acceptable") or [] if d in DECISIONS and d != item["expected"]],
                "confidence": item["confidence"],
                "reason": str(item.get("reason") or ""),
                "issue": str(item.get("issue") or ""),
                "judged_at": today,
            }
            if entry.get("status") == "confirmed":
                counter["kept_confirmed"] += 1
                continue
            entry["status"] = "claude"
            entry["expected"] = item["expected"]
            entry["acceptable"] = entry["claude"]["acceptable"]
            entry["issue"] = entry["claude"]["issue"]
            counter["claude"] += 1
        save_gold(gold)
    return counter


# ---------------------------------------------------------------------------
# Checking a run against the gold
# ---------------------------------------------------------------------------

CHECK_CATEGORIES = {
    "missed": "漏报：答案为不一致，工具判一致/排除",
    "missed_to_manual": "真问题只判待确认：答案为不一致，工具判待确认",
    "false_alarm": "误报：答案为一致，工具判不一致",
    "wrongly_passed": "不该放行：答案为待确认，工具判一致",
    "extra_manual": "多余待确认：答案为一致，工具判待确认",
    "scope": "范围不同：排除与否和答案不一致",
    "other": "其他不符",
    "missing_in_tool": "答案中有、工具结果中没有",
    "missing_in_gold": "工具结果中有、答案中没有（需要 draft 补充）",
    "draft_drift": "未复核条目：结果与起草时不同",
}


def _category(expected: str, actual: str) -> str:
    if expected == "mismatch":
        return "missed_to_manual" if actual == "manual" else "missed"
    if expected == "matched":
        return {"mismatch": "false_alarm", "manual": "extra_manual"}.get(actual, "scope")
    if expected == "manual":
        return "wrongly_passed" if actual == "matched" else ("scope" if "excluded" in (expected, actual) else "other")
    return "scope"


def check(run: Mapping[str, Any], samples: Sequence[str] | None = None) -> dict[str, Any]:
    findings: dict[str, list[dict[str, Any]]] = defaultdict(list)
    totals: Counter = Counter()
    for sample, payload in run["samples"].items():
        if samples and sample not in samples:
            continue
        gold = load_gold(sample)
        if gold is None:
            findings["missing_in_gold"].append({"sample": sample, "key": "*", "detail": "没有 gold 文件"})
            continue
        actual = {key: entry for key, entry in keyed_entries(payload["coverage_ledger"])}
        for g in gold["entries"]:
            key = g["key"]
            entry = actual.pop(key, None)
            source = g.get("status")
            confirmed = source in {"confirmed", "claude"}
            if entry is None:
                findings["missing_in_tool"].append({"sample": sample, "key": key, "expected": g["expected"]})
                continue
            decision = entry.get("decision")
            item = {
                "sample": sample, "key": key, "expected": g["expected"], "actual": decision,
                "reason_code": entry.get("reason_code"), "issue": g.get("issue") or "",
                "source": source,
            }
            if confirmed:
                totals[source] += 1
                if decision == g["expected"] or decision in (g.get("acceptable") or []):
                    totals[f"{source}_correct"] += 1
                    continue
                findings[_category(str(g["expected"]), str(decision))].append(item)
            else:
                totals["draft"] += 1
                before = (g.get("tool_at_draft") or {}).get("decision")
                if decision != before:
                    item["expected"] = before
                    findings["draft_drift"].append(item)
        for key, entry in actual.items():
            findings["missing_in_gold"].append({"sample": sample, "key": key, "actual": entry.get("decision")})
    return {"totals": dict(totals), "findings": {k: findings[k] for k in CHECK_CATEGORIES if findings.get(k)}}


def print_check(result: Mapping[str, Any]) -> None:
    totals = result["totals"]
    print(
        f"人工确认 {totals.get('confirmed', 0)} 条（相符 {totals.get('confirmed_correct', 0)}）；"
        f"Claude 已判 {totals.get('claude', 0)} 条（相符 {totals.get('claude_correct', 0)}）；"
        f"未复核 {totals.get('draft', 0)} 条"
    )
    if not result["findings"]:
        print("全部相符。")
    for name, items in result["findings"].items():
        print(f"\n## {CHECK_CATEGORIES[name]}（{len(items)}）")
        for item in items:
            expected = LABELS.get(str(item.get("expected")), item.get("expected", ""))
            actual = LABELS.get(str(item.get("actual")), item.get("actual", ""))
            issue = f" ｜ {item['issue']}" if item.get("issue") else ""
            reason = f"（{item['reason_code']}）" if item.get("reason_code") else ""
            tag = "[Claude] " if item.get("source") == "claude" else ""
            print(f"  {tag}{item['sample']} {item['key']}: 答案 {expected} → 工具 {actual}{reason}{issue}")


# ---------------------------------------------------------------------------
# Regression: an earlier commit ↔ the current code
# ---------------------------------------------------------------------------

# Runs inside a checkout of the baseline commit.  It must not rely on this
# module (older commits do not have it) nor on anything but
# ``compare_ptr_report``.
_BASELINE_SCRIPT = r"""
import json, os, sys
from concurrent.futures import ProcessPoolExecutor


def one(job):
    import mvp
    from mvp.ptr_compare import compare_ptr_report

    if not os.path.realpath(mvp.__file__).startswith(os.path.realpath(os.getcwd()) + os.sep):
        raise SystemExit("基线没有使用基线提交里的代码：" + mvp.__file__)
    sample, report, ptr = job
    result = compare_ptr_report(report, ptr, comparison_mode="rules", ocr_mode="auto")
    return sample, {
        "rule_version": (result.get("rule") or {}).get("version"),
        "inputs": {},
        "coverage_ledger": result["coverage_ledger"],
    }


if __name__ == "__main__":
    jobs, out, workers = json.loads(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    with ProcessPoolExecutor(workers) as pool:
        results = dict(pool.map(one, jobs))
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False, default=str)
"""


def run_samples_at(revision: str, samples: Sequence[str] = SAMPLES, workers: int = 6) -> dict[str, Any]:
    """Run the samples with the code of ``revision`` in a temporary worktree.

    The worktree is removed afterwards.  The sample PDFs are passed by their
    real paths, and the table/OCR cache of this checkout is shared, so a
    baseline whose PDFs are already cached takes seconds.
    """

    import subprocess
    import tempfile

    from mvp import table_cache

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()

    commit = git("rev-parse", "--verify", f"{revision}^{{commit}}")
    jobs = [[sample, *(str(path) for path in _sample_paths(sample))] for sample in samples]
    environment = dict(os.environ)
    cache = table_cache.cache_directory()
    if cache is not None:
        environment[table_cache.ENVIRONMENT_VARIABLE] = str(cache.resolve())
    with tempfile.TemporaryDirectory(prefix="ptr-gold-baseline-") as temporary:
        tree = Path(temporary) / "tree"
        script = Path(temporary) / "run_baseline.py"
        out = Path(temporary) / "baseline.json"
        script.write_text(_BASELINE_SCRIPT, encoding="utf-8")
        git("worktree", "add", "--detach", str(tree), commit)
        try:
            environment["PYTHONPATH"] = str(tree)
            done = subprocess.run(
                [sys.executable, str(script), json.dumps(jobs, ensure_ascii=False), str(out), str(workers)],
                cwd=tree, env=environment, capture_output=True, text=True,
            )
            if done.returncode != 0:
                raise RuntimeError(f"基线 {revision} 运行失败：\n{done.stderr[-4000:]}")
            results = json.loads(out.read_text(encoding="utf-8"))
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT, capture_output=True)
            subprocess.run(["git", "worktree", "prune"], cwd=ROOT, capture_output=True)
    return {"generated_at": None, "git_head": git("rev-parse", "--short", commit), "samples": results}


DIFF_CATEGORIES = {
    "regression": "变差，需要处理：原“一致”不再一致，或原“不一致”消失（且答案不支持这个变化）",
    "explained": "原“一致”不再一致 / 原“不一致”消失，但与标准答案相符（提交说明里逐条写原因）",
    "other": "其他判定变化",
    "added": "新增条目（基线里没有）",
    "removed": "消失的条目（当前结果里没有）",
    "reason_only": "判定不变、原因码变了",
}


def _gold_answers(sample: str) -> dict[str, Mapping[str, Any]]:
    gold = load_gold(sample)
    if gold is None:
        return {}
    return {str(g["key"]): g for g in gold["entries"] if g.get("status") in {"confirmed", "claude"}}


def diff_runs(base: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """Compare every ledger entry's decision between two runs.

    An entry that was 一致 and is no longer, or was 不一致 and is no longer,
    is a regression unless the reviewed answer (gold) says the new decision is
    right.  Entries are matched by sample and clause key.
    """

    findings: dict[str, list[dict[str, Any]]] = defaultdict(list)
    totals: Counter = Counter()
    for sample in sorted(set(base["samples"]) | set(current["samples"])):
        if sample not in base["samples"] or sample not in current["samples"]:
            findings["regression"].append({
                "sample": sample, "key": "*", "before": None, "after": None,
                "note": "只有一边跑了这个样本",
            })
            continue
        before = dict(keyed_entries(base["samples"][sample]["coverage_ledger"]))
        after = dict(keyed_entries(current["samples"][sample]["coverage_ledger"]))
        answers = _gold_answers(sample)
        for key in list(before) + [key for key in after if key not in before]:
            old, new = before.get(key), after.get(key)
            answer = answers.get(key)
            item = {
                "sample": sample, "key": key,
                "before": old.get("decision") if old else None,
                "after": new.get("decision") if new else None,
                "before_reason": old.get("reason_code") if old else None,
                "after_reason": new.get("reason_code") if new else None,
                "expected": answer.get("expected") if answer else None,
                "answer_source": answer.get("status") if answer else None,
            }
            totals["entries"] += 1
            if item["before"] == item["after"]:
                if item["before_reason"] != item["after_reason"]:
                    findings["reason_only"].append(item)
                else:
                    totals["unchanged"] += 1
                continue
            agrees = bool(answer) and new is not None and (
                item["after"] == answer["expected"] or item["after"] in (answer.get("acceptable") or [])
            )
            item["agrees_with_answer"] = agrees if answer else None
            if item["before"] in {"matched", "mismatch"}:
                findings["explained" if agrees else "regression"].append(item)
            elif old is None:
                findings["added"].append(item)
            elif new is None:
                findings["removed"].append(item)
            else:
                findings["other"].append(item)
    return {
        "base": base.get("git_head"), "current": current.get("git_head"),
        "totals": dict(totals),
        "findings": {name: findings[name] for name in DIFF_CATEGORIES if findings.get(name)},
    }


def print_diff(result: Mapping[str, Any]) -> None:
    totals = result["totals"]
    changed = sum(len(items) for name, items in result["findings"].items() if name != "reason_only")
    print(f"回归对比：基线 {result['base']} → 当前 {result['current']}")
    print(f"共 {totals.get('entries', 0)} 条，判定变化 {changed} 条，"
          f"变差 {len(result['findings'].get('regression', []))} 条。")
    if not changed:
        print("判定 0 变化。")
    for name, items in result["findings"].items():
        print(f"\n## {DIFF_CATEGORIES[name]}（{len(items)}）")
        for item in items:
            if item.get("note"):
                print(f"  {item['sample']}: {item['note']}")
                continue
            before = LABELS.get(str(item["before"]), "无")
            after = LABELS.get(str(item["after"]), "无")
            reasons = f"（{item['before_reason'] or '—'} → {item['after_reason'] or '—'}）"
            answer = ""
            if item.get("expected"):
                source = "Claude" if item.get("answer_source") == "claude" else "人工"
                answer = f" ｜ 答案（{source}）：{LABELS.get(str(item['expected']), item['expected'])}"
            elif name != "reason_only":
                answer = " ｜ 无答案"
            print(f"  {item['sample']} {item['key']}: {before} → {after}{reasons}{answer}")


# ---------------------------------------------------------------------------
# Review page
# ---------------------------------------------------------------------------

def _clip(value: Any, limit: int = 6000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def _review_item(sample: str, key: str, entry: Mapping[str, Any], gold_entry: Mapping[str, Any] | None) -> dict[str, Any]:
    text = entry.get("text_comparison") or {}
    table = dict(entry.get("table_comparison") or {})
    expansion = table.pop("row_expansion", None) or {}
    pages: dict[str, list[int]] = defaultdict(list)
    for ev in entry.get("evidence") or []:
        page = ev.get("pdf_page")
        if page is not None and page not in pages[str(ev.get("role"))]:
            pages[str(ev.get("role"))].append(page)
    diff = text.get("text_diff") or {}
    return {
        "sample": sample,
        "key": key,
        "clause": str(entry.get("clause_id")),
        "risk": risk_of(entry),
        "decision": entry.get("decision"),
        "reason": entry.get("reason_code"),
        "scope": entry.get("scope_reason_code"),
        "ptr": _clip(text.get("ptr_source")),
        "report": _clip(text.get("report_source")),
        "ptr_seg": diff.get("ptr_segments") or [],
        "report_seg": diff.get("report_segments") or [],
        "rules": [r.get("rule") for r in text.get("equivalence_rules") or []],
        "numeric": entry.get("numeric_comparison") or {},
        "unit": {k: (entry.get("unit_comparison") or {}).get(k) for k in ("status", "ptr", "report", "reason_code")},
        "reference": {k: (entry.get("reference_comparison") or {}).get(k) for k in ("status", "binding_status", "binding_reason_code", "ptr", "report")},
        "table": table,
        "cells": [
            {k: c.get(k) for k in ("column", "ptr", "report", "status", "reason_code")}
            for c in expansion.get("cells") or []
        ],
        "shift": (entry.get("clause_shift") or {}).get("message") if isinstance(entry.get("clause_shift"), Mapping) else None,
        "mapping": {k: (entry.get("mapping") or {}).get(k) for k in ("report_clause_ids", "resolution")},
        "pages": dict(pages),
        "gold": {
            k: gold_entry.get(k) for k in ("expected", "acceptable", "status", "issue", "note", "reviewed_at", "claude")
        } if gold_entry else None,
        "spot": False,
    }


# Spot checks go where Claude made judgement calls (A/B/C), fewer where a
# script already verified the whole class (D: identical text, E: out of scope).
SPOT_CHECK_QUOTA = {"A": 5, "B": 8, "C": 5, "D": 3, "E": 3}


def _mark_spot_checks(items: list[dict[str, Any]]) -> None:
    """Pick a fixed random sample of Claude's confident judgements per risk class."""

    import random

    pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        claude = (item.get("gold") or {}).get("claude") or {}
        if claude.get("confidence") == "high" and (item.get("gold") or {}).get("status") == "claude":
            pool[item["risk"]].append(item)
    rng = random.Random(20261108)
    for risk, members in sorted(pool.items()):
        for item in rng.sample(members, min(SPOT_CHECK_QUOTA.get(risk, 2), len(members))):
            item["spot"] = True


def build_review_page(run: Mapping[str, Any], out: Path | None = None) -> Path:
    items = []
    known: dict[str, list[str]] = {}
    for sample, payload in run["samples"].items():
        gold = load_gold(sample) or {"entries": [], "known_issues": []}
        known[sample] = list(gold.get("known_issues") or [])
        by_key = {e["key"]: e for e in gold["entries"]}
        for key, entry in keyed_entries(payload["coverage_ledger"]):
            items.append(_review_item(sample, key, entry, by_key.get(key)))
    _mark_spot_checks(items)
    data = {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "run": {"generated_at": run.get("generated_at"), "git_head": run.get("git_head")},
        "labels": LABELS,
        "risk_labels": RISK_LABELS,
        "known_issues": known,
        "items": items,
    }
    template = (Path(__file__).with_name("ptr_gold_review.html")).read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = template.replace("/*__GOLD_DATA__*/null", payload)
    out = Path(out or REVIEW_DIR / "index.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mvp.ptr_gold", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="跑全部样本并缓存结果")
    p_run.add_argument("--out", type=Path, default=RUN_CACHE)
    p_run.add_argument("--samples", nargs="*", default=SAMPLES)
    for name, help_text in (("draft", "起草/补充 gold"), ("review-page", "生成复核页")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--run", type=Path, default=RUN_CACHE)
    p_merge = sub.add_parser("merge", help="导入复核页导出的 JSON")
    p_merge.add_argument("export", type=Path)
    p_claude = sub.add_parser("claude", help="写入 Claude 的逐条判断（JSON Lines）")
    p_claude.add_argument("verdicts", type=Path)
    p_check = sub.add_parser("check", help="对答案")
    p_check.add_argument("--run", type=Path, help="已有的结果缓存；不给则现跑")
    p_check.add_argument("--samples", nargs="*")
    p_check.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_diff = sub.add_parser("diff", help="回归：某个提交与当前代码逐条比较判定")
    p_diff.add_argument("base", nargs="?", default="HEAD", help="基线提交（默认 HEAD，即比较未提交的改动）")
    p_diff.add_argument("--samples", nargs="*", default=SAMPLES)
    p_diff.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    if args.command == "run":
        result = run_samples(args.samples)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"已写入 {args.out}")
    elif args.command == "draft":
        for sample, counter in draft(load_run(args.run)).items():
            print(sample, dict(counter))
    elif args.command == "review-page":
        print(f"已生成 {build_review_page(load_run(args.run))}")
    elif args.command == "merge":
        print(dict(merge(json.loads(args.export.read_text(encoding="utf-8")))))
    elif args.command == "claude":
        lines = args.verdicts.read_text(encoding="utf-8").splitlines()
        print(dict(apply_claude(json.loads(line) for line in lines if line.strip())))
    elif args.command == "check":
        run = load_run(args.run) if args.run else run_samples(args.samples or SAMPLES)
        result = check(run, args.samples)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_check(result)
        return 1 if any(result["findings"].get(k) for k in ("missed", "wrongly_passed")) else 0
    elif args.command == "diff":
        result = diff_runs(run_samples_at(args.base, args.samples), run_samples(args.samples))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_diff(result)
        return 1 if result["findings"].get("regression") else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
