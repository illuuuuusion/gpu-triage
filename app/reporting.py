"""Compact Phase-1 report and sidecar writers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from triage_model import TriageReport


def validate_report_directory(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, probe_name = tempfile.mkstemp(prefix=".gpu-triage-write-", dir=path)
        probe = Path(probe_name)
        final = probe.with_suffix(".renamed")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("gpu-triage report write probe\n")
            handle.flush()
        os.replace(probe, final)
        final.unlink()
    except OSError as exc:
        raise RuntimeError(f"Report directory {path} is not atomically writable: {exc}") from exc
    return path


def atomic_write(path: Path, content: str | bytes) -> None:
    mode = "wb" if isinstance(content, bytes) else "w"
    kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, mode, **kwargs) as handle:
            handle.write(content)
            handle.flush()
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def markdown_report(report: TriageReport) -> str:
    target = report.target
    identity = report.measurements.get("target", {}).get("identity", {})
    link = report.measurements.get("target", {}).get("link", {})
    safety = report.safety
    lines = [
        "# GPU-TRIAGE SAFE PREFLIGHT",
        "",
        f"Run: {report.timestamp}",
        f"Stage: {report.stage.value}",
        f"Overall: {report.overall.value}",
        "",
        "## Target",
        "",
        f"- BDF: {target.get('bdf')}",
        f"- PCI: {identity.get('vendor_id', 0):04x}:{identity.get('device_id', 0):04x}",
        f"- Subsystem: {identity.get('subsystem_vendor_id')!s}:{identity.get('subsystem_device_id')!s}",
        f"- Class/revision: 0x{identity.get('class_code', 0):06x} / {identity.get('revision')}",
        f"- Driver: {identity.get('driver') or 'none'}",
        "",
        "## Environment / Safety",
        "",
        f"- Driver state: {safety.get('driver_intent', {}).get('state')}",
        f"- Boot cmdline: {report.environment.get('cmdline') or '(empty/unavailable)'}",
        f"- Display role: {'BLOCKED' if safety.get('display_risk', {}).get('risk') else 'non-boot; no active owner found'}",
        "- Automatic driver load/bind/unbind/reset: NOT PERFORMED",
        "",
        "## Result Matrix",
        "",
        "| Test | Status | Detail |",
        "| --- | --- | --- |",
    ]
    labels = {
        "pci_enumeration": "PCI enumeration",
        "target_identity": "Target identity",
        "pcie_link": "PCIe link",
        "aer": "AER",
        "driver_init": "Driver init",
        "telemetry": "Telemetry",
        "vulkan": "Vulkan",
        "vbios_rom": "VBIOS ROM",
        "vram_correctness": "VRAM correctness",
        "compute": "Compute",
        "physical_vram_package": "Physical VRAM package",
    }
    for key, item in report.matrix.items():
        detail = item.detail.replace("|", "\\|")
        lines.append(f"| {labels.get(key, key)} | {item.status.value} | {detail} |")
    lines += [
        "",
        "## Key Measurements",
        "",
        f"- PCIe link: {link.get('current_link_speed') or '?'} x{link.get('current_link_width') or '?'} "
        f"(max {link.get('max_link_speed') or '?'} x{link.get('max_link_width') or '?'})",
        "- PCI chain: " + " -> ".join(report.measurements.get("aer", {}).get("chain", [])),
        f"- BARs present: {len(report.measurements.get('target', {}).get('bars', []))}",
        f"- pstore: {'available' if report.measurements.get('pstore', {}).get('available') else 'unavailable'}",
        "",
        "## Observations",
        "",
    ]
    if report.observations:
        lines.extend(f"- [{item.get('level', 'INFO')}] {item.get('message')}" for item in report.observations)
    else:
        lines.append("- No additional fault signal was observed in this pre-driver snapshot.")
    lines += ["", "## Interpretation", ""]
    lines.extend(f"- {item}" for item in report.interpretation)
    lines += ["", "## Hypotheses", ""]
    if report.hypotheses:
        lines.extend(
            f"- {item.get('name')} — confidence {item.get('confidence')}: {item.get('basis')}"
            for item in report.hypotheses
        )
    else:
        lines.append("- No component-level fault hypothesis is justified by this preflight alone.")
    lines += [
        "",
        "## Not Tested / Limitations",
        "",
        "- No driver bind, module load, telemetry, Vulkan, VRAM, compute, reset, ROM or MMIO access was performed.",
        "- A single AER snapshot cannot distinguish old/latched events from events caused by this run.",
        "- Allocation offsets cannot identify a physical GDDR package.",
        "- Physical VRAM package: UNKNOWN",
        "",
        "## Sidecars",
        "",
    ]
    if report.sidecars:
        lines.extend(f"- {name}: `{path}`" for name, path in sorted(report.sidecars.items()))
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def write_report(report: TriageReport, report_dir: Path, stem: str) -> tuple[Path, Path]:
    json_path = report_dir / f"{stem}.json"
    markdown_path = report_dir / f"{stem}.md"
    atomic_write(json_path, json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    atomic_write(markdown_path, markdown_report(report))
    return json_path, markdown_path


def write_sidecars(
    report_dir: Path,
    stem: str,
    pci: dict[str, dict[str, Any]],
    kernel: dict[str, Any],
    pstore: dict[str, Any],
) -> dict[str, str]:
    sidecars: dict[str, str] = {}
    lspci_path = report_dir / f"{stem}-lspci.txt"
    blocks: list[str] = []
    for name, result in pci.items():
        blocks.append(f"### {name}\n$ {' '.join(result.get('cmd', []))}\nrc={result.get('rc')}\n{result.get('output', '')}")
    atomic_write(lspci_path, "\n\n".join(blocks))
    sidecars["lspci"] = lspci_path.name

    kernel_path = report_dir / f"{stem}-kernel.log"
    atomic_write(kernel_path, kernel.get("output", ""))
    sidecars["kernel"] = kernel_path.name

    entries = pstore.get("entries", [])
    if entries:
        pstore_dir = report_dir / f"{stem}-pstore"
        pstore_dir.mkdir(exist_ok=True)
        for index, entry in enumerate(entries):
            safe_name = Path(entry.get("name") or f"entry-{index}").name
            atomic_write(pstore_dir / safe_name, (entry.get("content") or "") + "\n")
        sidecars["pstore"] = pstore_dir.name
    return sidecars
