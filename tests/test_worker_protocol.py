from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mvp.document_store import DocumentStore
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
        "status_counts": {"pass": len(REPORT_SELF_RULE_ORDER) - 1, "warning": 0, "manual": 0, "error": 1},
        "coverage": {"planned": len(REPORT_SELF_RULE_ORDER), "completed": len(REPORT_SELF_RULE_ORDER)},
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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_REPORT_DIR = PROJECT_ROOT / "素材" / "report" / "0596"
SAMPLE_PTR_DIR = PROJECT_ROOT / "素材" / "ptr" / "0596"


def _only_pdf(directory: Path) -> Path | None:
    files = sorted(directory.glob("*.pdf")) if directory.is_dir() else []
    return files[0] if files else None


class RealWorkerProcessTests(unittest.TestCase):
    """The tests above replace the child process.  These start the real one:
    the coordinator parses its standard output as one JSON document, and a
    single stray line there fails every Run with WORKER_PROTOCOL_INVALID."""

    def test_importing_the_runners_prints_nothing_on_stdout(self) -> None:
        # A bare ``import fitz`` anywhere in the runners' imports makes
        # PyMuPDF print a deprecation warning on standard output.
        completed = subprocess.run(
            [sys.executable, "-c", "import mvp.run_modes"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=True,
        )
        self.assertEqual(completed.stdout, "")

    def test_worker_keeps_stdout_clean_when_an_import_prints(self) -> None:
        # Even if a module prints while it is imported, the reply stays clean:
        # the Worker imports the runners with standard output redirected.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sitecustomize.py").write_text(
                "import builtins, sys\n"
                "_import = builtins.__import__\n"
                "def noisy(name, *args, **kwargs):\n"
                "    if name == 'mvp.run_modes' and name not in sys.modules:\n"
                "        print('warning: printed while importing')\n"
                "    return _import(name, *args, **kwargs)\n"
                "builtins.__import__ = noisy\n",
                encoding="utf-8",
            )
            job = root / "job.json"
            job.write_text("{}", encoding="utf-8")  # rejected before any runner is used
            environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root), str(PROJECT_ROOT)]))
            completed = subprocess.run(
                [sys.executable, "-m", "mvp.run_worker", "--job", str(job)],
                cwd=str(PROJECT_ROOT), capture_output=True, text=True, env=environment,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(json.loads(completed.stderr)["schema_version"], "worker-error-1.0")

    def test_worker_reports_an_unreadable_report_as_json_on_stderr(self) -> None:
        # A PDF that is not an inspection report: the real child process
        # starts, imports every runner and fails in the documented way.
        try:
            import pymupdf
        except ImportError:  # pragma: no cover - compatibility with older environments
            self.skipTest("PyMuPDF is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = pymupdf.open()
            for number in range(3):
                document.new_page().insert_text((72, 72), f"not an inspection report, page {number + 1}")
            document.save(root / "report.pdf")
            document.close()
            store = RunStore(root / "state.sqlite3")
            case = store.create_case("real worker")
            run = store.create_run(
                case_id=case["id"], mode="report_self", inputs={"report_document_id": "report-doc"}
            )
            with self.assertRaises(CoordinatorError):
                execute_persisted_run(
                    store, run["id"], report_path=root / "report.pdf", output_dir=root / "published-run"
                )
            failure = store.get_run(run["id"])["failure"]
            store.close()
        self.assertEqual(failure["code"], "WORKER_FAILED")
        self.assertEqual(json.loads(failure["message"])["schema_version"], "worker-error-1.0")

    @unittest.skipUnless(
        _only_pdf(SAMPLE_REPORT_DIR) and _only_pdf(SAMPLE_PTR_DIR), "needs the private sample 素材/…/0596"
    )
    def test_ptr_run_goes_through_the_real_worker_and_is_published(self) -> None:
        # Coordinator → child process → JSON reply → published findings.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, ptr = _only_pdf(SAMPLE_REPORT_DIR), _only_pdf(SAMPLE_PTR_DIR)
            store = RunStore(root / "state.sqlite3")
            try:
                case = store.create_case("real worker")
                documents = DocumentStore(store, root / "blobs")
                inputs = {
                    f"{role}_document_id": documents.upload_pdf(
                        case_id=case["id"], role=role, original_filename=f"{role}.pdf", content=path.read_bytes()
                    )["id"]
                    for role, path in (("report", report), ("ptr", ptr))
                }
                run = store.create_run(
                    case_id=case["id"],
                    mode="report_ptr",
                    inputs=inputs,
                    input_snapshot=documents.validate_inputs(case_id=case["id"], mode="report_ptr", inputs=inputs),
                )
                published = execute_persisted_run(
                    store, run["id"], report_path=report, ptr_path=ptr, output_dir=root / "published-run"
                )
                findings = store.list_findings(run["id"])
            finally:
                store.close()
        self.assertEqual(published["lifecycle_status"], "succeeded")
        self.assertGreater(len(findings), 0)
        self.assertEqual(len(findings), sum(published["finding_counts"].values()))


if __name__ == "__main__":
    unittest.main()
