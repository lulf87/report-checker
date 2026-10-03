"""独立 PDF Worker 进程入口。Worker 只调用现有 runner，不写 SQLite。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Sequence

from mvp.run_modes import run_mode
from mvp.worker_protocol import JobSpec, WorkerProtocolError, worker_result_payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute one Report checker JobSpec in a child process.")
    parser.add_argument("--job", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = json.loads(args.job.read_text(encoding="utf-8"))
        job = JobSpec.from_dict(payload)
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_mode(
                mode=job.mode,
                report_path=job.report_path,
                record_path=job.record_path,
                output_dir=job.output_dir,
            )
        print(json.dumps(worker_result_payload(job, result), ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, json.JSONDecodeError, WorkerProtocolError, ValueError, RuntimeError) as exc:
        print(
            json.dumps(
                {"schema_version": "worker-error-1.0", "code": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
