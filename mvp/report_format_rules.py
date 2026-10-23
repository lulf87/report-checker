"""Format checks on the formal Report result table (part of REPORT-R08).

Agreed with the user on 2026-10-23, both reported as errors:

* **单项结论未覆盖整个序号** – within one item on one page the 单项结论 column
  is split into several cells and some of them are blank, e.g. sample 0483
  item 48: “符合” fills only the first line and the merged cell under it is
  empty.
* **数值结果缺少“单位：”** – a result written as a number (not a percentage,
  not text such as “符合要求”) needs a “单位：X” in its acceptance text
  (anywhere earlier in the same item, e.g. “……取较大值；单位：mV”).  Only the
  “单位：X” form counts; a unit written after the number (“1289 个”) does not.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from mvp.record_full import scan_report_rows

_NUMERIC_RESULT = re.compile(r"^[<>≤≥＜＞+\-−±]?\s*\d")


def _rect(value: Any) -> list[float] | None:
    return [round(float(v), 2) for v in value] if value else None


def report_format_defects(report_path: str | Path) -> dict[int, list[dict[str, Any]]]:
    rows = scan_report_rows(report_path)
    defects: dict[int, list[dict[str, Any]]] = defaultdict(list)

    conclusion_cells: dict[tuple[int, int], dict[tuple[float, ...], str]] = defaultdict(dict)
    for row in rows:
        if row.conclusion_rect:
            key = tuple(round(float(v), 1) for v in row.conclusion_rect)
            cells = conclusion_cells[(row.sequence, row.pdf_page)]
            cells[key] = cells.get(key, "") or row.conclusion_raw.strip()
    for (sequence, page), cells in sorted(conclusion_cells.items()):
        filled = [rect for rect, text in cells.items() if text]
        blank = [rect for rect, text in cells.items() if not text]
        if filled and blank:
            written = cells[filled[0]]
            defects[sequence].append(
                {
                    "kind": "conclusion_cell_not_covering_item",
                    "message": (
                        f"序号{sequence}的单项结论“{written}”只占了这一项的一部分行，"
                        f"下面还有{len(blank)}个空白的单项结论单元格（合并单元格没有覆盖整个序号）"
                    ),
                    "location": {
                        "document_role": "report",
                        "pdf_page": page,
                        "rect": list(blank[0]),
                        "source": "report_conclusion_cell",
                    },
                    "written_location": {
                        "document_role": "report",
                        "pdf_page": page,
                        "rect": list(filled[0]),
                        "source": "report_conclusion_cell",
                    },
                }
            )

    for row in rows:
        result = row.result_raw.strip()
        if not result or "%" in result or "％" in result or not _NUMERIC_RESULT.match(result):
            continue
        if row.unit_context:
            continue
        requirement = (row.requirement_path or (row.requirement_raw,))[0]
        # 同一允差下的多行（如“正向”“负向”）用最末一级要求区分。
        line = row.requirement_path[-1] if len(row.requirement_path) > 1 else ""
        defects[row.sequence].append(
            {
                "kind": "numeric_result_without_unit",
                "message": (
                    f"序号{row.sequence}{f'（{line}）' if line else ''}的检验结果“{result}”是数值，"
                    f"但对应的接受标准中没有写“单位：”"
                    + (f"（接受标准：{requirement[:40]}）" if requirement else "")
                ),
                "location": {
                    "document_role": "report",
                    "pdf_page": row.pdf_page,
                    "rect": _rect(row.requirement_rect or row.result_rect),
                    "source": "report_requirement_cell",
                },
                "result_location": {
                    "document_role": "report",
                    "pdf_page": row.pdf_page,
                    "rect": _rect(row.result_rect),
                    "source": "report_item_result",
                },
            }
        )
    return dict(defects)
