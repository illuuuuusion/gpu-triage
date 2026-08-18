#!/usr/bin/env python3
"""Hardware-free acceptance tests for bound-driver Phase 3."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from driver_probe import DriverBoundCollector  # noqa: E402
from safe_triage import SafeTriageError, run_pre_driver_triage  # noqa: E402
from tests.test_phase1 import FORBIDDEN, Fixture  # noqa: E402


def vulkan_device(
    *,
    index: int = 0,
    vendor: int = 0x1002,
    device: int = 0x73AF,
    domain: int | None = 0,
    bus: int = 3,
    slot: int = 0,
    function: int = 0,
    drm: tuple[int, int] | None = None,
) -> str:
    pci = ""
    if domain is not None:
        pci = f"""
VkPhysicalDevicePCIBusInfoPropertiesEXT:
    pciDomain = {domain}
    pciBus = {bus}
    pciDevice = {slot}
    pciFunction = {function}
"""
    drm_text = ""
    if drm:
        drm_text = f"""
VkPhysicalDeviceDrmPropertiesEXT:
    drmHasRender = true
    drmRenderMajor = {drm[0]}
    drmRenderMinor = {drm[1]}
"""
    return f"""GPU{index}:
VkPhysicalDeviceProperties:
    vendorID = 0x{vendor:04x}
    deviceID = 0x{device:04x}
    deviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
    deviceName = Fixture GPU {index}
{pci}{drm_text}
"""


class BoundFixture(Fixture):
    def __init__(self, root: Path):
        super().__init__(root)
        self.vulkan_output = ""
        self.journal_outputs = ["kernel: fixture boot complete\n"]
        self.journal_calls = 0
        self.nvidia_output = ""

    def command(self, cmd: list[str], timeout: float) -> dict:
        self.commands.append(cmd)
        executable = Path(cmd[0]).name
        if executable == "vulkaninfo":
            output, rc = self.vulkan_output, 0
        elif executable == "nvidia-smi":
            output, rc = self.nvidia_output, 0
        elif executable == "journalctl":
            index = min(self.journal_calls, len(self.journal_outputs) - 1)
            self.journal_calls += 1
            output, rc = self.journal_outputs[index], 0
        elif executable == "uname":
            output, rc = "Linux fixture 7.1.5-arch1-2", 0
        elif executable == "git":
            output, rc = "0123456789abcdef", 0
        elif executable == "lspci":
            output, rc = "0000:03:00.0 VGA compatible controller [1002:73af]\n", 0
        else:
            output, rc = "", 0
        return {"cmd": cmd, "rc": rc, "output": output, "seconds": 0.0}

    def driver_collector(self, legacy_runner=None) -> DriverBoundCollector:
        roots = self.collector().roots
        return DriverBoundCollector(
            roots,
            self.command,
            which=lambda name: f"/usr/bin/{name}",
            legacy_runner=legacy_runner,
        )


class Phase3Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.fixture = BoundFixture(self.root)
        (self.fixture.proc / "cmdline").write_text("quiet\n")

    def triage(self, *, driver_collector=None, gpu="0000:03:00.0", no_vram=False):
        return run_pre_driver_triage(
            gpu_arg=gpu,
            report_dir_arg=str(self.root / "reports"),
            repo_root=self.root,
            collector=self.fixture.collector(),
            driver_collector=driver_collector or self.fixture.driver_collector(),
            no_vram=no_vram,
            vram_seconds=5,
        )


class TestAmdBoundFlow(Phase3Case):
    def test_exact_mapping_telemetry_ras_and_legacy_gate(self):
        dev = self.fixture.add_gpu(driver="amdgpu")
        hwmon = dev / "hwmon/hwmon0"
        hwmon.mkdir(parents=True)
        (hwmon / "name").write_text("amdgpu\n")
        (hwmon / "temp1_input").write_text("61000\n")
        ras = dev / "ras"
        ras.mkdir()
        (ras / "umc_err_count").write_text("ue: 0\nce: 0\n")
        self.fixture.vulkan_output = (
            "Device Properties and Extensions:\n" + vulkan_device()
        )
        calls: list[tuple[str, int]] = []

        def legacy(bdf, seconds, log_path, mapping):
            calls.append((bdf, mapping["index"]))
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("fixture legacy PASS\n")
            (dev / "aer_dev_correctable").write_text("BadTLP 1\n")
            return {"status": "PASS", "kind": "LEGACY_SCREEN", "reason": None}

        report, json_path, markdown_path = self.triage(
            driver_collector=self.fixture.driver_collector(legacy)
        )

        self.assertEqual(calls, [("0000:03:00.0", 0)])
        self.assertEqual([stage.value for stage in report.stage_history], [
            "START", "S0_ENVIRONMENT", "S1_PRE_DRIVER", "S3_DRIVER_BOUND",
            "S4_VRAM_COMPUTE", "COMPLETE_INCOMPLETE",
        ])
        self.assertEqual(report.matrix["telemetry"].status.value, "PASS")
        self.assertEqual(report.matrix["vulkan"].status.value, "PASS")
        self.assertEqual(report.matrix["vram_correctness"].status.value, "PASS")
        self.assertEqual(report.matrix["aer"].status.value, "WARN")
        self.assertEqual(
            report.measurements["driver_bound"]["vulkan"]["mapping_source"],
            "VK_EXT_pci_bus_info",
        )
        self.assertIn("umc_err_count", report.measurements["driver_bound"]["telemetry_after"]["ras"])
        self.assertIn("vram_legacy", report.sidecars)
        self.assertTrue((json_path.parent / report.sidecars["vram_legacy"]).is_file())
        self.assertIn("legacy screen", markdown_path.read_text())
        self.assertEqual(json.loads(json_path.read_text())["overall"], "INCOMPLETE")

    def test_missing_telemetry_does_not_block_exact_vulkan_mapping(self):
        self.fixture.add_gpu(driver="amdgpu")
        self.fixture.vulkan_output = "Device Properties and Extensions:\n" + vulkan_device()
        report, _, _ = self.triage(no_vram=True)
        self.assertEqual(report.matrix["telemetry"].status.value, "UNAVAILABLE")
        self.assertEqual(report.matrix["vulkan"].status.value, "PASS")
        self.assertEqual(report.matrix["vram_correctness"].status.value, "NOT_RUN")

    def test_nonfatal_aer_delta_is_fail_and_keeps_upstream_attribution(self):
        dev = self.fixture.add_gpu(driver="amdgpu")
        self.fixture.vulkan_output = "Device Properties and Extensions:\n" + vulkan_device()

        def legacy(_bdf, _seconds, log_path, _mapping):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("fixture\n")
            (dev.parent / "aer_dev_nonfatal").write_text("Completion Timeout 1\n")
            return {"status": "PASS", "kind": "LEGACY_SCREEN"}

        report, _, _ = self.triage(driver_collector=self.fixture.driver_collector(legacy))
        self.assertEqual(report.matrix["aer"].status.value, "FAIL")
        self.assertEqual(report.overall.value, "FAIL")
        self.assertEqual(
            report.measurements["aer_delta"]["0000:00:01.0"]["role"], "upstream"
        )

    def test_device_lost_is_inconclusive_not_a_vram_fail(self):
        self.fixture.add_gpu(driver="amdgpu")
        self.fixture.vulkan_output = "Device Properties and Extensions:\n" + vulkan_device()

        def legacy(_bdf, _seconds, log_path, _mapping):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("VK_ERROR_DEVICE_LOST\n")
            return {
                "status": "INCONCLUSIVE", "kind": "LEGACY_SCREEN",
                "reason": "VULKAN_DEVICE_LOST",
            }

        report, _, _ = self.triage(driver_collector=self.fixture.driver_collector(legacy))
        self.assertEqual(report.matrix["vram_correctness"].status.value, "INCONCLUSIVE")
        self.assertNotEqual(report.overall.value, "FAIL")


class TestVulkanIsolation(Phase3Case):
    def test_repeated_feature_heading_is_not_counted_as_another_device(self):
        self.fixture.add_gpu(driver="amdgpu")
        self.fixture.vulkan_output = (
            "Device Properties and Extensions:\n"
            + vulkan_device()
            + "GPU0:\nVkPhysicalDeviceFeatures:\n    robustBufferAccess = true\n"
        )
        report, _, _ = self.triage(no_vram=True)
        mapping = report.measurements["driver_bound"]["vulkan"]
        self.assertEqual(mapping["hardware_device_count"], 1)
        self.assertTrue(mapping["legacy_safe"])

    def test_nonzero_domain_and_function_must_match_exactly(self):
        self.fixture.add_gpu("0001:03:00.1", driver="amdgpu")
        self.fixture.vulkan_output = (
            "Device Properties and Extensions:\n"
            + vulkan_device(domain=1, function=1)
        )
        report, _, _ = self.triage(gpu="0001:03:00.1", no_vram=True)
        mapping = report.measurements["driver_bound"]["vulkan"]
        self.assertTrue(mapping["exact_match"])
        self.assertEqual(mapping["target_bdf"], "0001:03:00.1")

        self.fixture.vulkan_output = (
            "Device Properties and Extensions:\n"
            + vulkan_device(domain=0, function=0)
        )
        report, _, _ = self.triage(gpu="0001:03:00.1", no_vram=False)
        self.assertFalse(report.measurements["driver_bound"]["vulkan"]["exact_match"])
        self.assertEqual(report.matrix["vram_correctness"].status.value, "UNAVAILABLE")

    def test_drm_render_node_is_an_exact_fallback(self):
        dev = self.fixture.add_gpu(driver="amdgpu")
        render = self.fixture.sys / "class/drm/renderD128"
        render.mkdir(parents=True)
        (render / "dev").write_text("226:128\n")
        (render / "device").symlink_to(dev)
        self.fixture.vulkan_output = (
            "Device Properties and Extensions:\n"
            + vulkan_device(domain=None, drm=(226, 128))
        )
        report, _, _ = self.triage(no_vram=True)
        mapping = report.measurements["driver_bound"]["vulkan"]
        self.assertTrue(mapping["exact_match"])
        self.assertEqual(mapping["mapping_source"], "VK_EXT_physical_device_drm")

    def test_second_hardware_device_blocks_legacy_memtest(self):
        self.fixture.add_gpu(driver="amdgpu")
        self.fixture.vulkan_output = (
            "Device Properties and Extensions:\n"
            + vulkan_device()
            + vulkan_device(index=1, vendor=0x10DE, device=0x2684, bus=4)
        )
        calls = []

        def legacy(*args):
            calls.append(args)
            return {"status": "PASS"}

        report, _, _ = self.triage(driver_collector=self.fixture.driver_collector(legacy))
        self.assertEqual(calls, [])
        self.assertEqual(report.matrix["vulkan"].status.value, "PASS")
        self.assertEqual(report.matrix["vram_correctness"].status.value, "BLOCKED")
        self.assertNotIn("S4_VRAM_COMPUTE", [stage.value for stage in report.stage_history])


class TestNvidiaAndSafety(Phase3Case):
    def test_nvidia_query_is_bdf_specific_and_xid_is_a_new_kernel_failure(self):
        self.fixture.add_gpu(vendor="0x10de", device="0x2684", driver="nvidia")
        self.fixture.vulkan_output = (
            "Device Properties and Extensions:\n"
            + vulkan_device(vendor=0x10DE, device=0x2684)
        )
        self.fixture.nvidia_output = (
            "00000000:03:00.0, Fixture NVIDIA, 590.1, 24564, 100, 55, 25, 1200, 9000, P2, 4, 16\n"
        )
        self.fixture.journal_outputs = [
            "kernel: boot complete\n",
            "kernel: boot complete\nkernel: NVRM: Xid (PCI:0000:03:00): 79, GPU has fallen off the bus\n",
        ]
        report, _, _ = self.triage(no_vram=True)
        queries = [cmd for cmd in self.fixture.commands if Path(cmd[0]).name == "nvidia-smi"]
        self.assertTrue(queries)
        self.assertTrue(all(cmd[1:3] == ["-i", "0000:03:00.0"] for cmd in queries))
        self.assertEqual(report.matrix["telemetry"].status.value, "PASS")
        self.assertEqual(report.matrix["driver_init"].status.value, "FAIL")
        self.assertEqual(report.overall.value, "FAIL")
        self.assertEqual(report.stage.value, "COMPLETE_FAIL")
        self.assertIn("nvidia_xid", report.measurements["driver_bound"]["kernel_failure_signals"])

    def test_unbound_target_never_reaches_driver_commands(self):
        self.fixture.add_gpu()
        (self.fixture.proc / "cmdline").write_text("module_blacklist=amdgpu,radeon quiet\n")
        calls = []

        def legacy(*args):
            calls.append(args)
            return {"status": "PASS"}

        report, _, _ = self.triage(driver_collector=self.fixture.driver_collector(legacy))
        command_names = {Path(cmd[0]).name for cmd in self.fixture.commands}
        self.assertNotIn("vulkaninfo", command_names)
        self.assertNotIn("nvidia-smi", command_names)
        self.assertEqual(calls, [])
        self.assertNotIn("S3_DRIVER_BOUND", [stage.value for stage in report.stage_history])
        words = {word for command in self.fixture.commands for word in command}
        self.assertFalse(words & FORBIDDEN)


class TestPhase3Checkpoints(Phase3Case):
    def setUp(self):
        super().setUp()
        self.fixture.add_gpu(driver="amdgpu")
        self.fixture.vulkan_output = "Device Properties and Extensions:\n" + vulkan_device()

    def test_stage3_collector_failure_leaves_aborted_checkpoint(self):
        driver = self.fixture.driver_collector()
        with mock.patch.object(driver, "telemetry", side_effect=OSError("stage3 fixture failure")):
            with self.assertRaisesRegex(SafeTriageError, "stage3 fixture failure"):
                self.triage(driver_collector=driver)
        checkpoint = next((self.root / "reports").glob("*.json"))
        parsed = json.loads(checkpoint.read_text())
        self.assertEqual(parsed["stage"], "ABORTED")
        self.assertEqual(parsed["stage_history"][-2:], ["S3_DRIVER_BOUND", "ABORTED"])

    def test_stage4_workload_failure_leaves_aborted_checkpoint(self):
        def legacy(*_args):
            raise OSError("stage4 fixture failure")

        with self.assertRaisesRegex(SafeTriageError, "stage4 fixture failure"):
            self.triage(driver_collector=self.fixture.driver_collector(legacy))
        checkpoint = next((self.root / "reports").glob("*.json"))
        parsed = json.loads(checkpoint.read_text())
        self.assertEqual(parsed["stage"], "ABORTED")
        self.assertEqual(parsed["stage_history"][-2:], ["S4_VRAM_COMPUTE", "ABORTED"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
