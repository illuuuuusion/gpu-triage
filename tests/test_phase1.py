#!/usr/bin/env python3
"""Hardware-free acceptance tests for safe-triage Phase 1."""

from __future__ import annotations

import json
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from collectors import QUARANTINE_SENTINEL, ReadOnlyCollector, Roots  # noqa: E402
from safe_triage import SafeTriageError, normalize_explicit_bdf, run_doctor, run_pre_driver_triage  # noqa: E402


FORBIDDEN = {
    "modprobe", "rmmod", "unbind", "bind", "remove", "rescan", "reset",
    "/dev/mem", "nvidia-smi", "vulkaninfo", "memtest_vulkan",
}


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.sys = root / "sys"
        self.proc = root / "proc"
        self.etc = root / "etc"
        self.run = root / "run"
        self.commands: list[list[str]] = []
        for path in (self.sys / "bus/pci/devices", self.proc, self.etc, self.run, self.sys / "fs/pstore"):
            path.mkdir(parents=True, exist_ok=True)
        (self.proc / "cmdline").write_text("module_blacklist=amdgpu,radeon quiet\n")
        (self.proc / "modules").write_text("\n")
        (self.etc / "os-release").write_text('ID=arch\nNAME="Arch Linux"\n')
        dmi = self.sys / "class/dmi/id"
        dmi.mkdir(parents=True)
        for name, value in {
            "board_vendor": "Fixture Corp",
            "board_name": "Safe Board",
            "board_version": "1",
            "bios_version": "F1",
        }.items():
            (dmi / name).write_text(value + "\n")

    def add_gpu(
        self,
        bdf: str = "0000:03:00.0",
        *,
        vendor: str = "0x1002",
        device: str = "0x73af",
        subsystem_vendor: str = "0x1462",
        subsystem_device: str = "0x3955",
        boot_vga: str = "0",
        driver: str | None = None,
        override: str = "",
    ) -> Path:
        domain = bdf.split(":", 1)[0]
        bridge_bdf = f"{domain}:00:01.0"
        bridge = self.sys / "devices" / f"pci{domain}:00" / bridge_bdf
        dev = bridge / bdf
        dev.mkdir(parents=True, exist_ok=True)
        bridge_link = self.sys / "bus/pci/devices" / bridge_bdf
        if not bridge_link.exists():
            bridge_link.symlink_to(bridge)
        link = self.sys / "bus/pci/devices" / bdf
        if not link.exists():
            link.symlink_to(dev)
        for name, value in {
            "vendor": vendor,
            "device": device,
            "class": "0x030000",
            "revision": "0xc0",
            "subsystem_vendor": subsystem_vendor,
            "subsystem_device": subsystem_device,
            "boot_vga": boot_vga,
            "enable": "1",
            "power_state": "D0",
            "driver_override": override,
            "modalias": "pci:fixture",
            "current_link_speed": "16.0 GT/s PCIe",
            "current_link_width": "16",
            "max_link_speed": "16.0 GT/s PCIe",
            "max_link_width": "16",
            "resource": "0x1000 0x1fff 0x200\n0x2000 0x2fff 0x200",
        }.items():
            (dev / name).write_text(value + ("" if value.endswith("\n") else "\n"))
        for node in (dev, bridge):
            for name in ("aer_dev_correctable", "aer_dev_nonfatal", "aer_dev_fatal"):
                (node / name).write_text("TOTAL_ERR_COR 0\n" if name.endswith("correctable") else "TOTAL_ERR 0\n")
        if driver:
            driver_path = self.sys / "bus/pci/drivers" / driver
            driver_path.mkdir(parents=True, exist_ok=True)
            (dev / "driver").symlink_to(driver_path)
        return dev

    def command(self, cmd: list[str], timeout: float) -> dict:
        self.commands.append(cmd)
        if cmd[0] == "uname":
            output = "Linux fixture 7.1.5-arch1-2"
        elif cmd[0] == "git":
            output = "0123456789abcdef"
        elif cmd[0] == "journalctl":
            output = "kernel: fixture boot complete\n"
        elif cmd[0] == "lspci":
            output = "0000:03:00.0 VGA compatible controller [0300]: AMD Navi 21 [1002:73af]\n"
        else:
            output = ""
        return {"cmd": cmd, "rc": 0, "output": output, "seconds": 0.0}

    def collector(self) -> ReadOnlyCollector:
        return ReadOnlyCollector(
            Roots(sys=self.sys, proc=self.proc, etc=self.etc, run=self.run),
            self.command,
        )


