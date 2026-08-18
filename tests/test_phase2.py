#!/usr/bin/env python3
"""Hardware-free acceptance tests for crash-tolerant Phase-2 reporting."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import reporting  # noqa: E402
from safe_triage import SafeTriageError, run_pre_driver_triage  # noqa: E402
from tests.test_phase1 import Fixture  # noqa: E402


class Phase2Fixture(unittest.TestCase):
    def make_run(self):
        root = Path(tempfile.mkdtemp())
        fixture = Fixture(root)
        fixture.add_gpu()
        report_dir = root / "reports"
        collector = fixture.collector()
        return root, fixture, report_dir, collector

    @staticmethod
    def triage(root: Path, report_dir: Path, collector):
        return run_pre_driver_triage(
            gpu_arg="0000:03:00.0",
            report_dir_arg=str(report_dir),
            repo_root=root,
            collector=collector,
        )


class TestCompactReport(Phase2Fixture):
    def test_report_budget_evidence_layers_and_raw_separation(self):
        root, _fixture, report_dir, collector = self.make_run()
        report, json_path, markdown_path = self.triage(root, report_dir, collector)
        text = markdown_path.read_text()
        lines = text.splitlines()

        self.assertGreaterEqual(len(lines), 50)
        self.assertLessEqual(len(lines), 120)
        for heading in (
            "## Key Measurements",
            "## Observations",
            "## Interpretation",
            "## Hypotheses",
        ):
            self.assertIn(heading, text)
        self.assertIn("## Result Matrix", text)
        self.assertNotIn("kernel: fixture boot complete", text)
        self.assertNotIn("VGA compatible controller [0300]", text)
        self.assertEqual(json.loads(json_path.read_text())["overall"], "INCOMPLETE")
        self.assertEqual(report.overall.value, "INCOMPLETE")

    def test_primary_and_runtime_mirror_hold_same_final_checkpoint(self):
        root, _fixture, report_dir, collector = self.make_run()
        report, json_path, markdown_path = self.triage(root, report_dir, collector)
        mirror = root / "run/gpu-triage"
        mirror_json = mirror / json_path.name
        mirror_markdown = mirror / markdown_path.name

        self.assertTrue(mirror_json.is_file())
        self.assertTrue(mirror_markdown.is_file())
        self.assertEqual(json.loads(json_path.read_text()), json.loads(mirror_json.read_text()))
        self.assertEqual(markdown_path.read_text(), mirror_markdown.read_text())
        self.assertFalse(report.persistence["persistence_lost"])


class TestStageCheckpoints(Phase2Fixture):
    def test_stage0_failure_leaves_parseable_incomplete_checkpoint(self):
        root, _fixture, report_dir, collector = self.make_run()
        with mock.patch.object(collector, "target", side_effect=OSError("fixture stage0 failure")):
            with self.assertRaisesRegex(SafeTriageError, "stage0 failure"):
                self.triage(root, report_dir, collector)

        checkpoints = list(report_dir.glob("*.json"))
        self.assertEqual(len(checkpoints), 1)
        parsed = json.loads(checkpoints[0].read_text())
        self.assertEqual(parsed["stage"], "ABORTED")
        self.assertEqual(parsed["overall"], "INCOMPLETE")
        self.assertEqual(parsed["stage_history"], ["START", "S0_ENVIRONMENT", "ABORTED"])

    def test_stage1_failure_leaves_parseable_incomplete_checkpoint(self):
        root, _fixture, report_dir, collector = self.make_run()
        with mock.patch.object(
            collector, "target_measurements", side_effect=OSError("fixture stage1 failure")
        ):
            with self.assertRaisesRegex(SafeTriageError, "stage1 failure"):
                self.triage(root, report_dir, collector)

        checkpoint = next(report_dir.glob("*.json"))
        parsed = json.loads(checkpoint.read_text())
        self.assertEqual(parsed["stage"], "ABORTED")
        self.assertEqual(parsed["overall"], "INCOMPLETE")
        self.assertEqual(
            parsed["stage_history"],
            ["START", "S0_ENVIRONMENT", "S1_PRE_DRIVER", "ABORTED"],
        )

    def test_stage_boundaries_use_fsync(self):
        root, _fixture, report_dir, collector = self.make_run()
        with mock.patch("reporting.os.fsync", wraps=reporting.os.fsync) as fsync:
            self.triage(root, report_dir, collector)
        # Initial directory probe plus three checkpoints in two destinations.
        self.assertGreaterEqual(fsync.call_count, 10)


class TestMediaFailureFallback(Phase2Fixture):
    def test_read_only_primary_keeps_old_files_valid_and_finishes_in_runtime_mirror(self):
        root, _fixture, report_dir, collector = self.make_run()
        real_atomic_write = reporting.atomic_write
        primary_calls = 0

        def fail_final_primary(path, content, *, durable=False):
            nonlocal primary_calls
            if path.parent == report_dir and path.suffix in {".json", ".md"}:
                primary_calls += 1
                if primary_calls >= 5:
                    raise OSError("fixture medium became read-only")
            return real_atomic_write(path, content, durable=durable)

        with mock.patch("reporting.atomic_write", side_effect=fail_final_primary):
            report, json_path, markdown_path = self.triage(root, report_dir, collector)

        self.assertEqual(json_path.parent, root / "run/gpu-triage")
        self.assertEqual(markdown_path.parent, root / "run/gpu-triage")
        self.assertTrue(report.persistence["persistence_lost"])
        self.assertIn("volatile runtime mirror", " ".join(report.persistence["warnings"]))
        runtime_json = json.loads(json_path.read_text())
        self.assertTrue(runtime_json["persistence"]["persistence_lost"])
        self.assertIn("Persistent-medium loss: YES", markdown_path.read_text())

        # The last pre-failure primary checkpoint remains valid rather than
        # becoming a partially overwritten JSON/Markdown pair.
        primary_json = next(report_dir.glob("*.json"))
        primary_markdown = next(report_dir.glob("*.md"))
        self.assertEqual(json.loads(primary_json.read_text())["overall"], "INCOMPLETE")
        self.assertIn("# GPU-TRIAGE REPORT", primary_markdown.read_text())


class TestBoundedSidecars(unittest.TestCase):
    def test_utf8_truncation_respects_byte_limit(self):
        bounded = reporting._bounded_bytes("€" * 100, 80, "unicode")
        self.assertIsInstance(bounded, str)
        self.assertLessEqual(len(bounded.encode("utf-8")), 80)
        self.assertIn("truncated at", bounded)

    def test_raw_sidecars_are_truncated_with_visible_markers(self):
        root = Path(tempfile.mkdtemp())
        primary = root / "primary"
        mirror = root / "mirror"
        primary.mkdir()
        writer = reporting.CheckpointWriter(primary, mirror, "bounded")
        large_pci = "P" * (reporting.MAX_PCI_SIDECAR_BYTES + 1024)
        large_kernel = "K" * (reporting.MAX_KERNEL_SIDECAR_BYTES + 1024)
        large_pstore = "S" * (reporting.MAX_PSTORE_ENTRY_BYTES + 1024)

        sidecars = reporting.write_sidecars(
            writer,
            {"overview": {"cmd": ["lspci", "-Dnnk"], "rc": 0, "output": large_pci}},
            {"output": large_kernel},
            {"entries": [{"name": "dmesg-test", "content": large_pstore}]},
        )

        for name in (sidecars["lspci"], sidecars["kernel"]):
            content = (primary / name).read_text()
            self.assertIn("truncated at", content)
        pstore_content = (primary / sidecars["pstore"] / "dmesg-test").read_text()
        self.assertIn("truncated at", pstore_content)
        self.assertLessEqual((primary / sidecars["lspci"]).stat().st_size, reporting.MAX_PCI_SIDECAR_BYTES)
        self.assertLessEqual((primary / sidecars["kernel"]).stat().st_size, reporting.MAX_KERNEL_SIDECAR_BYTES)
        self.assertLessEqual(
            (primary / sidecars["pstore"] / "dmesg-test").stat().st_size,
            reporting.MAX_PSTORE_ENTRY_BYTES,
        )

    def test_driver_kernel_window_preserves_both_large_snapshots(self):
        root = Path(tempfile.mkdtemp())
        primary = root / "primary"
        primary.mkdir()
        writer = reporting.CheckpointWriter(primary, root / "mirror", "window")
        sidecars = {"kernel": "window-kernel.log"}
        reporting.write_driver_sidecars(
            writer,
            sidecars,
            kernel_before={"output": "A" * reporting.MAX_KERNEL_SIDECAR_BYTES},
            kernel_after={"output": "B" * reporting.MAX_KERNEL_SIDECAR_BYTES},
        )
        content = (primary / "window-kernel.log").read_text()
        self.assertIn("### BEFORE DRIVER-BOUND STAGE", content)
        self.assertIn("### AFTER DRIVER-BOUND STAGE", content)
        self.assertIn("BBBB", content)
        self.assertLessEqual(
            (primary / "window-kernel.log").stat().st_size,
            reporting.MAX_KERNEL_SIDECAR_BYTES,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
