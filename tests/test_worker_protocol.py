from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mvp.run_coordinator import CoordinatorError, execute_persisted_run
from mvp.checker import REPORT_SELF_RULE_ORDER
from mvp.run_store import RunStore
from mvp.worker_protocol import (
    JOB_SCHEMA_VERSION,
    JobSpec,
    WorkerProtocolError,
    worker_result_payload,
)


def _report_self_result() -> dict:
    findings = [
        {"id": rule_id, "rule_id": rule_id, "status": "error" if rule_id == "REPORT-R07" else "pass"}
        for rule_id in REPORT_SELF_RULE_ORDER
    ]
    return {
        "schema_version": "report-self-1.0",
        "mode": "report_self",
        "lifecycle_status": "succeeded",
        "machine_overall_status": "error",
        "status_counts": {"pass": 11, "warning": 0, "manual": 0, "error": 1},
        "coverage": {"planned": 12, "completed": 12},
        "findings": findings,
    }


class WorkerProtocolTests(unittest.TestCase):
    def test_job_spec_is_strict_about_schema_paths_and_mode_roles(self) -> None:
        with self.assertRaises(WorkerProtocolError):
            JobSpec.from_dict({"schema_version": "wrong"})
        with self.assertRaises(WorkerProtocolError):
            JobSpec(
                run_id="run",
                mode="report_self",
                report_path=Path("relative.pdf"),
                output_dir=Path("/tmp/out"),
            )
        with self.assertRaises(WorkerProtocolError):
            JobSpec(
                run_id="run",
                mode="report_ptr",
                report_path=Path("/tmp/report.pdf"),
                output_dir=Path("/tmp/out"),
                record_path=Path("/tmp/ptr.pdf"),
            )

    def test_coordinator_publishes_only_validated_child_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RunStore(root / "state.sqlite3")
            case = store.create_case("worker test")
            run = store.create_run(
                case_id=case["id"], mode="report_self", inputs={"report_document_id": "report-doc"}
            )

            def fake_worker(command, **kwargs):
                job = JobSpec.from_dict(json.loads(Path(command[-1]).read_text(encoding="utf-8")))
                job.output_dir.mkdir(parents=True, exist_ok=True)
                result = _report_self_result()
                (job.output_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(worker_result_payload(job, result)),
                    stderr="",
                )

            with patch("mvp.run_coordinator.subprocess.run", side_effect=fake_worker):
                published = execute_persisted_run(
                    store,
                    run["id"],
                    report_path=root / "report.pdf",
                    output_dir=root / "published-run",
                )
            self.assertEqual(published["lifecycle_status"], "succeeded")
            self.assertEqual(published["finding_counts"]["error"], 1)
            self.assertTrue(all(item["execution_state"] == "succeeded" for item in store.get_rule_executions(run["id"])))
            store.close()

    def test_worker_nonzero_exit_fails_run_and_does_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RunStore(root / "state.sqlite3")
            case = store.create_case("worker failure")
            run = store.create_run(
                case_id=case["id"], mode="report_self", inputs={"report_document_id": "report-doc"}
            )
            with patch(
                "mvp.run_coordinator.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="crashed"),
            ):
                with self.assertRaises(CoordinatorError):
                    execute_persisted_run(
                        store,
                        run["id"],
                        report_path=root / "report.pdf",
                        output_dir=root / "failed-run",
                    )
            failed = store.get_run(run["id"])
            self.assertEqual(failed["lifecycle_status"], "failed")
            self.assertIsNone(failed["finding_counts"])
            self.assertTrue(all(item["execution_state"] == "failed" for item in store.get_rule_executions(run["id"])))
            store.close()

    def test_worker_artifact_manifest_is_atomic_and_hashes_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RunStore(root / "state.sqlite3")
            case = store.create_case("artifact manifest")
            run = store.create_run(
                case_id=case["id"], mode="report_self", inputs={"report_document_id": "report-doc"}
            )

            def fake_worker(command, **kwargs):
                job = JobSpec.from_dict(json.loads(Path(command[-1]).read_text(encoding="utf-8")))
                job.output_dir.mkdir(parents=True, exist_ok=True)
                evidence = job.output_dir / "evidence.png"
                evidence.write_bytes(b"png-bytes")
                result = _report_self_result()
                result["findings"][0]["evidence"] = [{"image": "evidence.png"}]
                (job.output_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(worker_result_payload(job, result)),
                    stderr="",
                )

            with patch("mvp.run_coordinator.subprocess.run", side_effect=fake_worker):
                execute_persisted_run(
                    store,
                    run["id"],
                    report_path=root / "report.pdf",
                    output_dir=root / "published-run",
                )
            manifest = json.loads((root / "published-run/artifact-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "worker-artifact-manifest-1.0")
            self.assertEqual(manifest["artifacts"][0]["path"], "evidence.png")
            self.assertEqual(manifest["artifacts"][0]["size_bytes"], len(b"png-bytes"))
            store.close()

    def test_missing_worker_artifact_fails_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RunStore(root / "state.sqlite3")
            case = store.create_case("missing artifact")
            run = store.create_run(
                case_id=case["id"], mode="report_self", inputs={"report_document_id": "report-doc"}
            )

            def fake_worker(command, **kwargs):
                job = JobSpec.from_dict(json.loads(Path(command[-1]).read_text(encoding="utf-8")))
                job.output_dir.mkdir(parents=True, exist_ok=True)
                result = _report_self_result()
                result["findings"][0]["evidence"] = [{"image": "missing.png"}]
                (job.output_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(worker_result_payload(job, result)),
                    stderr="",
                )

            with patch("mvp.run_coordinator.subprocess.run", side_effect=fake_worker):
                with self.assertRaises(CoordinatorError):
                    execute_persisted_run(
                        store,
                        run["id"],
                        report_path=root / "report.pdf",
                        output_dir=root / "failed-artifact",
                    )
            self.assertEqual(store.get_run(run["id"])["lifecycle_status"], "failed")
            self.assertFalse((root / "failed-artifact/artifact-manifest.json").exists())
            store.close()


if __name__ == "__main__":
    unittest.main()
