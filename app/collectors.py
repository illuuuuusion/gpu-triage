"""Read-only collectors for Stage 0 and Stage 1.

All filesystem roots and command execution are injectable.  Nothing in this
module writes to sysfs, loads a module, opens a DRM/Vulkan device, or accesses
PCI BAR resources.
"""

from __future__ import annotations

import os
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from triage_model import DriverState


BDF_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
DISPLAY_CLASSES = {0x030000, 0x030200, 0x038000}
SUPPORTED_VENDORS = {0x1002: "AMD", 0x10DE: "NVIDIA"}
AER_FILES = ("aer_dev_correctable", "aer_dev_nonfatal", "aer_dev_fatal")
ROOT_AER_FILES = (
    "aer_rootport_total_err_cor",
    "aer_rootport_total_err_nonfatal",
    "aer_rootport_total_err_fatal",
)
QUARANTINE_SENTINEL = "gpu-triage-quarantine"


@dataclass(frozen=True)
class Roots:
    sys: Path = Path("/sys")
    proc: Path = Path("/proc")
    etc: Path = Path("/etc")
    run: Path = Path("/run")


@dataclass(frozen=True)
class PciTarget:
    bdf: str
    vendor_id: int
    device_id: int
    class_code: int
    revision: int | None
    subsystem_vendor_id: int | None
    subsystem_device_id: int | None
    vendor: str
    driver: str | None
    boot_vga: bool


CommandRunner = Callable[[list[str], float], dict[str, Any]]


def default_run(command: list[str], timeout: float = 20) -> dict[str, Any]:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "cmd": command,
            "rc": proc.returncode,
            "seconds": round(time.monotonic() - started, 3),
            "output": proc.stdout,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        output = getattr(exc, "stdout", "") or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return {
            "cmd": command,
            "rc": None,
            "seconds": round(time.monotonic() - started, 3),
            "output": output or str(exc),
            "error": True,
        }


