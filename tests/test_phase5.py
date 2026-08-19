#!/usr/bin/env python3
"""Hardware-free acceptance tests for Phase-5 ASIC inference."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from asic_inference import (  # noqa: E402
    ProfileValidationError,
    infer_channel_lane,
    validate_profile,
)
from driver_probe import DriverBoundCollector  # noqa: E402
from safe_triage import run_pre_driver_triage  # noqa: E402
from tests.test_phase3 import BoundFixture, vulkan_device  # noqa: E402
from tests.test_phase4 import helper_stream  # noqa: E402
from vram_helper import HelperProtocolError, parse_helper_jsonl  # noqa: E402


def synthetic_profile() -> dict:
    return {
        "schema": 1,
        "profile_id": "test-navi21-revc0",
        "mapping_version": "1.2.0",
        "confidence": "MEDIUM",
        "asic": {
            "vendor_id": "0x1002",
            "device_id": "0x73af",
            "revision_ids": ["0xc0"],
        },
        "input": {
            "address_space": "allocation_relative",
            "offset_unit_bytes": 1,
            "word_width_bits": 32,
            "helper_schema": 2,
            "helper_version": "1.1.0",
            "pattern_version": 1,
            "experiments": ["vram_pattern"],
            "definition": "Synthetic test-only byte offset from the start of one Vulkan allocation.",
        },
        "sources": [{
            "id": "fixture-lab-1",
            "kind": "documented_experiment",
            "title": "Synthetic known-fault fixture; not production evidence",
            "locator": "tests/test_phase5.py",
        }],
        "mapping": {
            "channel": {
                "source": "input_offset",
                "source_ids": ["fixture-lab-1"],
                "bit_groups": [[20]],
                "values": {"0": "CH-A", "1": "CH-B"},
            },
            "lane": {
                "source": "xor_bit_index",
                "source_ids": ["fixture-lab-1"],
                "values": {str(bit): f"DQ{bit}" for bit in range(32)},
            },
        },
        "known_fault_validation": {
            "source_ids": ["fixture-lab-1"],
            "cases": [{
                "id": "injected-ch-b-dq17",
                "source_ids": ["fixture-lab-1"],
                "experiment": "vram_pattern",
                "offset": 1048576,
                "xor_bits": [17],
                "expected_channels": ["CH-B"],
                "expected_lanes": ["DQ17"],
            }],
        },
    }


class TestProfileGate(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.profiles = self.root / "profiles"
        self.profiles.mkdir()
        fixture = BoundFixture(self.root / "sys-fixture")
        fixture.add_gpu(driver="amdgpu")
        self.target = fixture.collector().target("0000:03:00.0")
        self.evidence = parse_helper_jsonl(
            helper_stream(errors=[0, 1]), target=self.target, max_error_records=8
        )

    def write_profile(self, profile: dict, name: str = "profile.json") -> None:
        (self.profiles / name).write_text(json.dumps(profile), encoding="utf-8")

    def infer(self):
        return infer_channel_lane(
            self.profiles,
            vendor_id=self.target.vendor_id,
            device_id=self.target.device_id,
            revision=self.target.revision,
            evidence=self.evidence,
        )

    def test_no_profile_is_literal_unknown(self):
        result = self.infer()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["channel"], "UNKNOWN")
        self.assertEqual(result["lane"], "UNKNOWN")
        self.assertEqual(result["reason"], "NO_VALIDATED_ASIC_PROFILE")

    def test_validated_profile_maps_bounded_hypothesis(self):
        self.write_profile(synthetic_profile())
        result = self.infer()
        self.assertEqual(result["status"], "HYPOTHESIS")
        self.assertEqual(result["channel"], "CH-B")
        self.assertEqual(result["lane"], "DQ17")
        self.assertEqual(result["mapping_version"], "1.2.0")
        self.assertEqual(result["mapping_confidence"], "MEDIUM")
        self.assertEqual(result["confidence"], "MEDIUM")
        self.assertEqual(result["mapping_profile"]["known_fault_cases"], 1)

    def test_missing_source_version_or_validation_fails_closed(self):
        for field in ("mapping_version", "sources", "known_fault_validation"):
            with self.subTest(field=field):
                profile = synthetic_profile()
                del profile[field]
                with self.assertRaises(ProfileValidationError):
                    validate_profile(profile)

    def test_known_fault_fixture_is_executed_not_trusted(self):
        profile = synthetic_profile()
        profile["known_fault_validation"]["cases"][0]["expected_channels"] = ["CH-A"]
        self.write_profile(profile)
        result = self.infer()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason"], "INVALID_ASIC_PROFILE_CATALOG")
        self.assertIn("does not match the rules", result["profile_errors"][0])

    def test_revision_and_input_semantics_must_match_exactly(self):
        profile = synthetic_profile()
        profile["asic"]["revision_ids"] = ["0xc1"]
        self.write_profile(profile)
        self.assertEqual(self.infer()["reason"], "NO_VALIDATED_ASIC_PROFILE")

        (self.profiles / "profile.json").unlink()
        profile = synthetic_profile()
        profile["input"]["address_space"] = "physical"
        self.write_profile(profile)
        self.assertEqual(self.infer()["reason"], "INPUT_SEMANTICS_MISMATCH")

    def test_helper_version_experiment_and_device_lost_gates(self):
        profile = synthetic_profile()
        profile["input"]["pattern_version"] = 2
        self.write_profile(profile)
        self.assertEqual(self.infer()["reason"], "INPUT_SEMANTICS_MISMATCH")

        (self.profiles / "profile.json").write_text(json.dumps(synthetic_profile()))
        self.evidence["error_records"][0]["experiment"] = "host_transfer"
        self.evidence["error_records"][1]["experiment"] = "host_transfer"
        self.assertEqual(self.infer()["reason"], "NO_APPLICABLE_ERROR_EVIDENCE")

        self.evidence["error_records"][0]["experiment"] = "vram_pattern"
        self.evidence["device_lost"] = True
        self.assertEqual(self.infer()["reason"], "DEVICE_LOST_INVALIDATES_INFERENCE")

    def test_ambiguous_profiles_fail_closed(self):
        first = synthetic_profile()
        second = synthetic_profile()
        second["profile_id"] = "test-navi21-second"
        second["mapping_version"] = "2.0.0"
        self.write_profile(first, "first.json")
        self.write_profile(second, "second.json")
        self.assertEqual(self.infer()["reason"], "AMBIGUOUS_ASIC_PROFILE")

    def test_inconsistent_helper_error_word_is_rejected_before_inference(self):
        text = helper_stream(errors=[0]).replace('"xor":"0x00020000"', '"xor":"0x00000000"')
        with self.assertRaisesRegex(HelperProtocolError, "inconsistent"):
            parse_helper_jsonl(text, target=self.target, max_error_records=8)


class TestPhase5Report(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.fixture = BoundFixture(self.root)
        (self.fixture.proc / "cmdline").write_text("quiet\n")
        device = self.fixture.add_gpu(driver="amdgpu")
        self.fixture.vulkan_output = "Device Properties and Extensions:\n" + vulkan_device()
        hwmon = device / "hwmon/hwmon0"
        hwmon.mkdir(parents=True)
        (hwmon / "name").write_text("amdgpu\n")
        (hwmon / "temp1_input").write_text("61000\n")

    def driver(self):
        target = self.fixture.collector().target("0000:03:00.0")

        def helper(_target, **kwargs):
            text = helper_stream(errors=[0, 1])
            kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["log_path"].write_text(text)
            return parse_helper_jsonl(text, target=target, max_error_records=256)

        return DriverBoundCollector(
            self.fixture.collector().roots,
            self.fixture.command,
            which=lambda name: f"/usr/bin/{name}",
            helper_runner=helper,
        )

    def run_triage(self):
        return run_pre_driver_triage(
            gpu_arg="0000:03:00.0",
            report_dir_arg=str(self.root / "reports"),
            repo_root=self.root,
            collector=self.fixture.collector(),
            driver_collector=self.driver(),
            vram_seconds=5,
        )

    def test_report_without_production_profile_stays_unknown(self):
        report, json_path, markdown_path = self.run_triage()
        self.assertEqual(report.matrix["asic_channel_lane"].status.value, "UNKNOWN")
        self.assertEqual(report.measurements["asic_inference"]["channel"], "UNKNOWN")
        self.assertIn("ASIC channel / lane | UNKNOWN", markdown_path.read_text())
        persisted = json.loads(json_path.read_text())
        self.assertEqual(persisted["measurements"]["asic_inference"]["reason"], "NO_VALIDATED_ASIC_PROFILE")

    def test_report_names_mapping_version_and_confidence(self):
        profile_dir = self.root / "data/asics/profiles"
        profile_dir.mkdir(parents=True)
        (profile_dir / "synthetic.json").write_text(json.dumps(synthetic_profile()))
        report, _, markdown_path = self.run_triage()
        self.assertEqual(report.matrix["asic_channel_lane"].status.value, "WARN")
        self.assertEqual(report.measurements["asic_inference"]["mapping_version"], "1.2.0")
        markdown = markdown_path.read_text()
        self.assertIn("test-navi21-revc0@1.2.0", markdown)
        self.assertIn("mapping confidence MEDIUM", markdown)
        self.assertIn("Physical VRAM package: UNKNOWN", markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
