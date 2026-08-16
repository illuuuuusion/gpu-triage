#!/usr/bin/env python3
"""Regression tests for the gpu-triage v0.1 MVP.

No real GPU is required: PCI devices are faked as temporary sysfs trees and the
pure helpers are exercised directly. Standard library only, so the suite also
runs unchanged inside the offline Arch live environment.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import gpu_diag  # noqa: E402


def make_pci_tree(root: Path, *, bdf: str = "0000:03:00.0", driver: str | None = "amdgpu",
                  broken_driver: bool = False, vendor: str = "0x1002") -> Path:
    """Create a minimal fake /sys/bus/pci/devices tree and return the devices dir.

    Mirrors real sysfs: devices/ holds symlinks into a nested topology.
    """
    devices = root / "devices"
    devices.mkdir(parents=True, exist_ok=True)
    bridge = root / "topology" / "pci0000:00" / "0000:00:01.0"
    dev = bridge / bdf
    dev.mkdir(parents=True, exist_ok=True)
    (devices / bdf).symlink_to(dev)

    for name, value in {
        "vendor": vendor,
        "device": "0x744c",
        "class": "0x030000",
        "revision": "0xc8",
        "subsystem_vendor": "0x1002",
        "subsystem_device": "0x0e3b",
        "boot_vga": "0",
    }.items():
        (dev / name).write_text(value + "\n")

    if broken_driver:
        (dev / "driver").symlink_to(root / "drivers" / "gone")
    elif driver:
        target = root / "drivers" / driver
        target.mkdir(parents=True, exist_ok=True)
        (dev / "driver").symlink_to(target)
    return devices


def base_report(vram_status: str | None, *, driver_bound: bool = True,
                drm: bool = True, telemetry: bool = True,
                aer_delta: dict | None = None, ksignals: list | None = None) -> dict:
    """A report skeleton with everything healthy unless stated otherwise."""
    return {
        "probe": {"driver_bound": driver_bound, "drm_nodes": ["card0"] if drm else []},
        "vram_test": None if vram_status is None else {"status": vram_status},
        "aer_delta": aer_delta or {},
        "kernel_failure_signals": ksignals or [],
        "telemetry": {"available": telemetry},
    }


class TestDriverDetection(unittest.TestCase):
    """A GPU with no bound driver must report None, not the literal 'driver'."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = gpu_diag.SYS_PCI

    def tearDown(self):
        gpu_diag.SYS_PCI = self._saved

    def test_missing_driver_symlink_is_none(self):
        devices = make_pci_tree(self.tmp, driver=None)
        self.assertIsNone(gpu_diag.read_driver_name(devices / "0000:03:00.0"))

    def test_existing_driver_symlink_returns_name(self):
        devices = make_pci_tree(self.tmp, driver="amdgpu")
        self.assertEqual(gpu_diag.read_driver_name(devices / "0000:03:00.0"), "amdgpu")

    def test_broken_driver_symlink_is_none(self):
        devices = make_pci_tree(self.tmp, broken_driver=True)
        self.assertIsNone(gpu_diag.read_driver_name(devices / "0000:03:00.0"))

    def test_enumerate_reports_none_for_unbound_gpu(self):
        gpu_diag.SYS_PCI = make_pci_tree(self.tmp, driver=None)
        gpus = gpu_diag.enumerate_gpus()
        self.assertEqual(len(gpus), 1)
        self.assertIsNone(gpus[0].driver)

    def test_unbound_gpu_classifies_as_fail(self):
        """The end-to-end acceptance criterion for the driver bug."""
        gpu_diag.SYS_PCI = make_pci_tree(self.tmp, driver=None)
        gpu = gpu_diag.enumerate_gpus()[0]
        probe = gpu_diag.basic_probe(gpu)
        self.assertFalse(probe["driver_bound"])

        result = gpu_diag.classify(base_report("PASS", driver_bound=False))
        self.assertEqual(result["overall"], "FAIL")
        messages = [f"[{f['severity']}] {f['area']}: {f['message']}" for f in result["findings"]]
        self.assertTrue(
            any(m.startswith("[FAIL] DRIVER: No kernel driver is bound") for m in messages),
            messages,
        )