class ReadOnlyCollector:
    def __init__(self, roots: Roots | None = None, run_command: CommandRunner | None = None):
        self.roots = roots or Roots()
        self.run_command = run_command or default_run

    @property
    def pci_root(self) -> Path:
        return self.roots.sys / "bus/pci/devices"

    def read_text(self, path: Path, *, limit: int | None = None) -> str | None:
        try:
            text = path.read_text(errors="replace")
        except (OSError, PermissionError):
            return None
        if limit is not None:
            text = text[:limit]
        return text.strip()

    def read_num(self, path: Path, base: int = 10) -> int | None:
        value = self.read_text(path)
        if value is None:
            return None
        try:
            return int(value, base)
        except ValueError:
            return None

    @staticmethod
    def driver_name(device: Path) -> str | None:
        link = device / "driver"
        if not link.is_symlink():
            return None
        try:
            return link.resolve(strict=True).name
        except OSError:
            return None

    def target(self, bdf: str) -> PciTarget | None:
        path = self.pci_root / bdf
        if not path.exists():
            return None
        vendor = self.read_num(path / "vendor", 16)
        device = self.read_num(path / "device", 16)
        cls = self.read_num(path / "class", 16)
        if vendor is None or device is None or cls is None:
            return None
        return PciTarget(
            bdf=bdf,
            vendor_id=vendor,
            device_id=device,
            class_code=cls,
            revision=self.read_num(path / "revision", 16),
            subsystem_vendor_id=self.read_num(path / "subsystem_vendor", 16),
            subsystem_device_id=self.read_num(path / "subsystem_device", 16),
            vendor=SUPPORTED_VENDORS.get(vendor, f"0x{vendor:04x}"),
            driver=self.driver_name(path),
            boot_vga=self.read_num(path / "boot_vga") == 1,
        )

    def display_devices(self) -> list[PciTarget]:
        devices: list[PciTarget] = []
        if not self.pci_root.exists():
            return devices
        for path in sorted(self.pci_root.iterdir()):
            if not BDF_RE.fullmatch(path.name.lower()):
                continue
            target = self.target(path.name.lower())
            if target and target.class_code & 0xFFFF00 in DISPLAY_CLASSES:
                devices.append(target)
        return devices

    @staticmethod
    def expected_driver(target: PciTarget) -> str:
        return "amdgpu" if target.vendor_id == 0x1002 else "nvidia"

    def _resolves_to_target(self, candidate: Path, target_path: Path) -> bool:
        try:
            resolved = candidate.resolve(strict=True)
            target = target_path.resolve(strict=True)
        except OSError:
            return False
        return resolved == target or target in resolved.parents

    def display_risk(self, target: PciTarget) -> dict[str, Any]:
        target_path = self.pci_root / target.bdf
        framebuffer_nodes: list[str] = []
        graphics = self.roots.sys / "class/graphics"
        if graphics.exists():
            for fb in sorted(graphics.glob("fb*")):
                if self._resolves_to_target(fb / "device", target_path):
                    framebuffer_nodes.append(fb.name)

        connected_connectors: list[str] = []
        drm = self.roots.sys / "class/drm"
        if drm.exists():
            for status in sorted(drm.glob("card*-*/status")):
                if self.read_text(status) != "connected":
                    continue
                device = status.parent / "device"
                if not device.exists():
                    device = status.parent.parent / "device"
                if self._resolves_to_target(device, target_path):
                    connected_connectors.append(status.parent.name)

        reasons: list[str] = []
        if target.boot_vga:
            reasons.append("boot_vga=1")
        if framebuffer_nodes:
            reasons.append("framebuffer owner: " + ", ".join(framebuffer_nodes))
        if connected_connectors:
            reasons.append("connected DRM connector: " + ", ".join(connected_connectors))
        return {
            "risk": bool(reasons),
            "reasons": reasons,
            "framebuffers": framebuffer_nodes,
            "connected_connectors": connected_connectors,
        }

    def cmdline(self) -> str:
        return self.read_text(self.roots.proc / "cmdline") or ""

    @staticmethod
    def blacklist_modules(cmdline: str) -> set[str]:
        modules: set[str] = set()
        for token in cmdline.split():
            key, sep, value = token.partition("=")
            if sep and key in {"module_blacklist", "modprobe.blacklist", "rd.driver.blacklist"}:
                modules.update(x.strip() for x in value.split(",") if x.strip())
        return modules

    def driver_intent(self, target: PciTarget, display_risk: bool = False) -> dict[str, Any]:
        dev = self.pci_root / target.bdf
        override = self.read_text(dev / "driver_override") or ""
        cmdline = self.cmdline()
        blacklisted = self.blacklist_modules(cmdline)
        if target.vendor_id == 0x1002:
            required = {"amdgpu", "radeon"}
        else:
            required = {"nouveau", "nvidia", "nvidia_drm", "nvidia_modeset", "nvidia_uvm"}
        expected = self.expected_driver(target)
        safe_claimed = required.issubset(blacklisted) or override == QUARANTINE_SENTINEL

        if display_risk:
            state = DriverState.DISPLAY_RISK
        elif target.driver == expected:
            state = DriverState.BOUND_EXPECTED
        elif target.driver:
            state = DriverState.BOUND_OTHER
        elif override == QUARANTINE_SENTINEL:
            state = DriverState.QUARANTINED_BDF
        elif required.issubset(blacklisted):
            state = DriverState.INTENTIONAL_GLOBAL_BLACKLIST
        else:
            state = DriverState.UNBOUND_UNEXPLAINED

        same_vendor = [
            item.bdf for item in self.display_devices()
            if item.bdf != target.bdf and item.vendor_id == target.vendor_id
        ]
        return {
            "state": state.value,
            "expected_driver": expected,
            "observed_driver": target.driver,
            "driver_override": override or None,
            "cmdline": cmdline,
            "blacklisted_modules": sorted(blacklisted),
            "required_blacklist": sorted(required),
            "safe_boot_claimed": safe_claimed,
            "same_vendor_display_devices": same_vendor,
        }

    def pci_chain(self, bdf: str) -> list[str]:
        try:
            path = (self.pci_root / bdf).resolve(strict=True)
        except OSError:
            return [bdf]
        result: list[str] = []
        for node in (path, *path.parents):
            name = node.name.lower()
            if BDF_RE.fullmatch(name) and name not in result:
                result.append(name)
        return result

    @staticmethod
    def parse_aer(text: str | None) -> dict[str, int]:
        result: dict[str, int] = {}
        if not text:
            return result
        for line in text.splitlines():
            match = re.match(r"^(.*?)\s+(-?\d+)$", line.strip())
            if match:
                result[match.group(1).strip()] = int(match.group(2))
        return result

    def aer(self, bdf: str) -> dict[str, Any]:
        chain = self.pci_chain(bdf)
        nodes: list[dict[str, Any]] = []
        for index, address in enumerate(chain):
            root = self.pci_root / address
            counters: dict[str, Any] = {}
            for name in AER_FILES:
                parsed = self.parse_aer(self.read_text(root / name))
                if parsed:
                    counters[name] = parsed
            for name in ROOT_AER_FILES:
                value = self.read_num(root / name)
                if value is not None:
                    counters[name] = value
            nodes.append({
                "bdf": address,
                "role": "endpoint" if index == 0 else "upstream",
                "counters": counters,
            })
        return {"chain": chain, "nodes": nodes, "available": any(n["counters"] for n in nodes)}

    def bars(self, bdf: str) -> list[dict[str, int]]:
        text = self.read_text(self.pci_root / bdf / "resource")
        result: list[dict[str, int]] = []
        if not text:
            return result
        for index, line in enumerate(text.splitlines()):
            fields = line.split()
            if len(fields) < 3:
                continue
            try:
                start, end, flags = (int(item, 16) for item in fields[:3])
            except ValueError:
                continue
            if start and end >= start:
                result.append({
                    "resource": index,
                    "start": start,
                    "end": end,
                    "size": end - start + 1,
                    "flags": flags,
                })
        return result

    def target_measurements(self, target: PciTarget) -> dict[str, Any]:
        dev = self.pci_root / target.bdf
        return {
            "identity": asdict(target),
            "modalias": self.read_text(dev / "modalias"),
            "enable": self.read_num(dev / "enable"),
            "power_state": self.read_text(dev / "power_state"),
            "driver_override": self.read_text(dev / "driver_override") or None,
            "reset_method": self.read_text(dev / "reset_method"),
            "bars": self.bars(target.bdf),
            "link": {
                name: self.read_text(dev / name)
                for name in ("current_link_speed", "current_link_width", "max_link_speed", "max_link_width")
            },
            "pci_chain": self.pci_chain(target.bdf),
        }

    def environment(self, repo_root: Path) -> dict[str, Any]:
        dmi_root = self.roots.sys / "class/dmi/id"
        dmi = {
            name: self.read_text(dmi_root / name)
            for name in ("board_vendor", "board_name", "board_version", "bios_version")
        }
        modules: list[str] = []
        module_text = self.read_text(self.roots.proc / "modules")
        if module_text:
            modules = [line.split()[0] for line in module_text.splitlines() if line.split()]
        os_release: dict[str, str] = {}
        text = self.read_text(self.roots.etc / "os-release")
        if text:
            for line in text.splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    os_release[key] = value.strip().strip('"')
        relevant_module_parameters = {
            "amdgpu": ("aspm", "dc", "gpu_recovery", "ras_enable", "runpm"),
            "radeon": ("aspm", "runpm"),
            "nouveau": ("config", "debug", "runpm"),
            "nvidia": ("NVreg_EnableGpuFirmware", "NVreg_OpenRmEnableUnsupportedGpus"),
            "nvidia_drm": ("fbdev", "modeset"),
            "nvidia_modeset": (),
            "nvidia_uvm": (),
        }
        gpu_modules: dict[str, Any] = {}
        for module, allowlist in relevant_module_parameters.items():
            module_root = self.roots.sys / "module" / module
            if module not in modules and not module_root.exists():
                continue
            gpu_modules[module] = {
                "loaded": module in modules,
                "version": self.read_text(module_root / "version"),
                "parameters": {
                    name: self.read_text(module_root / "parameters" / name)
                    for name in allowlist
                    if (module_root / "parameters" / name).is_file()
                },
            }

        bundle_manifest: dict[str, str] = {}
        manifest_text = self.read_text(repo_root / "offline/manifest.env")
        if manifest_text:
            for line in manifest_text.splitlines():
                match = re.fullmatch(r"([A-Z_][A-Z0-9_]*)='([^']*)'", line)
                if match:
                    bundle_manifest[match.group(1)] = match.group(2)
        release: dict[str, Any] | None = None
        release_text = self.read_text(repo_root / "offline/release.json")
        if release_text:
            try:
                parsed = json.loads(release_text)
                release = {
                    key: parsed.get(key)
                    for key in ("schema", "iso_date", "iso_name", "expected_kernel", "release_tag", "bundle_name")
                }
            except (TypeError, ValueError):
                release = None
        uname = self.run_command(["uname", "-a"], 3)
        revision = self.run_command(["git", "-C", str(repo_root), "rev-parse", "HEAD"], 3)
        return {
            "uname": uname.get("output", "").strip(),
            "os_release": os_release,
            "cmdline": self.cmdline(),
            "dmi": dmi,
            "loaded_modules": modules,
            "gpu_modules": gpu_modules,
            "archiso": {
                "arch_release": self.read_text(self.roots.etc / "arch-release"),
                "version": self.read_text(self.roots.run / "archiso/bootmnt/arch/version"),
            },
            "bundle": {
                "manifest": bundle_manifest,
                "release": release,
                "sha256sums_present": (repo_root / "offline/SHA256SUMS").is_file(),
                "profile_sums_present": (repo_root / "offline/PROFILE-SHA256SUMS").is_file(),
            },
            "tool_revision": revision.get("output", "").strip() if revision.get("rc") == 0 else None,
        }

    def pci_commands(self, bdf: str) -> dict[str, dict[str, Any]]:
        commands = {
            "overview": ["lspci", "-Dnnk"],
            "target": ["lspci", "-D", "-s", bdf, "-nnk"],
            "target_verbose": ["lspci", "-D", "-s", bdf, "-vv"],
            "path": ["lspci", "-D", "-PP", "-s", bdf],
            "tree": ["lspci", "-t"],
        }
        return {name: self.run_command(command, 10) for name, command in commands.items()}

    def kernel_log(self) -> dict[str, Any]:
        journal = self.run_command(["journalctl", "-k", "-b", "--no-pager"], 15)
        if journal.get("rc") == 0 and journal.get("output"):
            return {"source": "journalctl", **journal}
        dmesg = self.run_command(["dmesg", "--color=never"], 10)
        return {"source": "dmesg", **dmesg}

    @staticmethod
    def relevant_kernel_lines(log: str, target: PciTarget) -> list[str]:
        short_bdf = target.bdf.split(":", 1)[-1]
        patterns = (
            re.escape(target.bdf), re.escape(short_bdf), r"\bAER\b", r"PCIe.*error",
            r"\bNVRM\b", r"\bXid\b", r"amdgpu", r"nouveau", r"GPU.*(?:fault|reset|lost|error)",
            r"hard LOCKUP", r"soft lockup", r"watchdog", r"hung task", r"rcu.*stall",
        )
        matcher = re.compile("|".join(patterns), re.IGNORECASE)
        return [line for line in log.splitlines() if matcher.search(line)][-1000:]

    def pstore(self) -> dict[str, Any]:
        root = self.roots.sys / "fs/pstore"
        if not root.exists():
            return {"available": False, "entries": []}
        entries: list[dict[str, Any]] = []
        try:
            paths = sorted(item for item in root.iterdir() if item.is_file())
        except OSError as exc:
            return {"available": True, "read_error": str(exc), "entries": []}
        for path in paths:
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            entries.append({"name": path.name, "size": size, "content": self.read_text(path, limit=1024 * 1024)})
        return {"available": True, "entries": entries}


def positive_counter_paths(value: Any, prefix: str = "") -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            result.extend(positive_counter_paths(nested, f"{prefix}/{key}"))
    elif isinstance(value, int) and value > 0:
        result.append((prefix, value))
    return result
