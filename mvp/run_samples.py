from __future__ import annotations

import json
from pathlib import Path

from mvp.checker import STATUS_LABEL, run_sample, write_comparison


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    samples = ("1539", "2948", "2795", "1347")
    results = {
        sample: run_sample(root, sample, root / "output" / f"mvp-{sample}")
        for sample in samples
    }
    comparison_samples = [results[sample] for sample in ("2948", "2795", "1347")]
    comparison_path = write_comparison(comparison_samples, root / "output" / "mvp-comparison")
    print(
        json.dumps(
            {
                "samples": {
                    sample: {
                        "overall_status": result["overall_status"],
                        "overall_label": STATUS_LABEL[result["overall_status"]],
                        "html": str(root / "output" / f"mvp-{sample}" / "index.html"),
                        "json": str(root / "output" / f"mvp-{sample}" / "result.json"),
                    }
                    for sample, result in results.items()
                },
                "comparison_html": str(comparison_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