class TestNormalizeBdf(unittest.TestCase):
    def test_full_form_accepted(self):
        self.assertEqual(gpu_diag.normalize_bdf("0000:03:00.0"), "0000:03:00.0")

    def test_short_form_normalized(self):
        self.assertEqual(gpu_diag.normalize_bdf("03:00.0"), "0000:03:00.0")

    def test_uppercase_lowered(self):
        self.assertEqual(gpu_diag.normalize_bdf("0000:0A:00.1"), "0000:0a:00.1")

    def test_surrounding_whitespace_tolerated(self):
        self.assertEqual(gpu_diag.normalize_bdf("  03:00.0 "), "0000:03:00.0")

    def test_non_zero_domain_preserved(self):
        self.assertEqual(gpu_diag.normalize_bdf("0001:03:00.0"), "0001:03:00.0")

    def test_rejects_partial_and_malformed(self):
        for bad in ("1", "00.0", "3:00.0", "0000:03:00", "03:00.8", "0:03:00.0",
                    "", "nonsense", "0000:03:00.0x", "x0000:03:00.0"):
            with self.subTest(value=bad):
                self.assertIsNone(gpu_diag.normalize_bdf(bad))


class TestSelectGpu(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = gpu_diag.SYS_PCI
        gpu_diag.SYS_PCI = make_pci_tree(self.tmp)
        self.gpus = gpu_diag.enumerate_gpus()

    def tearDown(self):
        gpu_diag.SYS_PCI = self._saved

    def test_exact_bdf_accepted(self):
        self.assertEqual(gpu_diag.select_gpu(self.gpus, "0000:03:00.0", False).bdf, "0000:03:00.0")

    def test_short_bdf_accepted(self):
        self.assertEqual(gpu_diag.select_gpu(self.gpus, "03:00.0", False).bdf, "0000:03:00.0")

    def test_partial_input_rejected(self):
        """'1' must never suffix-match 0000:03:00.0. TestNormalizeBdf owns the
        full reject list; this only pins the error message down."""
        with self.assertRaises(gpu_diag.DiagError) as ctx:
            gpu_diag.select_gpu(self.gpus, "1", False)
        self.assertIn("Invalid PCI address", str(ctx.exception))
        self.assertIn("0000:03:00.0", str(ctx.exception))  # lists detected GPUs

    def test_valid_but_absent_bdf_rejected(self):
        with self.assertRaises(gpu_diag.DiagError) as ctx:
            gpu_diag.select_gpu(self.gpus, "0000:09:00.0", False)
        self.assertIn("not found", str(ctx.exception))
        self.assertIn("0000:03:00.0", str(ctx.exception))


class TestClassifyVram(unittest.TestCase):
    def test_healthy_with_vram_pass_is_pass(self):
        self.assertEqual(gpu_diag.classify(base_report("PASS"))["overall"], "PASS")

    def test_vram_skipped_is_warn(self):
        result = gpu_diag.classify(base_report("SKIPPED"))
        self.assertEqual(result["overall"], "WARN")
        self.assertIn(
            "[WARN] VRAM: VRAM test was skipped by request.",
            [f"[{f['severity']}] {f['area']}: {f['message']}" for f in result["findings"]],
        )

    def test_vram_skipped_never_yields_pass(self):
        self.assertNotEqual(gpu_diag.classify(base_report("SKIPPED"))["overall"], "PASS")

    def test_real_fail_survives_vram_skip(self):
        """A skip must degrade PASS to WARN but never soften an actual FAIL."""
        cases = {
            "driver": base_report("SKIPPED", driver_bound=False),
            "aer": base_report("SKIPPED", aer_delta={"0000:03:00.0": {"cor": {"BadTLP": 3}}}),
            "kernel": base_report("SKIPPED", ksignals=["nvidia_xid"]),
        }
        for name, report in cases.items():
            with self.subTest(case=name):
                self.assertEqual(gpu_diag.classify(report)["overall"], "FAIL")

    def test_vram_fail_is_fail(self):
        self.assertEqual(gpu_diag.classify(base_report("FAIL"))["overall"], "FAIL")

    def test_vram_unavailable_is_warn(self):
        self.assertEqual(gpu_diag.classify(base_report("UNAVAILABLE"))["overall"], "WARN")

    def test_skipped_run_marked_incomplete_in_text_report(self):
        report = base_report("SKIPPED")
        report.update({
            "timestamp": "2026-08-13T00:00:00+02:00",
            "probe": {
                "driver_bound": True,
                "drm_nodes": ["card0"],
                "gpu": {"bdf": "0000:03:00.0", "vendor": "AMD", "device_id": 0x744C, "driver": "amdgpu"},
                "description": None,
                "link": {},
                "bars": [],
            },
        })
        report["classification"] = gpu_diag.classify(report)
        text = gpu_diag.text_report(report)
        self.assertIn("Status: SKIPPED", text)
        self.assertIn("INCOMPLETE", text)
        self.assertIn("Overall:   WARN", text)


class TestFindMemtest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._path = os.environ.get("PATH", "")
        self._override = os.environ.pop(gpu_diag.MEMTEST_ENV, None)

    def tearDown(self):
        os.environ["PATH"] = self._path
        os.environ.pop(gpu_diag.MEMTEST_ENV, None)
        if self._override is not None:
            os.environ[gpu_diag.MEMTEST_ENV] = self._override

    def _make_exe(self, directory: Path, name: str = "memtest_vulkan") -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        exe = directory / name
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        return exe

    def test_found_on_path(self):
        exe = self._make_exe(self.tmp / "bin")
        os.environ["PATH"] = str(self.tmp / "bin")
        self.assertEqual(gpu_diag.find_memtest(), str(exe))

    def test_missing_returns_none_and_reason(self):
        os.environ["PATH"] = str(self.tmp / "empty")
        self.assertIsNone(gpu_diag.find_memtest())
        self.assertIn("PATH", gpu_diag.memtest_missing_reason())

    def test_env_override_wins(self):
        on_path = self._make_exe(self.tmp / "bin")
        override = self._make_exe(self.tmp / "custom", "my_memtest")
        os.environ["PATH"] = str(self.tmp / "bin")
        os.environ[gpu_diag.MEMTEST_ENV] = str(override)
        self.assertEqual(gpu_diag.find_memtest(), str(override))
        self.assertNotEqual(gpu_diag.find_memtest(), str(on_path))

    def test_bad_override_reports_clearly(self):
        os.environ["PATH"] = str(self.tmp / "bin")
        self._make_exe(self.tmp / "bin")
        os.environ[gpu_diag.MEMTEST_ENV] = str(self.tmp / "does-not-exist")
        self.assertIsNone(gpu_diag.find_memtest())
        self.assertIn(gpu_diag.MEMTEST_ENV, gpu_diag.memtest_missing_reason())

    def test_non_executable_override_rejected(self):
        plain = self.tmp / "plain"
        plain.write_text("not executable")
        os.environ["PATH"] = str(self.tmp / "empty")
        os.environ[gpu_diag.MEMTEST_ENV] = str(plain)
        self.assertIsNone(gpu_diag.find_memtest())

    def test_missing_memtest_yields_unavailable_not_crash(self):
        os.environ["PATH"] = str(self.tmp / "empty")
        result = gpu_diag.run_memtest_vulkan("0000:03:00.0", 5, self.tmp / "unused.log")
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertTrue(result["reason"])
        self.assertEqual(gpu_diag.classify(base_report("UNAVAILABLE"))["overall"], "WARN")


class TestMemtestAttribution(unittest.TestCase):
    """A verdict may only be reported for the GPU that was actually tested.

    memtest_vulkan autoselects the first device when the selection prompt times
    out, so a target it never listed would otherwise get another card's result.
    """

    PASS_TAIL = "memtest_vulkan: no any errors, testing PASSed."

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._path = os.environ.get("PATH", "")
        self._override = os.environ.pop(gpu_diag.MEMTEST_ENV, None)

    def tearDown(self):
        os.environ["PATH"] = self._path
        os.environ.pop(gpu_diag.MEMTEST_ENV, None)
        if self._override is not None:
            os.environ[gpu_diag.MEMTEST_ENV] = self._override

    def _fake_memtest(self, devices: list[str], *, tail: str, newline: bool = False) -> None:
        """Install a stub that mimics the real device list, prompt and summary."""
        listing = "".join(
            f"{i}: Bus=0x{bus}   8GB Fake GPU\\n" for i, bus in enumerate(devices, 1)
        )
        script = (
            f"#!{sys.executable}\n"
            "import sys, time\n"
            f'sys.stdout.write("{listing}")\n'
            'sys.stdout.write("(first device will be autoselected in 8 seconds)'
            '   Override index to test:")\n'
            "sys.stdout.flush()\n"
            "time.sleep(0.6)\n"
            'sys.stdout.write("\\n    Standard 5-minute test of 1: Bus=0x00:00\\n")\n'
            "sys.stdout.flush()\n"
            "time.sleep(0.4)\n"
            f'sys.stdout.write({tail!r})\n'
            + ('sys.stdout.write("\\n")\n' if newline else "")
        )
        bindir = self.tmp / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        exe = bindir / "memtest_vulkan"
        exe.write_text(script)
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        os.environ["PATH"] = str(bindir)

    def test_target_not_listed_is_error_despite_passing_output(self):
        """The regression: PASS text for device 0a:00 must not become PASS for 03:00."""
        self._fake_memtest(["0a:00"], tail=self.PASS_TAIL)
        result = gpu_diag.run_memtest_vulkan("0000:03:00.0", 5, self.tmp / "mt.log")
        self.assertEqual(result["status"], "ERROR")
        self.assertIsNone(result["selected_index"])
        self.assertIn("did not offer", result["reason"])
        self.assertNotEqual(gpu_diag.classify(base_report(result["status"]))["overall"], "PASS")

    def test_target_not_listed_is_error_despite_failing_output(self):
        """The mirror image: another card's errors must not condemn the target."""
        self._fake_memtest(["0a:00"], tail="Error found. Total errors 42")
        result = gpu_diag.run_memtest_vulkan("0000:03:00.0", 5, self.tmp / "mt.log")
        self.assertEqual(result["status"], "ERROR")

    def test_listed_target_is_selected_and_passes(self):
        self._fake_memtest(["0a:00", "03:00"], tail=self.PASS_TAIL)
        result = gpu_diag.run_memtest_vulkan("0000:03:00.0", 5, self.tmp / "mt.log")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["selected_index"], 2)
        self.assertIsNone(result["reason"])

    def test_non_zero_domain_target_still_matches(self):
        """memtest_vulkan prints no domain; a domain != 0000 must not be excluded."""
        self._fake_memtest(["03:00"], tail=self.PASS_TAIL)
        result = gpu_diag.run_memtest_vulkan("0001:03:00.0", 5, self.tmp / "mt.log")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["target_bus_device"], "03:00")

    def test_ambiguous_target_is_error(self):
        self._fake_memtest(["03:00", "03:00"], tail=self.PASS_TAIL)
        result = gpu_diag.run_memtest_vulkan("0000:03:00.0", 5, self.tmp / "mt.log")
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("unambiguously", result["reason"])

    def test_trailing_partial_line_is_not_duplicated(self):
        """The summary often arrives without a trailing newline."""
        log = self.tmp / "mt.log"
        self._fake_memtest(["03:00"], tail=self.PASS_TAIL)
        gpu_diag.run_memtest_vulkan("0000:03:00.0", 5, log)
        self.assertEqual(log.read_text().count(self.PASS_TAIL), 1)


