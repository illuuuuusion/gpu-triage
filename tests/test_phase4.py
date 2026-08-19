#!/usr/bin/env python3
"""Hardware-free acceptance tests for the native Phase-4 protocol."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from driver_probe import DriverBoundCollector  # noqa: E402
from safe_triage import run_pre_driver_triage  # noqa: E402
from vram_helper import HelperProtocolError, parse_helper_jsonl, run_vram_helper  # noqa: E402
from tests.test_phase3 import BoundFixture, vulkan_device  # noqa: E402


def helper_stream(*, statuses=None, errors=None, device_lost=False) -> str:
    statuses = statuses or {
        "host_transfer": "PASS", "gpu_local_copy": "PASS",
        "compute_kat": "PASS", "vram_pattern": "FAIL" if errors else "PASS",
    }
    errors = errors or []
    events = [
        {"type": "meta", "schema": 2, "helper": "gpu-triage-vram-helper",
         "version": "1.1.0", "pattern_version": 1, "prng": "hash32-v1",
         "offset_space": "allocation_relative", "offset_unit_bytes": 1},
        {"type": "identity", "exact_match": True, "bdf": "0000:03:00.0",
         "vendor_id": 0x1002, "device_id": 0x73AF,
         "mapping_source": "VK_EXT_pci_bus_info", "name": "Fixture"},
    ]
    for error in errors:
        events.append({
            "type": "error", "experiment": "vram_pattern", "allocation": 2, "offset": 1048576,
            "width_bits": 32, "expected": "0xaaaaaaaa", "actual": "0xaaa8aaaa",
            "xor": "0x00020000", "bits_0_to_1": [], "bits_1_to_0": [17],
            "pattern": "alternating_aa", "seed": 1234, "pass": 3,
            "reread": error, "timestamp_ms": 8421, "temp_mC": 68125,
        })
    for name, status in statuses.items():
        events.append({"type": "experiment", "name": name, "status": status,
                       "comparisons": 4096, "errors": len(errors) if name == "vram_pattern" else 0})
    events.append({
        "type": "summary", "status": "INCONCLUSIVE" if device_lost else
            ("FAIL" if "FAIL" in statuses.values() else "PASS"),
        "device_lost": device_lost, "experiments": statuses,
        "limits": {"seconds": 5, "bytes": 1048576, "max_error_records": 8,
                   "max_vram_percent": 25},
        "temperature": {"status": "PASS", "maximum_mC": 68125},
        "error_summary": {
            "total": len(errors), "recorded": len(errors),
            "first_offset": 1048576 if errors else None,
            "last_offset": 1048576 if errors else None,
            "xor_bit_histogram": {"17": len(errors)} if errors else {},
            "bits_0_to_1": {}, "bits_1_to_0": {"17": len(errors)} if errors else {},
            "clusters_64b": 1 if errors else 0, "stride_candidate_bytes": 0,
            "reproducible": {"reread": 1 if len(errors) > 1 else 0,
                             "pass": 0, "allocation": 0},
        },
    })
    return "\n".join(json.dumps(item, separators=(",", ":")) for item in events) + "\n"


class Phase4Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.fixture = BoundFixture(self.root)
        (self.fixture.proc / "cmdline").write_text("quiet\n")
        self.device = self.fixture.add_gpu(driver="amdgpu")
        self.fixture.vulkan_output = "Device Properties and Extensions:\n" + vulkan_device()
        hwmon = self.device / "hwmon/hwmon0"
        hwmon.mkdir(parents=True)
        (hwmon / "name").write_text("amdgpu\n")
        (hwmon / "temp1_input").write_text("61000\n")

    def target(self):
        return self.fixture.collector().target("0000:03:00.0")


class TestProtocol(Phase4Case):
    def test_structured_error_record_and_aggregations_survive(self):
        parsed = parse_helper_jsonl(helper_stream(errors=[0, 1]), target=self.target(), max_error_records=8)
        self.assertEqual(parsed["status"], "FAIL")
        self.assertEqual(parsed["error_records"][0]["bits_1_to_0"], [17])
        self.assertEqual(parsed["error_summary"]["reproducible"]["reread"], 1)

    def test_identity_mismatch_fails_closed(self):
        text = helper_stream().replace('"bdf":"0000:03:00.0"', '"bdf":"0001:03:00.0"')
        with self.assertRaisesRegex(HelperProtocolError, "exact target"):
            parse_helper_jsonl(text, target=self.target(), max_error_records=8)

    def test_record_limit_is_enforced_independently_of_helper_exit(self):
        executable = self.root / "helper"
        executable.write_text("fixture")
        executable.chmod(0o755)

        def command(_cmd, _timeout):
            return {"rc": 0, "output": helper_stream(errors=[0, 1]), "seconds": 0.1}

        result = run_vram_helper(
            self.target(), seconds=5, max_bytes=1024 * 1024,
            max_error_records=1, max_vram_percent=25, max_temp_mc=95000,
            log_path=self.root / "out.jsonl", run_command=command,
            executable=str(executable),
        )
        self.assertEqual(result["status"], "INCONCLUSIVE")
        self.assertIn("error-record limit", result["reason"])

    def test_device_lost_is_not_a_vram_fail(self):
        statuses = {name: "INCONCLUSIVE" for name in (
            "host_transfer", "gpu_local_copy", "compute_kat", "vram_pattern"
        )}
        parsed = parse_helper_jsonl(
            helper_stream(statuses=statuses, device_lost=True),
            target=self.target(), max_error_records=8,
        )
        self.assertTrue(parsed["device_lost"])
        self.assertEqual(parsed["status"], "INCONCLUSIVE")


class TestIndependentMatrix(Phase4Case):
    def triage_with(self, statuses):
        def helper(_target, **kwargs):
            kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["log_path"].write_text(helper_stream(statuses=statuses))
            return parse_helper_jsonl(helper_stream(statuses=statuses), target=self.target(), max_error_records=256)

        driver = DriverBoundCollector(
            self.fixture.collector().roots, self.fixture.command,
            which=lambda name: f"/usr/bin/{name}", helper_runner=helper,
        )
        return run_pre_driver_triage(
            gpu_arg="0000:03:00.0", report_dir_arg=str(self.root / "reports"),
            repo_root=self.root, collector=self.fixture.collector(),
            driver_collector=driver, vram_seconds=5,
        )

    def test_all_independent_experiments_can_reach_full_pass(self):
        statuses = {name: "PASS" for name in (
            "host_transfer", "gpu_local_copy", "compute_kat", "vram_pattern"
        )}
        report, json_path, markdown_path = self.triage_with(statuses)
        self.assertEqual(report.overall.value, "PASS")
        self.assertEqual(report.stage.value, "COMPLETE_PASS")
        self.assertEqual(report.matrix["transfer_path"].status.value, "PASS")
        self.assertEqual(report.matrix["gpu_local_copy"].status.value, "PASS")
        self.assertEqual(report.matrix["compute"].status.value, "PASS")
        self.assertEqual(report.matrix["vram_correctness"].status.value, "PASS")
        self.assertIn("vram", report.sidecars)
        self.assertTrue((json_path.parent / report.sidecars["vram"]).is_file())
        self.assertIn("GPU-local copy", markdown_path.read_text())

    def test_one_experiment_fails_without_collapsing_other_rows(self):
        statuses = {"host_transfer": "PASS", "gpu_local_copy": "PASS",
                    "compute_kat": "FAIL", "vram_pattern": "PASS"}
        report, _, _ = self.triage_with(statuses)
        self.assertEqual(report.overall.value, "FAIL")
        self.assertEqual(report.matrix["compute"].status.value, "FAIL")
        self.assertEqual(report.matrix["transfer_path"].status.value, "PASS")
        self.assertEqual(report.matrix["vram_correctness"].status.value, "PASS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
