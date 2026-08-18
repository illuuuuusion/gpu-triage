"""Driver-bound evidence collection for an already initialized target.

This module has no driver loading, binding, unbinding, reset, remove or rescan
operation.  Its entry points are called only after Stage 0/1 has observed the
vendor-expected driver on a non-display target.
"""

from __future__ import annotations

import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from collectors import PciTarget, ReadOnlyCollector, Roots, default_run


CommandRunner = Callable[[list[str], float], dict[str, Any]]
LegacyRunner = Callable[[str, int, Path, dict[str, Any]], dict[str, Any]]


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip(), 0)
    except ValueError:
        return None


def _property(block: str, name: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*(\S.*?)\s*$", block, re.MULTILINE)
    return match.group(1).strip() if match else None


def parse_vulkaninfo_devices(text: str) -> list[dict[str, Any]]:
    """Parse the device-property blocks emitted by ``vulkaninfo --text``.

    The text form is used because vulkaninfo's Profiles JSON mode emits one
    selected GPU per file.  A single enumeration must retain every physical
    device in the same ICD view for the legacy singleton gate.
    """
    marker = text.find("Device Properties and Extensions:")
    payload = text[marker:] if marker >= 0 else text
    starts = list(re.finditer(r"^GPU(\d+):\s*$", payload, re.MULTILINE))
    devices: list[dict[str, Any]] = []
    for position, match in enumerate(starts):
        end = starts[position + 1].start() if position + 1 < len(starts) else len(payload)
        block = payload[match.end():end]
        vendor_id = _parse_int(_property(block, "vendorID"))
        device_id = _parse_int(_property(block, "deviceID"))
        # vulkaninfo reuses GPU<n>: headings in later feature/memory sections.
        # Only the canonical VkPhysicalDeviceProperties blocks identify a new
        # physical device; counting the later headings would falsely defeat the
        # legacy singleton gate on every real system.
        if "VkPhysicalDeviceProperties:" not in block or vendor_id is None or device_id is None:
            continue
        pci_values = {
            key: _parse_int(_property(block, key))
            for key in ("pciDomain", "pciBus", "pciDevice", "pciFunction")
        }
        pci_bdf = None
        if all(value is not None for value in pci_values.values()):
            pci_bdf = (
                f"{pci_values['pciDomain']:04x}:{pci_values['pciBus']:02x}:"
                f"{pci_values['pciDevice']:02x}.{pci_values['pciFunction']:x}"
            )
        drm_render = None
        if (_property(block, "drmHasRender") or "").lower() == "true":
            major = _parse_int(_property(block, "drmRenderMajor"))
            minor = _parse_int(_property(block, "drmRenderMinor"))
            if major is not None and minor is not None:
                drm_render = {"major": major, "minor": minor}
        devices.append({
            "index": int(match.group(1)),
            "name": _property(block, "deviceName"),
            "type": _property(block, "deviceType"),
            "vendor_id": vendor_id,
            "device_id": device_id,
            "pci_bdf": pci_bdf,
            "drm_render": drm_render,
        })
    return devices


def _normalise_nvidia_bdf(value: str) -> str | None:
    match = re.fullmatch(
        r"(?:0x)?([0-9a-fA-F]{4}|[0-9a-fA-F]{8}):([0-9a-fA-F]{2}):"
        r"([0-9a-fA-F]{2})\.([0-7])",
        value.strip(),
    )
    if not match:
        return None
    domain = int(match.group(1), 16)
    return f"{domain:04x}:{match.group(2).lower()}:{match.group(3).lower()}.{match.group(4)}"


def nested_counter_delta(before: Any, after: Any) -> Any:
    if isinstance(before, dict) or isinstance(after, dict):
        left = before if isinstance(before, dict) else {}
        right = after if isinstance(after, dict) else {}
        return {
            key: nested_counter_delta(left.get(key, 0), right.get(key, 0))
            for key in sorted(set(left) | set(right))
        }
    if isinstance(before, int) and isinstance(after, int):
        return after - before
    return None


def aer_counter_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Delta AER nodes by BDF while preserving endpoint/upstream attribution."""
    left = {item.get("bdf"): item for item in before.get("nodes", []) if item.get("bdf")}
    right = {item.get("bdf"): item for item in after.get("nodes", []) if item.get("bdf")}
    result: dict[str, Any] = {}
    for bdf in sorted(set(left) | set(right)):
        old = left.get(bdf, {})
        new = right.get(bdf, {})
        result[bdf] = {
            "role": new.get("role") or old.get("role"),
            "counters": nested_counter_delta(old.get("counters", {}), new.get("counters", {})),
        }
    return result


def positive_delta_paths(value: Any, prefix: str = "") -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            result.extend(positive_delta_paths(nested, f"{prefix}/{key}"))
    elif isinstance(value, int) and value > 0:
        result.append((prefix, value))
    return result


def classify_aer_delta(delta: dict[str, Any]) -> dict[str, Any]:
    correctable: list[tuple[str, int]] = []
    severe: list[tuple[str, int]] = []
    for path, value in positive_delta_paths(delta):
        lowered = path.lower()
        if "nonfatal" in lowered or "fatal" in lowered:
            severe.append((path, value))
        else:
            correctable.append((path, value))
    if severe:
        return {"status": "FAIL", "correctable": correctable, "severe": severe}
    if correctable:
        return {"status": "WARN", "correctable": correctable, "severe": []}
    return {"status": "PASS", "correctable": [], "severe": []}


def new_log_lines(before: str, after: str) -> list[str]:
    """Return a multiset suffix without assuming stable journal timestamps."""
    remaining = Counter(before.splitlines())
    result: list[str] = []
    for line in after.splitlines():
        if remaining[line]:
            remaining[line] -= 1
        else:
            result.append(line)
    return result


def kernel_failure_signals(lines: list[str]) -> list[str]:
    rules = {
        "nvidia_xid": re.compile(r"\bXid\b", re.IGNORECASE),
        "nvidia_nvrm_error": re.compile(r"NVRM.*(?:error|failed|fallen off)", re.IGNORECASE),
        "amdgpu_gpu_reset": re.compile(r"amdgpu.*GPU reset", re.IGNORECASE),
        "amdgpu_timeout": re.compile(r"amdgpu.*(?:ring .*timeout|timeout)", re.IGNORECASE),
        "amdgpu_vm_fault": re.compile(r"amdgpu.*(?:VM fault|page fault)", re.IGNORECASE),
        "device_lost": re.compile(r"GPU.*device lost|VK_ERROR_DEVICE_LOST", re.IGNORECASE),
        "kernel_lockup": re.compile(r"hard LOCKUP|soft lockup|watchdog|hung task|rcu.*stall", re.IGNORECASE),
    }
    return [name for name, pattern in rules.items() if any(pattern.search(line) for line in lines)]


class DriverBoundCollector:
    """Read driver-managed interfaces without changing binding or module state."""

    SENSOR_RE = re.compile(
        r"^(?:temp\d+_(?:input|label|crit)|power\d+_(?:average|input|cap)|"
        r"fan\d+_input|freq\d+_input|in\d+_input)$"
    )

    def __init__(
        self,
        roots: Roots | None = None,
        run_command: CommandRunner | None = None,
        *,
        which: Callable[[str], str | None] | None = None,
        legacy_runner: LegacyRunner | None = None,
    ):
        self.roots = roots or Roots()
        self.run_command = run_command or default_run
        self.which = which or shutil.which
        self.legacy_runner = legacy_runner or self._default_legacy_runner
        self.readonly = ReadOnlyCollector(self.roots, self.run_command)

    @property
    def pci_root(self) -> Path:
        return self.roots.sys / "bus/pci/devices"

    def _read(self, path: Path, limit: int = 64 * 1024) -> str | None:
        return self.readonly.read_text(path, limit=limit)

    def _drm_bdf(self, major: int, minor: int) -> str | None:
        candidates = [self.roots.sys / "dev/char" / f"{major}:{minor}" / "device"]
        drm_root = self.roots.sys / "class/drm"
        if drm_root.exists():
            for node in drm_root.iterdir():
                if self._read(node / "dev") == f"{major}:{minor}":
                    candidates.append(node / "device")
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            for node in (resolved, *resolved.parents):
                if re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", node.name.lower()):
                    return node.name.lower()
        return None

    def vulkan_identity(self, target: PciTarget) -> dict[str, Any]:
        executable = self.which("vulkaninfo")
        if not executable:
            return {
                "status": "UNAVAILABLE", "exact_match": False,
                "reason": "VULKANINFO_NOT_FOUND", "devices": [], "hardware_device_count": 0,
            }
        result = self.run_command([executable, "--text"], 30)
        if result.get("rc") != 0:
            return {
                "status": "UNAVAILABLE", "exact_match": False,
                "reason": "VULKAN_ENUMERATION_FAILED", "command": result,
                "devices": [], "hardware_device_count": 0,
            }
        devices = parse_vulkaninfo_devices(result.get("output", ""))
        for device in devices:
            drm = device.get("drm_render")
            device["drm_bdf"] = self._drm_bdf(drm["major"], drm["minor"]) if drm else None
            if device.get("pci_bdf"):
                device["mapping_source"] = "VK_EXT_pci_bus_info"
                device["mapped_bdf"] = device["pci_bdf"]
            elif device.get("drm_bdf"):
                device["mapping_source"] = "VK_EXT_physical_device_drm"
                device["mapped_bdf"] = device["drm_bdf"]
            else:
                device["mapping_source"] = None
                device["mapped_bdf"] = None
        hardware = [item for item in devices if "CPU" not in (item.get("type") or "").upper()]
        matches = [
            item for item in hardware
            if item.get("mapped_bdf") == target.bdf
            and item.get("vendor_id") == target.vendor_id
            and item.get("device_id") == target.device_id
        ]
        if len(matches) != 1:
            return {
                "status": "INCONCLUSIVE" if len(matches) > 1 else "UNAVAILABLE",
                "exact_match": False,
                "reason": "MULTIPLE_EXACT_DEVICE_MATCHES" if len(matches) > 1 else "EXACT_DEVICE_MAPPING_NOT_PROVEN",
                "devices": devices,
                "hardware_device_count": len(hardware),
                "match_count": len(matches),
                "command": {key: result.get(key) for key in ("cmd", "rc", "seconds")},
            }
        match = matches[0]
        legacy_safe = len(hardware) == 1
        return {
            "status": "PASS",
            "exact_match": True,
            "reason": None,
            "target_bdf": target.bdf,
            "index": match["index"],
            "mapping_source": match["mapping_source"],
            "vendor_id": match["vendor_id"],
            "device_id": match["device_id"],
            "devices": devices,
            "hardware_device_count": len(hardware),
            "legacy_safe": legacy_safe,
            "legacy_reason": None if legacy_safe else "LEGACY_DEVICE_INDEX_AMBIGUOUS",
            "command": {key: result.get(key) for key in ("cmd", "rc", "seconds")},
        }

    def amd_telemetry(self, target: PciTarget) -> dict[str, Any]:
        root = self.pci_root / target.bdf
        hwmons: list[dict[str, Any]] = []
        hwmon_root = root / "hwmon"
        if hwmon_root.exists():
            for node in sorted(hwmon_root.glob("hwmon*")):
                sensors: dict[str, Any] = {}
                try:
                    paths = sorted(node.iterdir())
                except OSError:
                    paths = []
                for path in paths:
                    if not self.SENSOR_RE.fullmatch(path.name):
                        continue
                    value = self._read(path)
                    if value is None:
                        continue
                    try:
                        sensors[path.name] = int(value)
                    except ValueError:
                        sensors[path.name] = value
                hwmons.append({"name": self._read(node / "name"), "sensors": sensors})
        ras: dict[str, str] = {}
        ras_root = root / "ras"
        if ras_root.exists():
            for path in sorted(ras_root.glob("*_err_count")):
                value = self._read(path)
                if value is not None:
                    ras[path.name] = value
        return {
            "backend": "amdgpu-sysfs",
            "available": bool(hwmons or ras),
            "hwmon": hwmons,
            "ras_available": bool(ras),
            "ras": ras,
        }

    def nvidia_telemetry(self, target: PciTarget) -> dict[str, Any]:
        executable = self.which("nvidia-smi")
        if not executable:
            return {"backend": "nvidia-smi", "available": False, "reason": "NVIDIA_SMI_NOT_FOUND"}
        fields = [
            "pci.bus_id", "name", "driver_version", "memory.total", "memory.used",
            "temperature.gpu", "power.draw", "clocks.gr", "clocks.mem", "pstate",
            "pcie.link.gen.current", "pcie.link.width.current",
        ]
        result = self.run_command([
            executable, "-i", target.bdf, "--query-gpu=" + ",".join(fields),
            "--format=csv,noheader,nounits",
        ], 12)
        lines = [line for line in result.get("output", "").splitlines() if line.strip()]
        if result.get("rc") != 0 or len(lines) != 1:
            return {
                "backend": "nvidia-smi", "available": False,
                "reason": "NVIDIA_SMI_QUERY_FAILED", "command": result,
            }
        values = [item.strip() for item in lines[0].split(",")]
        parsed = dict(zip(fields, values))
        observed = _normalise_nvidia_bdf(parsed.get("pci.bus_id", ""))
        if observed != target.bdf:
            return {
                "backend": "nvidia-smi", "available": False,
                "reason": "NVIDIA_SMI_BDF_MISMATCH", "observed_bdf": observed,
                "command": {key: result.get(key) for key in ("cmd", "rc", "seconds")},
            }
        return {
            "backend": "nvidia-smi", "available": True, "values": parsed,
            "command": {key: result.get(key) for key in ("cmd", "rc", "seconds")},
        }

    def telemetry(self, target: PciTarget) -> dict[str, Any]:
        if target.vendor_id == 0x1002:
            return self.amd_telemetry(target)
        return self.nvidia_telemetry(target)

    @staticmethod
    def _default_legacy_runner(
        target_bdf: str, seconds: int, log_path: Path, mapping: dict[str, Any]
    ) -> dict[str, Any]:
        from legacy_vram import run_legacy_memtest

        return run_legacy_memtest(target_bdf, seconds, log_path, mapping)

    def legacy_memtest(
        self, target: PciTarget, seconds: int, log_path: Path, mapping: dict[str, Any]
    ) -> dict[str, Any]:
        if not (
            mapping.get("status") == "PASS"
            and mapping.get("exact_match") is True
            and mapping.get("target_bdf") == target.bdf
            and mapping.get("vendor_id") == target.vendor_id
            and mapping.get("device_id") == target.device_id
            and mapping.get("hardware_device_count") == 1
            and mapping.get("legacy_safe") is True
        ):
            return {"status": "UNAVAILABLE", "reason": "EXACT_DEVICE_MAPPING_NOT_PROVEN"}
        return self.legacy_runner(target.bdf, seconds, log_path, mapping)