class TestReportDir(unittest.TestCase):
    def test_unwritable_target_raises_diagerror(self):
        """A read-only stick must produce an instruction, not a traceback."""
        parent = Path(tempfile.mkdtemp())
        os.chmod(parent, 0o500)
        self.addCleanup(os.chmod, parent, 0o700)
        with self.assertRaises(gpu_diag.DiagError) as ctx:
            gpu_diag.choose_report_dir(str(parent / "reports"))
        self.assertIn("--report-dir", str(ctx.exception))


class TestNumericDelta(unittest.TestCase):
    def test_counter_appearing_only_after_is_reported(self):
        """A counter file that shows up during the run is evidence, not noise."""
        delta = gpu_diag.numeric_delta({}, {"0000:00:01.0": {"aer_dev_fatal": {"CmpltTO": 7}}})
        self.assertEqual(gpu_diag.positive_numbers(delta), [("/0000:00:01.0/aer_dev_fatal/CmpltTO", 7)])

    def test_counter_present_before_and_after_uses_difference(self):
        delta = gpu_diag.numeric_delta({"d": {"cor": {"BadTLP": 2}}}, {"d": {"cor": {"BadTLP": 5}}})
        self.assertEqual(gpu_diag.positive_numbers(delta), [("/d/cor/BadTLP", 3)])

    def test_counter_disappearing_is_not_a_positive_signal(self):
        delta = gpu_diag.numeric_delta({"d": {"cor": {"BadTLP": 2}}}, {})
        self.assertEqual(gpu_diag.positive_numbers(delta), [])

    def test_new_counter_classifies_as_fail(self):
        report = base_report("PASS", aer_delta=gpu_diag.numeric_delta({}, {"d": {"cor": {"BadTLP": 1}}}))
        self.assertEqual(gpu_diag.classify(report)["overall"], "FAIL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
