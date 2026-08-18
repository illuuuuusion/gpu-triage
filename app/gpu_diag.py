#!/usr/bin/env python3
"""gpu-triage command line.

Offline-first consumer GPU diagnostic orchestrator for Linux live systems.

The reachable ``triage`` and deprecated ``quick`` commands use the adaptive
Stage-0/1/3/4 state machine in safe_triage.py. Driver-bound collection is only
reachable when the expected driver was already bound before the run.

No /dev/mem, no register writes, no clock/power/fan changes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import selectors
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

from safe_triage import SafeTriageError, run_doctor, run_pre_driver_triage

SYS_PCI = Path("/sys/bus/pci/devices")
PCI_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")
BDF_FULL_RE = re.compile(r"^([0-9a-fA-F]{4}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])$")
BDF_SHORT_RE = re.compile(r"^([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])$")
DISPLAY_CLASSES = {0x030000, 0x030200, 0x038000}
VENDORS = {0x1002: "AMD", 0x10DE: "NVIDIA", 0x8086: "Intel"}
AER_FILES = ("aer_dev_correctable", "aer_dev_nonfatal", "aer_dev_fatal")
MEMTEST_ENV = "GPU_TRIAGE_MEMTEST"
REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class GPU:
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


class DiagError(RuntimeError):
    pass


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except (OSError, PermissionError):
        return None


def read_num(path: Path, base: int = 10) -> int | None:
    """Read a sysfs integer. base=16 also accepts the usual '0x' prefix."""
    value = read_text(path)
    if value is None:
        return None
    try:
        return int(value, base)
    except ValueError:
        return None


def read_driver_name(dev: Path) -> str | None:
    """Return the bound kernel driver name, or None if nothing is bound.

    Path.resolve() is non-strict: on a device without a 'driver' symlink it
    returns the literal path instead of raising, which would report a bound
    driver named "driver". A broken symlink must not count as bound either,
    because an unbound GPU is exactly the fault this tool has to detect.
    """
    link = dev / "driver"
    if not link.is_symlink():
        return None
    try:
        return link.resolve(strict=True).name
    except OSError:
        return None


def normalize_bdf(value: str) -> str | None:
    """Normalize 'DDDD:BB:DD.F' or 'BB:DD.F' to lowercase 'DDDD:BB:DD.F'.

    Returns None for anything else. Matching must be exact: partial input such
    as "1" previously matched any BDF ending in that text and could silently
    select the wrong GPU.
    """
    text = value.strip()
    m = BDF_FULL_RE.match(text)
    if m:
        return f"{m.group(1)}:{m.group(2)}:{m.group(3)}.{m.group(4)}".lower()
    m = BDF_SHORT_RE.match(text)
    if m:
        return f"0000:{m.group(1)}:{m.group(2)}.{m.group(3)}".lower()
    return None


def run(cmd: list[str], timeout: float = 20, env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
        return {
            "cmd": cmd,
            "rc": p.returncode,
            "seconds": round(time.monotonic() - started, 3),
            "output": p.stdout,
        }
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return {
            "cmd": cmd,
            "rc": None,
            "seconds": round(time.monotonic() - started, 3),
            "output": out,
            "timeout": True,
        }
    except OSError as e:
        return {"cmd": cmd, "rc": None, "seconds": 0, "output": str(e), "error": True}


def enumerate_gpus(include_intel: bool = False) -> list[GPU]:
    result: list[GPU] = []
    if not SYS_PCI.exists():
        return result
    for dev in sorted(SYS_PCI.iterdir()):
        vendor = read_num(dev / "vendor", 16)
        device = read_num(dev / "device", 16)
        cls = read_num(dev / "class", 16)
        if vendor is None or device is None or cls is None:
            continue
        base_class = cls & 0xFFFF00
        if base_class not in DISPLAY_CLASSES:
            continue
        if vendor not in (0x1002, 0x10DE) and not (include_intel and vendor == 0x8086):
            continue
        driver = read_driver_name(dev)
        result.append(
            GPU(
                bdf=dev.name,
                vendor_id=vendor,
                device_id=device,
                class_code=cls,
                revision=read_num(dev / "revision", 16),
                subsystem_vendor_id=read_num(dev / "subsystem_vendor", 16),
                subsystem_device_id=read_num(dev / "subsystem_device", 16),
                vendor=VENDORS.get(vendor, f"0x{vendor:04x}"),
                driver=driver,
                boot_vga=(read_num(dev / "boot_vga") == 1),
            )
        )
    return result


def pci_resource_bars(bdf: str) -> list[dict[str, Any]]:
    path = SYS_PCI / bdf / "resource"
    text = read_text(path)
    if not text:
        return []
    bars = []
    for index, line in enumerate(text.splitlines()):
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            start, end, flags = (int(x, 16) for x in parts[:3])
        except ValueError:
            continue
        size = (end - start + 1) if end >= start and start != 0 else 0
        if size:
            bars.append({"resource": index, "start": start, "end": end, "size": size, "flags": flags})
    return bars


def pci_link_info(bdf: str) -> dict[str, Any]:
    root = SYS_PCI / bdf
    keys = ("current_link_speed", "current_link_width", "max_link_speed", "max_link_width")
    data = {k: read_text(root / k) for k in keys}
    # lspci is useful evidence and a fallback for kernels without all sysfs link fields.
    # -nn keeps the numeric vendor/device IDs in the header line, which is also the
    # human-readable device description used by the text report.
    if shutil.which("lspci"):
        data["lspci"] = run(["lspci", "-D", "-s", bdf, "-nn", "-vv"], timeout=8)["output"]
    return data


def pci_description(link: dict[str, Any]) -> str | None:
    """First line of the lspci -vv block: '0000:03:00.0 VGA ... [1002:744c] (rev c8)'."""
    head = (link.get("lspci") or "").strip().splitlines()
    return head[0].strip() if head else None


def pci_chain(bdf: str) -> list[str]:
    """Return endpoint + upstream PCI ancestors that expose PCI BDF names."""
    try:
        p = (SYS_PCI / bdf).resolve()
    except OSError:
        return [bdf]
    found: list[str] = []
    for node in [p, *p.parents]:
        if PCI_RE.match(node.name) and node.name not in found:
            found.append(node.name)
    return found


def parse_aer_file(text: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    if not text:
        return out
    for line in text.splitlines():
        m = re.match(r"^(.*?)\s+(-?\d+)$", line.strip())
        if m:
            out[m.group(1).strip()] = int(m.group(2))
    return out


def aer_snapshot(bdf: str) -> dict[str, Any]:
    snap: dict[str, Any] = {}
    for dev in pci_chain(bdf):
        root = SYS_PCI / dev
        per_dev: dict[str, Any] = {}
        for name in AER_FILES:
            parsed = parse_aer_file(read_text(root / name))
            if parsed:
                per_dev[name] = parsed
        # Root ports may expose aggregate counters too.
        for name in ("aer_rootport_total_err_cor", "aer_rootport_total_err_nonfatal", "aer_rootport_total_err_fatal"):
            val = read_num(root / name)
            if val is not None:
                per_dev[name] = val
        if per_dev:
            snap[dev] = per_dev
    return snap


def numeric_delta(before: Any, after: Any) -> Any:
    """Recursive delta over nested counter dicts.

    A branch present on only one side is treated as all-zero on the other side,
    not as unknown. An AER counter file that first appears during the run — a
    device that re-enumerated after a link event — would otherwise be dropped
    silently, which is exactly the evidence this tool exists to catch.
    """
    if isinstance(before, dict) or isinstance(after, dict):
        b = before if isinstance(before, dict) else {}
        a = after if isinstance(after, dict) else {}
        return {k: numeric_delta(b.get(k, 0), a.get(k, 0)) for k in sorted(set(b) | set(a))}
    if isinstance(before, int) and isinstance(after, int):
        return after - before
    return None


def positive_numbers(obj: Any, prefix: str = "") -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            result.extend(positive_numbers(v, f"{prefix}/{k}"))
    elif isinstance(obj, int) and obj > 0:
        result.append((prefix, obj))
    return result


def drm_nodes_for_bdf(bdf: str) -> list[str]:
    out = []
    drm = Path("/sys/class/drm")
    if not drm.exists():
        return out
    target = (SYS_PCI / bdf).resolve()
    for node in drm.iterdir():
        if not (node.name.startswith("renderD") or node.name.startswith("card")):
            continue
        try:
            if (node / "device").resolve() == target:
                out.append(node.name)
        except OSError:
            pass
    return sorted(out)


def amd_hwmon(bdf: str) -> dict[str, Any]:
    root = SYS_PCI / bdf / "hwmon"
    if not root.exists():
        return {"available": False}
    hwmons = []
    for h in sorted(root.glob("hwmon*")):
        item: dict[str, Any] = {"name": read_text(h / "name"), "path": str(h)}
        sensors: dict[str, Any] = {}
        for p in sorted(h.iterdir()):
            name = p.name
            if not re.match(r"^(temp\d+_(input|label|crit)|power\d+_(average|input|cap)|fan\d+_input|freq\d+_input|in\d+_input)$", name):
                continue
            raw = read_text(p)
            if raw is None:
                continue
            try:
                val: Any = int(raw)
            except ValueError:
                val = raw
            sensors[name] = val
        item["sensors"] = sensors
        hwmons.append(item)
    return {"available": bool(hwmons), "hwmon": hwmons}


def nvidia_telemetry(bdf: str) -> dict[str, Any]:
    if not shutil.which("nvidia-smi"):
        return {"available": False, "reason": "nvidia-smi not found"}
    fields = [
        "pci.bus_id",
        "name",
        "driver_version",
        "memory.total",
        "memory.used",
        "temperature.gpu",
        "power.draw",
        "clocks.gr",
        "clocks.mem",
        "pstate",
        "pcie.link.gen.current",
        "pcie.link.width.current",
    ]
    q = run(
        ["nvidia-smi", "-i", bdf, "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"],
        timeout=10,
    )
    data: dict[str, Any] = {"available": q.get("rc") == 0, "query": q}
    if q.get("rc") == 0 and q.get("output", "").strip():
        vals = [x.strip() for x in q["output"].strip().splitlines()[0].split(",")]
        data["values"] = dict(zip(fields, vals))
    # Keep full XML because it carries more vendor-specific diagnostics and remains valuable offline.
    xml = run(["nvidia-smi", "-i", bdf, "-q", "-x"], timeout=12)
    data["xml"] = xml
    return data


def kernel_log() -> str:
    if shutil.which("journalctl"):
        r = run(["journalctl", "-k", "-b", "--no-pager"], timeout=15)
        if r.get("rc") == 0 and r.get("output"):
            return r["output"]
    if shutil.which("dmesg"):
        return run(["dmesg", "--color=never"], timeout=10).get("output", "")
    return ""


def relevant_kernel_lines(log: str, gpu: GPU) -> list[str]:
    bdf_short = gpu.bdf[5:] if gpu.bdf.startswith("0000:") else gpu.bdf
    patterns = [
        re.escape(gpu.bdf),
        re.escape(bdf_short),
        r"\bAER\b",
        r"PCIe.*error",
        r"NVRM",
        r"Xid",
        r"amdgpu.*(?:error|fail|timeout|reset|fault)",
        r"GPU.*(?:fault|reset|lost|error)",
    ]
    rx = re.compile("|".join(patterns), re.IGNORECASE)
    return [line for line in log.splitlines() if rx.search(line)][-500:]


def kernel_failure_signals(lines: Iterable[str]) -> list[str]:
    signals = []
    rules = {
        "nvidia_xid": re.compile(r"\bXid\b", re.I),
        "nvidia_nvrm_error": re.compile(r"NVRM.*(?:error|fallen off|failed)", re.I),
        "amdgpu_gpu_reset": re.compile(r"amdgpu.*GPU reset", re.I),
        "amdgpu_timeout": re.compile(r"amdgpu.*(?:ring .*timeout|timeout)", re.I),
        "amdgpu_vm_fault": re.compile(r"amdgpu.*(?:VM fault|page fault)", re.I),
        "pcie_fatal": re.compile(r"AER.*(?:fatal|uncorrected)|PCIe.*fatal", re.I),
    }
    for line in lines:
        for name, rx in rules.items():
            if rx.search(line) and name not in signals:
                signals.append(name)
    return signals


def find_memtest() -> str | None:
    """Locate memtest_vulkan.

    The offline package bundle installs it onto PATH, so PATH is the source of
    truth. GPU_TRIAGE_MEMTEST overrides that for ad-hoc runs; when the override
    is set but unusable it is not silently ignored.
    """
    override = os.environ.get(MEMTEST_ENV)
    if override:
        p = Path(override).expanduser()
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    for name in ("memtest_vulkan", "memtest_vulkan_verbose"):
        found = shutil.which(name)
        if found:
            return found
    return None


def memtest_missing_reason() -> str:
    override = os.environ.get(MEMTEST_ENV)
    if override:
        return f"{MEMTEST_ENV}={override!r} is not an executable file"
    return "memtest_vulkan not found on PATH; install the offline package bundle"


def bdf_bus_device(bdf: str) -> str:
    """'0000:03:00.0' -> '03:00', the addressing granularity memtest_vulkan prints."""
    parts = bdf.lower().split(":")
    if len(parts) < 2:
        return bdf.lower()
    return f"{parts[-2]}:{parts[-1].split('.')[0]}"


def parse_memtest_device(line: str) -> tuple[int, str] | None:
    """Parse one device line: '1: Bus=0x01:00 DevId=0x2204   24GB NVIDIA ...'.

    memtest_vulkan prints neither the PCI domain nor the function, so the
    bus:device pair is returned exactly as reported. Fabricating a '0000:'
    domain here would permanently hide GPUs on a non-zero domain.
    """
    m = re.search(r"^\s*(\d+):\s+Bus=0x([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})\b", line)
    if not m:
        return None
    return int(m.group(1)), f"{m.group(2).lower()}:{m.group(3).lower()}"


def run_memtest_vulkan(
    target_bdf: str,
    seconds: int,
    log_path: Path,
    mapping: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deprecated compatibility helper with the same Phase-3 identity gate.

    The production state machine uses ``legacy_vram.run_legacy_memtest``.  This
    wrapper remains for older callers, but bus:device output alone is no longer
    accepted as proof for any PCI domain or function.
    """
    if not (
        mapping
        and mapping.get("mapping_source") in {"VK_EXT_pci_bus_info", "VK_EXT_physical_device_drm"}
        and isinstance(mapping.get("vendor_id"), int)
        and isinstance(mapping.get("device_id"), int)
        and mapping.get("status") == "PASS"
        and mapping.get("exact_match") is True
        and mapping.get("target_bdf") == target_bdf
        and mapping.get("hardware_device_count") == 1
        and mapping.get("legacy_safe") is True
    ):
        return {"status": "UNAVAILABLE", "reason": "EXACT_DEVICE_MAPPING_NOT_PROVEN"}
    exe = find_memtest()
    if not exe:
        return {"status": "UNAVAILABLE", "reason": memtest_missing_reason()}

    cmd = [exe]
    env = os.environ.copy()
    # Keep the caller's Vulkan environment unchanged. The exact mapper and
    # memtest must observe the same ICD view; silently filtering it here would
    # invalidate the singleton proof.

    started = time.monotonic()
    chunks: list[str] = []
    parse_buffer = ""
    selected = False
    selection_sent = False
    selected_index: int | None = None
    target_matches: list[int] = []
    target_key = bdf_bus_device(target_bdf)
    target_unreachable = False
    devices_seen: dict[int, str] = {}

    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        try:
            p = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                env=env,
                start_new_session=True,
            )
        except OSError as e:
            return {"status": "UNAVAILABLE", "reason": str(e), "cmd": cmd}

        assert p.stdout is not None
        sel = selectors.DefaultSelector()
        sel.register(p.stdout, selectors.EVENT_READ)
        init_deadline = started + 25
        test_deadline: float | None = None
        interrupted = False

        def consume_text(text: str) -> None:
            nonlocal parse_buffer, selected_index, selected, selection_sent
            nonlocal test_deadline, target_unreachable
            if not text:
                return
            chunks.append(text)
            log.write(text)
            log.flush()
            parse_buffer += text
            # Parse complete lines but retain the final partial prompt.
            lines = parse_buffer.split("\n")
            parse_buffer = lines.pop()
            for clean in lines:
                parsed = parse_memtest_device(clean)
                if parsed:
                    index, seen = parsed
                    devices_seen[index] = seen
                    if seen == target_key and index not in target_matches:
                        target_matches.append(index)
                if clean.startswith("Testing ") or "Standard 5-minute test of" in clean:
                    selected = True
                    if test_deadline is None:
                        test_deadline = time.monotonic() + max(1, seconds)

            # The selection prompt may not end in a newline, so inspect the partial buffer too.
            if ("Override index to test:" in parse_buffer or "Override index to test:" in text) and not selection_sent:
                # Either the target is addressed unambiguously now, or the run is
                # over. Letting the prompt time out hands the test to whatever
                # device memtest_vulkan autoselects, and a verdict for a GPU that
                # was never touched is worse than no verdict at all.
                if len(target_matches) == 1 and p.stdin:
                    try:
                        p.stdin.write(f"{target_matches[0]}\n".encode())
                        p.stdin.flush()
                        selected_index = target_matches[0]
                        selection_sent = True
                    except (BrokenPipeError, OSError):
                        target_unreachable = True
                else:
                    target_unreachable = True

        while True:
            if p.poll() is not None:
                break
            now = time.monotonic()
            # Stop before the autoselect timer expires and starts loading a
            # foreign device instead of the target.
            if target_unreachable:
                try:
                    os.killpg(p.pid, signal.SIGINT)
                    interrupted = True
                except ProcessLookupError:
                    pass
                break
            # Once the target test has actually started, count the requested duration from that moment.
            if test_deadline is not None and now >= test_deadline:
                try:
                    os.killpg(p.pid, signal.SIGINT)
                    interrupted = True
                except ProcessLookupError:
                    pass
                break
            if test_deadline is None and now >= init_deadline:
                try:
                    os.killpg(p.pid, signal.SIGINT)
                    interrupted = True
                except ProcessLookupError:
                    pass
                break

            events = sel.select(timeout=0.25)
            for key, _ in events:
                try:
                    data = os.read(key.fileobj.fileno(), 65536)
                except OSError:
                    data = b""
                if not data:
                    continue
                consume_text(data.decode(errors="replace"))

        # Drain/report after normal exit or SIGINT.
        try:
            tail, _ = p.communicate(timeout=10 if interrupted else 3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                tail, _ = p.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                tail, _ = p.communicate()
        if tail:
            consume_text(tail.decode(errors="replace") if isinstance(tail, bytes) else tail)
        # No flush of parse_buffer here: consume_text already logged and collected
        # every chunk in full, so appending the trailing partial line again would
        # duplicate it in both the log and the analysed text.

    text = "".join(chunks)
    low = text.lower()
    reason: str | None = None
    seen_list = ", ".join(sorted(set(devices_seen.values()))) or "none"

    # Attribution before interpretation. A PASS or FAIL may only be reported once
    # it is established that memtest_vulkan tested the requested GPU; reading the
    # output text first would attribute a foreign device's result to the target.
    if len(target_matches) > 1:
        status = "ERROR"
        reason = (f"memtest_vulkan listed {len(target_matches)} devices at bus:device {target_key}; "
                  f"the target could not be identified unambiguously")
    elif selected_index is None:
        status = "ERROR"
        reason = (f"memtest_vulkan did not offer {target_bdf} (bus:device {target_key}); "
                  f"devices listed: {seen_list}")
    elif any(x in low for x in ("early exit", "runtime error", "initialization_failed", "incompatible_driver")):
        status = "ERROR"
        reason = "memtest_vulkan reported an initialization or runtime failure"
    elif not selected:
        status = "ERROR"
        reason = "memtest_vulkan never started a test run on the selected device"
    elif "error found" in low:
        status = "FAIL"
    elif "no any errors" in low and "passed" in low:
        status = "PASS"
    else:
        status = "INCONCLUSIVE"

    all_lines = text.splitlines()
    error_summaries = [line for line in all_lines if "Error found" in line or "Errors address range" in line]
    return {
        "status": status,
        "reason": reason,
        "seconds": round(time.monotonic() - started, 2),
        "target_bdf": target_bdf,
        "target_bus_device": target_key,
        "selected_index": selected_index,
        "devices_seen": devices_seen,
        "error_summaries": error_summaries[:50],
        "log": str(log_path),
        "cmd": cmd,
        "vk_driver_files": env.get("VK_DRIVER_FILES"),
    }


def choose_report_dir(explicit: str | None) -> Path:
    # start.sh sets GPU_TRIAGE_REPORT_DIR to <repo>/reports on the writable USB
    # partition; --gpu-less direct invocations fall back to the working directory.
    target = explicit or os.environ.get("GPU_TRIAGE_REPORT_DIR")
    p = Path(target).expanduser().resolve() if target else Path.cwd() / "gpu-triage-reports"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # A read-only remounted USB stick is the expected failure here and
        # deserves an instruction, not a traceback.
        raise DiagError(f"Report directory {p} cannot be created: {e}. Use --report-dir.") from e
    return p


def basic_probe(gpu: GPU) -> dict[str, Any]:
    dev = SYS_PCI / gpu.bdf
    link = pci_link_info(gpu.bdf)
    return {
        "gpu": asdict(gpu),
        "description": pci_description(link),
        "driver_bound": gpu.driver is not None,
        "drm_nodes": drm_nodes_for_bdf(gpu.bdf),
        "link": link,
        "bars": pci_resource_bars(gpu.bdf),
        "power_state": read_text(dev / "power_state"),
        "enable": read_num(dev / "enable"),
        "rom_present": (dev / "rom").exists(),
        "pci_chain": pci_chain(gpu.bdf),
    }


def collect_telemetry(gpu: GPU) -> dict[str, Any]:
    if gpu.vendor_id == 0x1002:
        return {"backend": "amdgpu-hwmon", **amd_hwmon(gpu.bdf)}
    if gpu.vendor_id == 0x10DE:
        return {"backend": "nvidia-smi", **nvidia_telemetry(gpu.bdf)}
    return {"backend": None, "available": False}


def classify(report: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    severity = "PASS"

    probe = report["probe"]
    if not probe.get("driver_bound"):
        findings.append({"severity": "FAIL", "area": "DRIVER", "message": "No kernel driver is bound to the target GPU."})
        severity = "FAIL"
    if not probe.get("drm_nodes"):
        findings.append({"severity": "WARN", "area": "DRIVER", "message": "No DRM card/render node found for the target GPU."})
        if severity == "PASS":
            severity = "WARN"

    vram = report.get("vram_test")
    if vram:
        st = vram.get("status")
        if st == "FAIL":
            findings.append({"severity": "FAIL", "area": "VRAM", "message": "memtest_vulkan reported data errors."})
            severity = "FAIL"
        elif st in ("ERROR", "INCONCLUSIVE"):
            findings.append({"severity": "WARN", "area": "VRAM/VULKAN", "message": f"VRAM test ended as {st}; hardware vs. driver cause is not yet isolated."})
            if severity == "PASS":
                severity = "WARN"
        elif st == "UNAVAILABLE":
            findings.append({"severity": "WARN", "area": "VRAM", "message": "VRAM test backend unavailable."})
            if severity == "PASS":
                severity = "WARN"
        elif st == "SKIPPED":
            # An essential test that never ran must not be reported as PASS.
            findings.append({"severity": "WARN", "area": "VRAM", "message": "VRAM test was skipped by request."})
            if severity == "PASS":
                severity = "WARN"

    aer_pos = positive_numbers(report.get("aer_delta", {}))
    # Root-port TOTAL counters overlap the per-component ones, so a bare count of
    # affected counters overstates the number of events. Name them instead and let
    # the reader see the overlap; any positive change stays a FAIL.
    if aer_pos:
        detail = "; ".join(f"{path.lstrip('/')} +{value}" for path, value in aer_pos[:5])
        if len(aer_pos) > 5:
            detail += f"; and {len(aer_pos) - 5} more"
        findings.append({"severity": "FAIL", "area": "PCIE", "message": f"PCIe AER counters increased during test: {detail}"})
        severity = "FAIL"

    ksig = report.get("kernel_failure_signals", [])
    if ksig:
        findings.append({"severity": "FAIL", "area": "GPU/DRIVER", "message": "Kernel reported GPU failure signals: " + ", ".join(ksig)})
        severity = "FAIL"

    if not report.get("telemetry", {}).get("available", False):
        findings.append({"severity": "WARN", "area": "TELEMETRY", "message": "Vendor telemetry unavailable; this alone does not prove a hardware fault."})
        if severity == "PASS":
            severity = "WARN"

    if not findings:
        findings.append({"severity": "PASS", "area": "MVP", "message": "No fault signal was detected by the implemented MVP tests."})

    return {
        "overall": severity,
        "findings": findings,
        "limitations": [
            "PASS means only that the implemented short tests found no fault; it is not proof of a healthy GPU.",
            "This MVP does not yet isolate shader/core vs. VRAM faults with independent known-answer compute tests.",
            "This MVP does not map a VRAM error to a physical memory package.",
            "Electrical VRM/rail/signal-integrity faults can only be inferred indirectly without external measurement hardware.",
        ],
    }


def human_size(n: int) -> str:
    x = float(n)
    for u in ("B", "KiB", "MiB", "GiB"):
        if x < 1024:
            return f"{x:.1f} {u}"
        x /= 1024
    return f"{x:.1f} TiB"


def text_report(report: dict[str, Any]) -> str:
    gpu = report["probe"]["gpu"]
    c = report["classification"]
    lines = [
        "GPU-TRIAGE MVP REPORT",
        "=" * 72,
        f"Timestamp: {report['timestamp']}",
        f"Target:    {report['probe'].get('description') or (gpu['bdf'] + ' ' + gpu['vendor'] + ' device=0x' + format(gpu['device_id'], '04x'))}",
        f"Driver:    {gpu.get('driver') or 'NONE'}",
        f"Overall:   {c['overall']}",
        "",
        "PCIe",
        "----",
    ]
    link = report["probe"]["link"]
    lines.append(f"Current link: {link.get('current_link_speed') or '?'} x{link.get('current_link_width') or '?'}")
    lines.append(f"Maximum link: {link.get('max_link_speed') or '?'} x{link.get('max_link_width') or '?'}")
    bars = report["probe"].get("bars", [])
    if bars:
        lines.append("BAR/resources: " + ", ".join(f"R{x['resource']}={human_size(x['size'])}" for x in bars))
    aer = positive_numbers(report.get("aer_delta", {}))
    lines.append("AER delta: " + ("none" if not aer else "; ".join(f"{p} +{v}" for p, v in aer[:20])))

    lines += ["", "VRAM", "----"]
    v = report.get("vram_test") or {}
    lines.append(f"Status: {v.get('status', 'NOT RUN')}")
    if v.get("seconds") is not None:
        lines.append(f"Runtime: {v['seconds']} s")
    if v.get("reason"):
        lines.append(f"Reason: {v['reason']}")
    if v.get("status") in ("SKIPPED", "UNAVAILABLE"):
        lines.append("This run is INCOMPLETE: VRAM was never exercised, so the result cannot be a PASS.")
    for e in v.get("error_summaries", [])[:10]:
        lines.append(e)

    lines += ["", "Telemetry", "---------"]
    t = report.get("telemetry", {})
    lines.append(f"Backend: {t.get('backend')}  available={t.get('available')}")
    if t.get("values"):
        for k, val in t["values"].items():
            lines.append(f"{k}: {val}")
    if t.get("backend") == "amdgpu-hwmon":
        for h in t.get("hwmon", []):
            lines.append(f"[{h.get('name') or 'hwmon'}]")
            for k, val in h.get("sensors", {}).items():
                lines.append(f"{k}: {val}")

    lines += ["", "Kernel signals", "--------------"]
    sig = report.get("kernel_failure_signals", [])
    lines.append(", ".join(sig) if sig else "none detected")

    lines += ["", "Classification", "--------------"]
    for f in c["findings"]:
        lines.append(f"[{f['severity']}] {f['area']}: {f['message']}")
    lines += ["", "Important limitations", "---------------------"]
    for x in c["limitations"]:
        lines.append("- " + x)
    lines += ["", "Raw evidence is preserved in the JSON report and sidecar logs.", ""]
    return "\n".join(lines)


def select_gpu(gpus: list[GPU], requested: str | None, interactive: bool) -> GPU:
    if requested:
        detected = ", ".join(g.bdf for g in gpus) or "none"
        target = normalize_bdf(requested)
        if target is None:
            raise DiagError(
                f"Invalid PCI address {requested!r}; expected 0000:03:00.0 or 03:00.0. "
                f"Detected GPUs: {detected}"
            )
        for g in gpus:
            if g.bdf.lower() == target:
                return g
        raise DiagError(f"GPU {target} not found. Detected GPUs: {detected}")
    candidates = [g for g in gpus if not g.boot_vga]
    if len(candidates) == 1:
        return candidates[0]
    if len(gpus) == 1:
        return gpus[0]
    if not interactive:
        raise DiagError("Multiple GPUs found; select target with --gpu 0000:BB:DD.F")
    print("Select GPU under test:")
    for i, g in enumerate(gpus, 1):
        role = "boot/display" if g.boot_vga else "candidate dGPU"
        print(f"  {i}) {g.bdf} {g.vendor} dev=0x{g.device_id:04x} driver={g.driver or '-'} [{role}]")
    while True:
        try:
            n = int(input("GPU number: ").strip())
            return gpus[n - 1]
        except (ValueError, IndexError):
            print("Invalid selection.")


def list_gpus() -> int:
    gpus = enumerate_gpus(include_intel=True)
    if not gpus:
        print("No display-class PCI GPUs found.")
        return 2
    # Deliberately unnumbered: --gpu takes a PCI address, never a list position,
    # and an ordinal here invites '--gpu 1'. The interactive prompt in
    # select_gpu() stays the only numbered list.
    for g in gpus:
        role = "boot/display" if g.boot_vga else "test candidate"
        print(f"{g.bdf}  {g.vendor:<6} device=0x{g.device_id:04x} driver={g.driver or '-':<12} [{role}]")
    return 0


def run_quick(args: argparse.Namespace, interactive: bool = False) -> int:
    gpus = enumerate_gpus(include_intel=False)
    if not gpus:
        raise DiagError("No AMD/NVIDIA display-class PCI GPU found")
    gpu = select_gpu(gpus, args.gpu, interactive)
    report_dir = choose_report_dir(args.report_dir)
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    stem = f"gpu-triage-{stamp}-{gpu.bdf.replace(':', '_')}"
    sidecar = report_dir / (stem + "-memtest.log")

    print(f"Target: {gpu.bdf} {gpu.vendor} device=0x{gpu.device_id:04x} driver={gpu.driver or 'NONE'}")
    print(f"Reports: {report_dir}")
    print("1/5 Probe PCI/driver...")
    probe = basic_probe(gpu)
    print("2/5 Snapshot PCIe AER...")
    aer_before = aer_snapshot(gpu.bdf)
    print("3/5 Collect telemetry/kernel evidence...")
    telemetry_before = collect_telemetry(gpu)

    if args.no_vram:
        print("4/5 VRAM test skipped (--no-vram); this run cannot result in PASS.")
        vram = {"status": "SKIPPED"}
    else:
        print(f"4/5 VRAM test for ~{args.vram_seconds}s (driver-managed Vulkan, no /dev/mem)...")
        vram = run_memtest_vulkan(gpu.bdf, args.vram_seconds, sidecar)
        print(f"     VRAM result: {vram.get('status')}")

    print("5/5 Final evidence/report...")
    aer_after = aer_snapshot(gpu.bdf)
    telemetry_after = collect_telemetry(gpu)
    klog_after = kernel_log()
    relevant = relevant_kernel_lines(klog_after, gpu)

    report: dict[str, Any] = {
        "schema": 1,
        "tool": "gpu-triage-mvp",
        "timestamp": dt.datetime.now().astimezone().isoformat(),
        "probe": probe,
        "aer_before": aer_before,
        "aer_after": aer_after,
        "aer_delta": numeric_delta(aer_before, aer_after),
        "telemetry_before": telemetry_before,
        "telemetry": telemetry_after,
        "vram_test": vram,
        "kernel_failure_signals": kernel_failure_signals(relevant),
        "kernel_relevant_lines": relevant,
        "environment": {
            "uname": run(["uname", "-a"], 3).get("output", "").strip(),
            "cmdline": read_text(Path("/proc/cmdline")),
            "python": sys.version,
            "repo_root": str(REPO_ROOT),
        },
    }
    report["classification"] = classify(report)

    # Print before persisting: if the stick went read-only during the run, the
    # evidence is at least on screen instead of lost behind a write error.
    print()
    print(text_report(report))

    json_path = report_dir / (stem + ".json")
    txt_path = report_dir / (stem + ".txt")
    try:
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        txt_path.write_text(text_report(report), encoding="utf-8")
    except OSError as e:
        raise DiagError(f"Could not write report to {report_dir}: {e}. Use --report-dir.") from e

    print(f"JSON: {json_path}")
    print(f"TEXT: {txt_path}")
    if sidecar.exists():
        print(f"VRAM log: {sidecar}")
    return 0 if report["classification"]["overall"] == "PASS" else 1


def interactive_main(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    print("\nA triage target is never selected interactively. Use --gpu 0000:BB:DD.F.")
    return 2


def run_safe_cli(args: argparse.Namespace) -> int:
    if getattr(args, "rom", False):
        raise DiagError("--rom is reserved and is not enabled by this safe pre-driver implementation")
    try:
        report, json_path, markdown_path = run_pre_driver_triage(
            gpu_arg=args.gpu,
            report_dir_arg=args.report_dir,
            repo_root=REPO_ROOT,
            preflight_only=args.preflight_only,
            no_vram=args.no_vram,
            vram_seconds=args.vram_seconds,
        )
    except SafeTriageError as exc:
        raise DiagError(str(exc)) from exc
    print(f"Target:  {report.target['bdf']}")
    print(f"Stage:   {report.stage.value}")
    print(f"Overall: {report.overall.value}")
    print(f"JSON:    {json_path}")
    print(f"REPORT:  {markdown_path}")
    if report.persistence.get("persistence_lost"):
        print(
            "WARNING: The requested report medium failed. Evidence exists only "
            f"in the volatile runtime mirror: {report.persistence.get('active_dir')}",
            file=sys.stderr,
        )
    return 0 if report.overall.value == "PASS" else 1


def run_doctor_cli(args: argparse.Namespace) -> int:
    ok, findings = run_doctor(report_dir_arg=args.report_dir, repo_root=REPO_ROOT)
    for item in findings:
        print(f"[{item['status']}] {item['check']}: {item['detail']}")
    return 0 if ok else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gpu-triage", description="Safe offline AMD/NVIDIA GPU triage")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("list", help="list display-class PCI GPUs")
    doctor = sub.add_parser("doctor", help="verify report target, pinned bundle, kernel and safe runtime")
    doctor.add_argument("--report-dir", help="explicit atomically writable report directory")
    for command, help_text in (
        ("triage", "run adaptive safe triage for an explicitly selected GPU"),
        ("quick", "deprecated alias for safe triage"),
    ):
        q = sub.add_parser(command, help=help_text)
        q.add_argument("--gpu", required=True, help="exact target PCI BDF, e.g. 0000:03:00.0")
        q.add_argument("--preflight-only", action="store_true", help="stop after read-only Stage 1")
        q.add_argument("--rom", action="store_true", help="reserved explicit ROM opt-in (not enabled yet)")
        q.add_argument("--vram-seconds", type=int, default=60, help="duration of the strictly gated legacy VRAM screen")
        q.add_argument("--no-vram", action="store_true", help="collect driver-bound evidence without VRAM load")
        q.add_argument("--report-dir", help="explicit atomically writable report directory")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command is None:
            return interactive_main(parser)
        if args.command == "list":
            return list_gpus()
        if args.command == "doctor":
            return run_doctor_cli(args)
        if args.command in ("quick", "triage"):
            if args.vram_seconds < 5 or args.vram_seconds > 3600:
                raise DiagError("--vram-seconds must be between 5 and 3600")
            if args.command == "quick":
                print("WARNING: 'quick' is deprecated; running adaptive safe 'triage'.", file=sys.stderr)
            return run_safe_cli(args)
        parser.error("unknown command")
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except DiagError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        # Removable media can disappear mid-run. Report it, do not traceback.
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
