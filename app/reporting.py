"""Compact, crash-tolerant report, checkpoint and sidecar writers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from triage_model import Stage, TriageReport


MAX_PCI_SIDECAR_BYTES = 2 * 1024 * 1024
MAX_KERNEL_SIDECAR_BYTES = 2 * 1024 * 1024
MAX_PSTORE_ENTRY_BYTES = 1024 * 1024
MAX_PSTORE_TOTAL_BYTES = 4 * 1024 * 1024
MAX_PSTORE_ENTRIES = 32


class ReportWriteError(RuntimeError):
    """No report destination can preserve the current checkpoint."""


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_report_directory(path: Path) -> Path:
    """Prove create, flush, atomic rename and directory-sync support."""
    probe: Path | None = None
    final: Path | None = None
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, probe_name = tempfile.mkstemp(prefix=".gpu-triage-write-", dir=path)
        probe = Path(probe_name)
        final = probe.with_suffix(".renamed")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("gpu-triage report write probe\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(probe, final)
        _sync_directory(path)
        final.unlink()
        _sync_directory(path)
    except OSError as exc:
        for candidate in (probe, final):
            if candidate is not None:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
        raise RuntimeError(f"Report directory {path} is not atomically writable: {exc}") from exc
    return path


def atomic_write(path: Path, content: str | bytes, *, durable: bool = False) -> None:
    """Replace one file atomically; optionally fsync its contents.

    Directory fsync is owned by the checkpoint writer so a stage boundary
    needs only one directory sync after both main files are replaced.
    """
    mode = "wb" if isinstance(content, bytes) else "w"
    kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, mode, **kwargs) as handle:
            handle.write(content)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _one_line(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _hex(value: Any, width: int) -> str:
    return f"{value:0{width}x}" if isinstance(value, int) else "?"


def markdown_report(report: TriageReport) -> str:
    """Render the bounded 50--120 line primary report (never raw dumps)."""
    target = report.target
    identity = report.measurements.get("target", {}).get("identity", {})
    link = report.measurements.get("target", {}).get("link", {})
    safety = report.safety
    persistence = report.persistence
    display = safety.get("display_risk")
    if not isinstance(display, dict):
        display_role = "not evaluated"
    elif display.get("risk"):
        display_role = "BLOCKED"
    else:
        display_role = "no active owner observed"
    driver = identity.get("driver") if "driver" in identity else "not evaluated"
    driver_bound_ran = Stage.S3_DRIVER_BOUND in report.stage_history
    legacy_screen_ran = (
        report.measurements.get("driver_bound", {}).get("vram", {}).get("kind") == "LEGACY_SCREEN"
    )
    lines = [
        "# GPU-TRIAGE REPORT",
        "",
        f"Run: {_one_line(report.timestamp)}",
        f"Stage: {report.stage.value}",
        f"Overall: {report.overall.value}",
        "",
        "## Target",
        "",
        f"- BDF: {_one_line(target.get('bdf') or '?')}",
        f"- PCI: {_hex(identity.get('vendor_id'), 4)}:{_hex(identity.get('device_id'), 4)}",
        f"- Subsystem: {_hex(identity.get('subsystem_vendor_id'), 4)}:{_hex(identity.get('subsystem_device_id'), 4)}",
        f"- Class/revision: 0x{_hex(identity.get('class_code'), 6)} / {_hex(identity.get('revision'), 2)}",
        f"- Driver: {_one_line(driver or 'none')}",
        "",
        "## Environment / Safety",
        "",
        f"- Driver state: {_one_line(safety.get('driver_intent', {}).get('state') or 'not evaluated')}",
        f"- Boot cmdline: {_one_line(report.environment.get('cmdline') or '(empty/unavailable)')}",
        f"- Display role: {display_role}",
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
    if report.matrix:
        for key, item in report.matrix.items():
            detail = _one_line(item.detail).replace("|", "\\|")
            lines.append(f"| {labels.get(key, key)} | {item.status.value} | {detail} |")
    else:
        lines.append("| Stage results | NOT_RUN | checkpoint precedes result collection |")
    chain = report.measurements.get("aer", {}).get("chain", [])
    lines += [
        "",
        "## Key Measurements",
        "",
        f"- PCIe link: {_one_line(link.get('current_link_speed') or '?')} x{_one_line(link.get('current_link_width') or '?')} "
        f"(max {_one_line(link.get('max_link_speed') or '?')} x{_one_line(link.get('max_link_width') or '?')})",
        "- PCI chain: " + (_one_line(" -> ".join(chain)) if chain else "not collected"),
        f"- BARs present: {len(report.measurements.get('target', {}).get('bars', []))}",
        f"- pstore: {'available' if report.measurements.get('pstore', {}).get('available') else 'unavailable/not collected'}",
        f"- Telemetry backend: {_one_line(report.measurements.get('driver_bound', {}).get('telemetry_after', {}).get('backend') or 'not run')}",
        f"- Vulkan mapping: {_one_line(report.measurements.get('driver_bound', {}).get('vulkan', {}).get('mapping_source') or 'not proven')}",
        "",
        "## Observations",
        "",
    ]
    observations = report.observations[:12]
    if observations:
        lines.extend(
            f"- [{_one_line(item.get('level', 'INFO'), 20)}] {_one_line(item.get('message'))}"
            for item in observations
        )
        if len(report.observations) > len(observations):
            lines.append(f"- [INFO] {len(report.observations) - len(observations)} more observation(s) are in JSON/sidecars.")
    else:
        lines.append("- No additional fault signal has been recorded at this checkpoint.")
    lines += ["", "## Interpretation", ""]
    interpretations = report.interpretation[:8]
    if interpretations:
        lines.extend(f"- {_one_line(item)}" for item in interpretations)
    else:
        lines.append("- This checkpoint is incomplete; no diagnostic interpretation is available yet.")
    lines += ["", "## Hypotheses", ""]
    hypotheses = report.hypotheses[:5]
    if hypotheses:
        lines.extend(
            f"- {_one_line(item.get('name'))} — confidence {_one_line(item.get('confidence'))}: {_one_line(item.get('basis'))}"
            for item in hypotheses
        )
    else:
        lines.append("- No component-level fault hypothesis is justified by this evidence alone.")
    lines += [
        "",
        "## Persistence",
        "",
        f"- Requested destination: {_one_line(persistence.get('requested_dir') or 'not initialized')}",
        f"- Runtime mirror: {_one_line(persistence.get('mirror_dir') or 'unavailable')}",
        f"- Active destination: {_one_line(persistence.get('active_dir') or 'none')}",
        f"- Persistent-medium loss: {'YES' if persistence.get('persistence_lost') else 'no'}",
    ]
    warnings = persistence.get("warnings", [])
    if warnings:
        lines.extend(f"- WARNING: {_one_line(item)}" for item in warnings[:3])
    lines += [
        "",
        "## Not Tested / Limitations",
        "",
        "- No driver bind, module load, reset, remove, rescan, ROM or MMIO access was performed.",
        (
            "- Driver-bound telemetry and Vulkan identity enumeration used only the already bound expected driver."
            if driver_bound_ran else
            "- Driver-bound telemetry, Vulkan and VRAM were not performed in this pre-driver run."
        ),
        (
            "- memtest_vulkan was used only as a legacy screen; it does not isolate VRAM, PCIe, compute or controller faults."
            if legacy_screen_ran else
            "- No VRAM correctness workload ran."
        ),
        (
            "- AER deltas cover this process window only; they do not prove the link fault-free under other loads."
            if driver_bound_ran else
            "- A single AER snapshot cannot distinguish old/latched events from events caused by this run."
        ),
        "- Allocation offsets cannot identify a physical GDDR package.",
        "- Physical VRAM package: UNKNOWN",
        "- Runtime-mirror checkpoints are volatile and are not a guarantee across power loss.",
        "",
        "## Sidecars",
        "",
    ]
    if report.sidecars:
        lines.extend(f"- {name}: `{_one_line(path)}`" for name, path in sorted(report.sidecars.items()))
    else:
        lines.append("- none collected at this checkpoint")
    lines.append("")
    if not 50 <= len(lines) <= 120:
        raise ValueError(f"Primary report line budget exceeded: {len(lines)} lines")
    return "\n".join(lines)


class CheckpointWriter:
    """Mirror reports and sidecars, surviving loss of one destination."""

    def __init__(self, primary_dir: Path, mirror_dir: Path, stem: str):
        self.primary_dir = primary_dir
        self.mirror_dir = mirror_dir if mirror_dir.resolve() != primary_dir.resolve() else None
        self.stem = stem
        self.primary_available = True
        self.mirror_available = self.mirror_dir is not None
        self.primary_error: str | None = None
        self.mirror_error: str | None = None
        self._pending_artifacts: dict[str, set[Path]] = {"primary": set(), "mirror": set()}
        if self.mirror_dir is not None:
            try:
                self.mirror_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.mirror_available = False
                self.mirror_error = str(exc)

    def _state(self) -> dict[str, Any]:
        warnings: list[str] = []
        if not self.primary_available:
            warnings.append(
                f"Primary report medium failed ({self.primary_error}); evidence continues only in the volatile runtime mirror."
            )
        if self.mirror_dir is not None and not self.mirror_available:
            warnings.append(f"Runtime mirror failed ({self.mirror_error}); only the requested destination remains.")
        active = self.primary_dir if self.primary_available else (self.mirror_dir if self.mirror_available else None)
        return {
            "requested_dir": str(self.primary_dir),
            "mirror_dir": str(self.mirror_dir) if self.mirror_dir is not None else str(self.primary_dir),
            "active_dir": str(active) if active is not None else None,
            "primary_available": self.primary_available,
            "mirror_available": self.mirror_available,
            "persistence_lost": not self.primary_available,
            "warnings": warnings,
        }

    def _destinations(self) -> list[tuple[str, Path]]:
        result: list[tuple[str, Path]] = []
        if self.mirror_available and self.mirror_dir is not None:
            result.append(("mirror", self.mirror_dir))
        if self.primary_available:
            result.append(("primary", self.primary_dir))
        return result

    def _failed(self, name: str, exc: OSError) -> None:
        if name == "primary":
            self.primary_available = False
            self.primary_error = str(exc)
        else:
            self.mirror_available = False
            self.mirror_error = str(exc)

    def _require_destination(self) -> None:
        if not self.primary_available and not self.mirror_available:
            raise ReportWriteError(
                "Both the requested report medium and /run runtime mirror failed; no crash-tolerant checkpoint remains"
            )

    def _flush_artifacts(self, name: str) -> None:
        """Order bounded sidecar data before the stage checkpoint."""
        paths = self._pending_artifacts[name]
        for path in sorted(paths):
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        for directory in sorted({path.parent for path in paths}, key=lambda item: len(item.parts), reverse=True):
            _sync_directory(directory)

    def checkpoint(self, report: TriageReport) -> tuple[Path, Path]:
        """Durably replace the main files at a stage boundary."""
        # At most two destinations can transition from healthy to failed. A
        # third pass is sufficient to persist the final availability state in
        # whichever destination remains.
        for _attempt in range(3):
            report.persistence = self._state()
            changed = False
            for name, directory in self._destinations():
                try:
                    self._flush_artifacts(name)
                    atomic_write(
                        directory / f"{self.stem}.json",
                        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
                        durable=True,
                    )
                    atomic_write(directory / f"{self.stem}.md", markdown_report(report), durable=True)
                    _sync_directory(directory)
                    self._pending_artifacts[name].clear()
                except OSError as exc:
                    self._failed(name, exc)
                    changed = True
            self._require_destination()
            if not changed:
                break
        report.persistence = self._state()
        active = self.primary_dir if self.primary_available else self.mirror_dir
        if active is None:
            raise ReportWriteError("No active report destination")
        return active / f"{self.stem}.json", active / f"{self.stem}.md"

    def artifact(self, relative: Path, content: str | bytes) -> None:
        """Atomically mirror one bounded sidecar without per-file fsync."""
        for name, directory in self._destinations():
            try:
                target = directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(target, content)
                self._pending_artifacts[name].add(target)
            except OSError as exc:
                self._failed(name, exc)
        self._require_destination()


def _bounded_bytes(content: str | bytes, limit: int, label: str) -> str | bytes:
    raw = content if isinstance(content, bytes) else content.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return content
    marker = f"\n[gpu-triage: {label} truncated at {limit} bytes]\n".encode()
    if len(marker) >= limit:
        clipped = marker[:limit]
        return clipped if isinstance(content, bytes) else clipped.decode("utf-8", errors="ignore")
    payload_size = limit - len(marker)
    if isinstance(content, bytes):
        return raw[:payload_size] + marker
    # Dropping a partial final codepoint keeps re-encoding at or below the
    # byte limit; replacement characters could otherwise grow the sidecar.
    return raw[:payload_size].decode("utf-8", errors="ignore") + marker.decode()


def write_sidecars(
    writer: CheckpointWriter,
    pci: dict[str, dict[str, Any]],
    kernel: dict[str, Any],
    pstore: dict[str, Any],
) -> dict[str, str]:
    """Write bounded raw evidence to both report destinations."""
    sidecars: dict[str, str] = {}
    lspci_name = f"{writer.stem}-lspci.txt"
    blocks: list[str] = []
    for name, result in pci.items():
        blocks.append(
            f"### {name}\n$ {' '.join(result.get('cmd', []))}\n"
            f"rc={result.get('rc')}\n{result.get('output', '')}"
        )
    writer.artifact(Path(lspci_name), _bounded_bytes("\n\n".join(blocks), MAX_PCI_SIDECAR_BYTES, "PCI sidecar"))
    sidecars["lspci"] = lspci_name

    kernel_name = f"{writer.stem}-kernel.log"
    writer.artifact(
        Path(kernel_name),
        _bounded_bytes(kernel.get("output", ""), MAX_KERNEL_SIDECAR_BYTES, "kernel sidecar"),
    )
    sidecars["kernel"] = kernel_name

    entries = pstore.get("entries", [])
    if entries:
        pstore_name = f"{writer.stem}-pstore"
        remaining = MAX_PSTORE_TOTAL_BYTES
        copied = 0
        for index, entry in enumerate(entries[:MAX_PSTORE_ENTRIES]):
            if remaining <= 0:
                break
            safe_name = Path(entry.get("name") or f"entry-{index}").name
            content = (entry.get("content") or "") + "\n"
            limit = min(MAX_PSTORE_ENTRY_BYTES, remaining)
            bounded = _bounded_bytes(content, limit, f"pstore entry {safe_name}")
            size = len(bounded if isinstance(bounded, bytes) else bounded.encode("utf-8"))
            writer.artifact(Path(pstore_name) / safe_name, bounded)
            remaining -= size
            copied += 1
        if len(entries) > copied and remaining > 0:
            marker = (
                f"gpu-triage copied {copied} of {len(entries)} pstore entries; "
                f"limits are {MAX_PSTORE_ENTRIES} entries and {MAX_PSTORE_TOTAL_BYTES} bytes.\n"
            )
            writer.artifact(Path(pstore_name) / "_TRUNCATED.txt", _bounded_bytes(marker, remaining, "pstore set"))
        if copied:
            sidecars["pstore"] = pstore_name
    return sidecars


def write_driver_sidecars(
    writer: CheckpointWriter,
    sidecars: dict[str, str],
    *,
    kernel_before: dict[str, Any],
    kernel_after: dict[str, Any],
    vram_log: Path | None = None,
) -> dict[str, str]:
    """Persist a bounded before/after kernel window and the legacy VRAM log."""
    kernel_name = sidecars.get("kernel", f"{writer.stem}-kernel.log")
    heading_bytes = 96
    per_snapshot = (MAX_KERNEL_SIDECAR_BYTES - heading_bytes) // 2
    before = _bounded_bytes(kernel_before.get("output", ""), per_snapshot, "pre-driver kernel snapshot")
    after = _bounded_bytes(kernel_after.get("output", ""), per_snapshot, "post-driver kernel snapshot")
    if isinstance(before, bytes):
        before = before.decode("utf-8", errors="replace")
    if isinstance(after, bytes):
        after = after.decode("utf-8", errors="replace")
    window = (
        "### BEFORE DRIVER-BOUND STAGE\n"
        + before
        + "\n\n### AFTER DRIVER-BOUND STAGE\n"
        + after
    )
    writer.artifact(
        Path(kernel_name),
        _bounded_bytes(window, MAX_KERNEL_SIDECAR_BYTES, "kernel sidecar"),
    )
    sidecars["kernel"] = kernel_name
    if vram_log is not None and vram_log.is_file():
        try:
            content = vram_log.read_bytes()
        except OSError:
            content = b""
        if content:
            name = f"{writer.stem}-memtest-vulkan.log"
            writer.artifact(
                Path(name),
                _bounded_bytes(content, MAX_KERNEL_SIDECAR_BYTES, "VRAM sidecar"),
            )
            sidecars["vram_legacy"] = name
    return sidecars
