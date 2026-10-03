from __future__ import annotations

import json
from pathlib import Path

from mvp.checker import STATUS_LABEL, run_1539


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    output_dir = root / "output" / "mvp-1539"
    result = run_1539(root, output_dir)
    print(
        json.dumps(
            {
                "sample": result["sample"],
                "overall_status": result["overall_status"],
                "overall_label": STATUS_LABEL[result["overall_status"]],
                "html": str(output_dir / "index.html"),
                "json": str(output_dir / "result.json"),
                "findings": [
                    {"id": item["id"], "status": item["status"], "summary": item["summary"]}
                    for item in result["findings"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