class TestPhase1Rx6900Xt(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.fixture = Fixture(self.temp)
        self.dev = self.fixture.add_gpu()

    def run_triage(self):
        return run_pre_driver_triage(
            gpu_arg="0000:03:00.0",
            report_dir_arg=str(self.temp / "reports"),
            repo_root=self.temp,
            collector=self.fixture.collector(),
        )

    def test_fixture_22_creates_safe_usable_report(self):
        report, json_path, markdown_path = self.run_triage()
        self.assertEqual(report.overall.value, "INCOMPLETE")
        self.assertEqual([stage.value for stage in report.stage_history], [
            "START", "S0_ENVIRONMENT", "S1_PRE_DRIVER", "COMPLETE_INCOMPLETE",
        ])
        self.assertEqual(report.safety["driver_intent"]["state"], "INTENTIONAL_GLOBAL_BLACKLIST")
        self.assertEqual(report.matrix["driver_init"].status.value, "NOT_RUN")
        self.assertEqual(report.matrix["vram_correctness"].status.value, "NOT_RUN")
        self.assertEqual(report.matrix["physical_vram_package"].status.value, "UNKNOWN")
        self.assertTrue(json_path.is_file())
        self.assertTrue(markdown_path.is_file())
        parsed = json.loads(json_path.read_text())
        identity = parsed["measurements"]["target"]["identity"]
        self.assertEqual((identity["vendor_id"], identity["device_id"]), (0x1002, 0x73AF))
        self.assertEqual((identity["subsystem_vendor_id"], identity["subsystem_device_id"]), (0x1462, 0x3955))
        self.assertIn("Physical VRAM package: UNKNOWN", markdown_path.read_text())

    def test_endpoint_and_upstream_aer_are_distinct(self):
        report, _, _ = self.run_triage()
        nodes = report.measurements["aer"]["nodes"]
        self.assertEqual(nodes[0]["bdf"], "0000:03:00.0")
        self.assertEqual(nodes[0]["role"], "endpoint")
        self.assertEqual(nodes[1]["bdf"], "0000:00:01.0")
        self.assertEqual(nodes[1]["role"], "upstream")

    def test_upstream_counter_is_not_attributed_to_endpoint(self):
        bridge = self.dev.parent
        (bridge / "aer_dev_nonfatal").write_text("Completion Timeout 2\n")
        report, _, _ = self.run_triage()
        nodes = report.measurements["aer"]["nodes"]
        self.assertEqual(nodes[0]["counters"]["aer_dev_nonfatal"]["TOTAL_ERR"], 0)
        self.assertEqual(nodes[1]["counters"]["aer_dev_nonfatal"]["Completion Timeout"], 2)
        self.assertTrue(any("upstream AER" in item["message"] for item in report.observations))

    def test_no_forbidden_command_is_invoked(self):
        self.run_triage()
        words = {word for command in self.fixture.commands for word in command}
        self.assertTrue({command[0] for command in self.fixture.commands} <= {"uname", "git", "lspci", "journalctl"})
        self.assertFalse(words & FORBIDDEN, self.fixture.commands)
        for command in self.fixture.commands:
            self.assertNotIn("-xxx", command)
            self.assertNotIn("-xxxx", command)
            self.assertNotIn("-M", command)

    def test_pstore_is_copied_and_never_removed(self):
        pstore = self.fixture.sys / "fs/pstore/dmesg-erst-1"
        pstore.write_text("GPU hard LOCKUP fixture\n")
        report, _, _ = self.run_triage()
        copied = self.temp / "reports" / report.sidecars["pstore"] / pstore.name
        self.assertEqual(copied.read_text().strip(), "GPU hard LOCKUP fixture")
        self.assertTrue(pstore.exists())


class TestPhase1SafetyGates(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.fixture = Fixture(self.temp)

    def call(self, gpu: str | None = "0000:03:00.0"):
        return run_pre_driver_triage(
            gpu_arg=gpu,
            report_dir_arg=str(self.temp / "reports"),
            repo_root=self.temp,
            collector=self.fixture.collector(),
        )

    def test_bdf_is_mandatory_and_full(self):
        with self.assertRaisesRegex(SafeTriageError, "explicit --gpu"):
            normalize_explicit_bdf(None)
        with self.assertRaisesRegex(SafeTriageError, "full domain"):
            normalize_explicit_bdf("03:00.0")

    def test_boot_vga_aborts_before_commands(self):
        self.fixture.add_gpu(boot_vga="1")
        with self.assertRaisesRegex(SafeTriageError, "DISPLAY_RISK"):
            self.call()
        self.assertEqual(self.fixture.commands, [])

    def test_framebuffer_owner_aborts(self):
        dev = self.fixture.add_gpu()
        fb = self.fixture.sys / "class/graphics/fb0"
        fb.mkdir(parents=True)
        (fb / "device").symlink_to(dev)
        with self.assertRaisesRegex(SafeTriageError, "framebuffer owner"):
            self.call()

    def test_claimed_safe_boot_with_bound_driver_aborts(self):
        self.fixture.add_gpu(driver="amdgpu")
        with self.assertRaisesRegex(SafeTriageError, "already bound"):
            self.call()

    def test_same_vendor_global_blacklist_is_blocked(self):
        self.fixture.add_gpu()
        self.fixture.add_gpu("0000:04:00.0", device="0x164e")
        with self.assertRaisesRegex(SafeTriageError, "SAFE_BOOT_NOT_PROVEN"):
            self.call()

    def test_unbound_without_intent_is_clear_fail(self):
        self.fixture.add_gpu()
        (self.fixture.proc / "cmdline").write_text("quiet\n")
        report, _, _ = self.call()
        self.assertEqual(report.overall.value, "FAIL")
        self.assertEqual(report.matrix["driver_init"].status.value, "FAIL")
        self.assertEqual(report.matrix["vulkan"].status.value, "BLOCKED")

    def test_bdf_quarantine_is_observed(self):
        self.fixture.add_gpu(override=QUARANTINE_SENTINEL)
        (self.fixture.proc / "cmdline").write_text("quiet\n")
        report, _, _ = self.call()
        self.assertEqual(report.safety["driver_intent"]["state"], "QUARANTINED_BDF")
        self.assertEqual(report.overall.value, "INCOMPLETE")

    def test_nvidia_safe_profile(self):
        self.fixture.add_gpu(vendor="0x10de", device="0x2684")
        (self.fixture.proc / "cmdline").write_text(
            "module_blacklist=nouveau,nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm\n"
        )
        report, _, _ = self.call()
        self.assertEqual(report.safety["driver_intent"]["state"], "INTENTIONAL_GLOBAL_BLACKLIST")


class TestPhase1Doctor(unittest.TestCase):
    def test_doctor_verifies_bundle_without_gpu_access(self):
        root = Path(tempfile.mkdtemp())
        offline = root / "offline"
        profiles = offline / "profiles"
        packages = offline / "packages"
        profiles.mkdir(parents=True)
        packages.mkdir()
        package = packages / "python.pkg.tar.zst"
        package.write_bytes(b"fixture package")
        profile = profiles / "safe-runtime.files"
        profile.write_text("packages/python.pkg.tar.zst\n")
        (offline / "manifest.env").write_text(f"EXPECTED_KERNEL='{os.uname().release}'\n")
        (offline / "SHA256SUMS").write_text(
            f"{hashlib.sha256(package.read_bytes()).hexdigest()}  packages/{package.name}\n"
        )
        (offline / "PROFILE-SHA256SUMS").write_text(
            f"{hashlib.sha256(profile.read_bytes()).hexdigest()}  profiles/{profile.name}\n"
        )
        arch_release = root / "arch-release"
        arch_release.write_text("Arch Linux\n")
        with mock.patch("safe_triage.shutil.which", return_value="/usr/bin/lspci"):
            ok, findings = run_doctor(
                report_dir_arg=str(root / "reports"), repo_root=root, arch_release_path=arch_release
            )
        self.assertTrue(ok, findings)
        self.assertEqual({item["check"] for item in findings}, {
            "report_dir", "safe_runtime", "kernel_bundle", "SHA256SUMS",
            "PROFILE-SHA256SUMS", "safe_runtime_profile", "arch_iso",
        })


if __name__ == "__main__":
    unittest.main(verbosity=2)
