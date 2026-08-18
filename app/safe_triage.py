"""Adaptive safe-triage orchestration for pre-driver and already-bound targets."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Any

from collectors import ReadOnlyCollector, SUPPORTED_VENDORS, positive_counter_paths
from driver_probe import (
    DriverBoundCollector,
    aer_counter_delta,
    classify_aer_delta,
    kernel_failure_signals,
    new_log_lines,
)
from reporting import (
    CheckpointWriter,
    ReportWriteError,
    validate_report_directory,
    write_driver_sidecars,
    write_sidecars,
)
from triage_model import DriverState, MatrixEntry, Overall, Stage, Status, TriageReport, derive_overall


FULL_BDF_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


class SafeTriageError(RuntimeError):
    pass


def _verify_sums(root: Path, sums_path: Path) -> tuple[bool, str]:
    if not sums_path.is_file():
        return False, f"missing {sums_path.name}"
    count = 0
    for line in sums_path.read_text(errors="replace").splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
            return False, f"malformed line in {sums_path.name}"
        relative = fields[1].lstrip("*")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return False, f"unsafe path in {sums_path.name}: {relative}"
        if not candidate.is_file():
            return False, f"missing hashed file: {relative}"
        hasher = hashlib.sha256()
        try:
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
        except OSError as exc:
            return False, f"cannot hash {relative}: {exc}"
        digest = hasher.hexdigest()
        if digest.lower() != fields[0].lower():
            return False, f"hash mismatch: {relative}"
        count += 1
    return (count > 0), (f"{count} hash(es) verified" if count else f"no entries in {sums_path.name}")


def run_doctor(
    *,
    report_dir_arg: str | None,
    repo_root: Path,
    arch_release_path: Path = Path("/etc/arch-release"),
) -> tuple[bool, list[dict[str, str]]]:
    """Verify the Stage-1 environment without installing or touching a GPU."""
    findings: list[dict[str, str]] = []

    report_dir = Path(report_dir_arg).expanduser().resolve() if report_dir_arg else (repo_root / "reports").resolve()
    try:
        validate_report_directory(report_dir)
        findings.append({"status": "PASS", "check": "report_dir", "detail": str(report_dir)})
    except RuntimeError as exc:
        findings.append({"status": "FAIL", "check": "report_dir", "detail": str(exc)})

    findings.append({
        "status": "PASS" if shutil.which("lspci") else "FAIL",
        "check": "safe_runtime",
        "detail": "python + lspci available" if shutil.which("lspci") else "lspci missing",
    })
    findings.append({
        "status": "PASS" if arch_release_path.is_file() else "FAIL",
        "check": "arch_iso",
        "detail": str(arch_release_path),
    })

    offline = repo_root / "offline"
    manifest = offline / "manifest.env"
    values: dict[str, str] = {}
    if manifest.is_file():
        for line in manifest.read_text(errors="replace").splitlines():
            match = re.fullmatch(r"([A-Z_][A-Z0-9_]*)='([^']*)'", line)
            if match:
                values[match.group(1)] = match.group(2)
    expected = values.get("EXPECTED_KERNEL")
    running = os.uname().release
    if expected:
        findings.append({
            "status": "PASS" if expected == running else "FAIL",
            "check": "kernel_bundle",
            "detail": f"running={running}; expected={expected}",
        })
    else:
        findings.append({"status": "FAIL", "check": "kernel_bundle", "detail": "manifest.env lacks EXPECTED_KERNEL"})

    ok, detail = _verify_sums(offline, offline / "SHA256SUMS")
    findings.append({"status": "PASS" if ok else "FAIL", "check": "SHA256SUMS", "detail": detail})
    profile = offline / "profiles/safe-runtime.files"
    profile_sums = offline / "PROFILE-SHA256SUMS"
    if profile.is_file() and profile_sums.is_file():
        profile_ok, profile_detail = _verify_sums(offline, profile_sums)
        findings.append({"status": "PASS" if profile_ok else "FAIL", "check": "PROFILE-SHA256SUMS", "detail": profile_detail})
        findings.append({"status": "PASS", "check": "safe_runtime_profile", "detail": str(profile)})
    else:
        excluded = (offline / "excluded.txt").read_text(errors="replace") if (offline / "excluded.txt").is_file() else ""
        provided = all(re.search(rf"^{name}\s", excluded, re.MULTILINE) for name in ("python", "pciutils"))
        findings.append({
            "status": "PASS" if provided and shutil.which("lspci") else "FAIL",
            "check": "safe_runtime_profile",
            "detail": "legacy bundle: python and pciutils are recorded as ISO-provided" if provided else "profile metadata missing",
        })
    return not any(item["status"] == "FAIL" for item in findings), findings


def normalize_explicit_bdf(value: str | None) -> str:
    if not value:
        raise SafeTriageError("triage requires an explicit --gpu 0000:BB:DD.F")
    if not FULL_BDF_RE.fullmatch(value.strip()):
        raise SafeTriageError("Invalid PCI address; triage requires full domain:bus:device.function form")
    return value.strip().lower()


def _link_width(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


def _aer_observations(aer: dict[str, Any]) -> list[dict[str, str]]:
    observations: list[dict[str, str]] = []
    for node in aer.get("nodes", []):
        positives = positive_counter_paths(node.get("counters", {}))
        if positives:
            detail = ", ".join(f"{path.lstrip('/')}={value}" for path, value in positives[:8])
            observations.append({
                "level": "WARN",
                "message": f"Existing {node['role']} AER counters at {node['bdf']}: {detail}; no run delta is implied.",
            })
    return observations


def _confirm_bound_target(collector: ReadOnlyCollector, original: Any) -> Any:
    """Fail closed if identity, driver binding or display ownership changed."""
    current = collector.target(original.bdf)
    if current is None:
        raise SafeTriageError(f"Target {original.bdf} disappeared before driver-bound work")
    identity = ("vendor_id", "device_id", "class_code", "revision", "subsystem_vendor_id", "subsystem_device_id")
    if any(getattr(current, name) != getattr(original, name) for name in identity):
        raise SafeTriageError(f"Target identity changed at {original.bdf}; refusing driver-bound work")
    expected = collector.expected_driver(current)
    if current.driver != expected:
        raise SafeTriageError(
            f"Target binding changed at {original.bdf}: expected {expected}, observed {current.driver or 'none'}"
        )
    display = collector.display_risk(current)
    if display["risk"]:
        raise SafeTriageError(
            f"DISPLAY_RISK appeared for {original.bdf}: {'; '.join(display['reasons'])}"
        )
    return current


def run_pre_driver_triage(
    *,
    gpu_arg: str | None,
    report_dir_arg: str | None,
    repo_root: Path,
    collector: ReadOnlyCollector | None = None,
    driver_collector: DriverBoundCollector | None = None,
    preflight_only: bool = False,
    no_vram: bool = False,
    vram_seconds: int = 60,
) -> tuple[TriageReport, Path, Path]:
    """Run the state machine; the historical function name is API-compatible."""
    collector = collector or ReadOnlyCollector()

    # Report persistence is checked before touching the target device.
    report_dir = Path(report_dir_arg).expanduser().resolve() if report_dir_arg else (repo_root / "reports").resolve()
    try:
        validate_report_directory(report_dir)
    except RuntimeError as exc:
        raise SafeTriageError(str(exc)) from exc

    timestamp = dt.datetime.now().astimezone()
    bdf = normalize_explicit_bdf(gpu_arg)
    stem = f"gpu-triage-{timestamp.strftime('%Y%m%d-%H%M%S')}-{bdf.replace(':', '_')}"
    writer = CheckpointWriter(report_dir, collector.roots.run / "gpu-triage", stem)
    report = TriageReport(
        schema=3,
        tool="gpu-triage-safe-triage",
        timestamp=timestamp.isoformat(),
        stage=Stage.S0_ENVIRONMENT,
        stage_history=[Stage.START, Stage.S0_ENVIRONMENT],
        target={"bdf": bdf},
        environment={},
        safety={},
        measurements={},
        overall=Overall.INCOMPLETE,
    )

    try:
        # START -> S0 is persisted before the first target lookup. A failure in
        # any Stage-0 gate therefore still leaves a parseable INCOMPLETE report.
        json_path, markdown_path = writer.checkpoint(report)

        target = collector.target(bdf)
        if target is None:
            detected = ", ".join(item.bdf for item in collector.display_devices()) or "none"
            raise SafeTriageError(f"GPU {bdf} was not found exactly. Display devices: {detected}")
        if (
            target.vendor_id not in SUPPORTED_VENDORS
            or target.class_code & 0xFFFF00 not in {0x030000, 0x030200, 0x038000}
        ):
            raise SafeTriageError(f"Target {bdf} is not a supported AMD/NVIDIA display/3D PCI device")

        display = collector.display_risk(target)
        intent = collector.driver_intent(target)
        report.safety = {"display_risk": display, "driver_intent": intent}
        if display["risk"]:
            raise SafeTriageError(f"DISPLAY_RISK for {bdf}: {'; '.join(display['reasons'])}")
        if intent["safe_boot_claimed"] and target.driver:
            raise SafeTriageError(
                f"Safe boot is claimed, but {bdf} is already bound to {target.driver}; refusing device-interactive work"
            )
        if (
            intent["state"] == DriverState.INTENTIONAL_GLOBAL_BLACKLIST.value
            and intent["same_vendor_display_devices"]
        ):
            peers = ", ".join(intent["same_vendor_display_devices"])
            raise SafeTriageError(f"BLOCKED: SAFE_BOOT_NOT_PROVEN (same-vendor display device(s): {peers})")

        report.environment = collector.environment(repo_root)
        report.stage = Stage.S1_PRE_DRIVER
        report.stage_history.append(Stage.S1_PRE_DRIVER)
        json_path, markdown_path = writer.checkpoint(report)

        measurements = collector.target_measurements(target)
        aer = collector.aer(bdf)
        pci = collector.pci_commands(bdf)
        kernel = collector.kernel_log()
        relevant_kernel = collector.relevant_kernel_lines(kernel.get("output", ""), target)
        pstore = collector.pstore()
        sidecars = write_sidecars(writer, pci, kernel, pstore)

        observations = _aer_observations(aer)
        if relevant_kernel:
            observations.append({
                "level": "WARN",
                "message": f"{len(relevant_kernel)} relevant kernel line(s) preserved in the kernel sidecar.",
            })
        if pstore.get("entries"):
            observations.append({
                "level": "WARN",
                "message": (
                    f"{len(pstore['entries'])} persistent pstore record(s) found; bounded copies were preserved "
                    "and gpu-triage did not delete the originals."
                ),
            })

        link = measurements["link"]
        current_width = _link_width(link.get("current_link_width"))
        maximum_width = _link_width(link.get("max_link_width"))
        if current_width is None:
            link_entry = MatrixEntry(Status.UNAVAILABLE, "current link width unavailable")
        elif maximum_width is not None and current_width < maximum_width:
            link_entry = MatrixEntry(
                Status.WARN,
                f"x{current_width}, endpoint max x{maximum_width}; platform expectation unknown",
            )
        else:
            link_entry = MatrixEntry(Status.PASS, f"{link.get('current_link_speed') or '?'} x{current_width}")

        state = DriverState(intent["state"])
        if state in {DriverState.QUARANTINED_BDF, DriverState.INTENTIONAL_GLOBAL_BLACKLIST}:
            driver_entry = MatrixEntry(Status.NOT_RUN, f"intentional safe mode ({state.value})")
            interactive_status = Status.UNSAFE_SKIPPED
        elif state is DriverState.UNBOUND_UNEXPLAINED:
            driver_entry = MatrixEntry(Status.FAIL, "target is unbound without proven quarantine/blacklist")
            interactive_status = Status.BLOCKED
        elif state is DriverState.BOUND_EXPECTED:
            driver_entry = MatrixEntry(Status.PASS, f"expected driver {target.driver} already bound; not invoked")
            interactive_status = Status.NOT_RUN
        else:
            driver_entry = MatrixEntry(Status.BLOCKED, f"observed driver {target.driver or 'none'}")
            interactive_status = Status.BLOCKED

        matrix = {
            "pci_enumeration": MatrixEntry(Status.PASS, "target exists at exact BDF"),
            "target_identity": MatrixEntry(Status.PASS, "vendor/device/class/subsystem read from sysfs"),
            "pcie_link": link_entry,
            "aer": MatrixEntry(
                Status.WARN if _aer_observations(aer) else (Status.PASS if aer["available"] else Status.UNAVAILABLE),
                "snapshot only; no active counted errors observed"
                if not _aer_observations(aer)
                else "pre-existing counters observed",
            ),
            "driver_init": driver_entry,
            "telemetry": MatrixEntry(interactive_status, "Stage 1 performs no driver-interactive calls"),
            "vulkan": MatrixEntry(interactive_status, "Stage 1 performs no Vulkan enumeration"),
            "vbios_rom": MatrixEntry(Status.NOT_RUN, "ROM is a separate opt-in stage"),
            "vram_correctness": MatrixEntry(Status.NOT_RUN, "no safely initialized exact-mapped Vulkan device tested"),
            "compute": MatrixEntry(Status.NOT_RUN, "not part of safe preflight"),
            "physical_vram_package": MatrixEntry(Status.UNKNOWN, "no validated ASIC/board mapping"),
        }
        report.measurements = {
            "target": measurements,
            "aer": aer,
            "kernel": {"source": kernel.get("source"), "relevant_lines": relevant_kernel},
            "pstore": {
                "available": pstore.get("available", False),
                "entries": [
                    {"name": item.get("name"), "size": item.get("size")}
                    for item in pstore.get("entries", [])[:128]
                ],
                "omitted_entry_metadata": max(0, len(pstore.get("entries", [])) - 128),
            },
            "pci_commands": {
                name: {"cmd": value.get("cmd"), "rc": value.get("rc")}
                for name, value in pci.items()
            },
        }
        report.observations = observations
        report.interpretation = [
            "PCI identity, topology, BARs, link state and AER availability were collected read-only.",
            "No driver initialization was attempted; driver-bound correctness remains untested.",
            "VRAM/compute and physical memory-package attribution remain unproven.",
        ]
        report.matrix = matrix
        report.sidecars = sidecars

        if state is not DriverState.BOUND_EXPECTED or preflight_only:
            if state is DriverState.BOUND_EXPECTED and preflight_only:
                report.matrix["driver_init"] = MatrixEntry(
                    Status.PASS, f"expected driver {target.driver} already bound; preflight-only requested"
                )
                report.matrix["telemetry"] = MatrixEntry(Status.NOT_RUN, "preflight-only requested")
                report.matrix["vulkan"] = MatrixEntry(Status.NOT_RUN, "preflight-only requested")
            report.overall = derive_overall(matrix, full_mode=False)
            report.stage = Stage.COMPLETE_INCOMPLETE
            report.stage_history.append(Stage.COMPLETE_INCOMPLETE)
            json_path, markdown_path = writer.checkpoint(report)
            return report, json_path, markdown_path

        # Stage 3 is reachable only for the already observed vendor-expected
        # driver.  The checkpoint precedes every driver-interactive command.
        report.stage = Stage.S3_DRIVER_BOUND
        report.stage_history.append(Stage.S3_DRIVER_BOUND)
        report.matrix["telemetry"] = MatrixEntry(Status.NOT_RUN, "pending Stage 3")
        report.matrix["vulkan"] = MatrixEntry(Status.NOT_RUN, "pending exact identity mapping")
        report.interpretation = [
            "The vendor-expected driver was already bound before gpu-triage began.",
            "Stage 3 may read driver-managed telemetry and enumerate Vulkan identity; it does not load or rebind a driver.",
            "VRAM/compute and physical memory-package attribution remain unproven.",
        ]
        json_path, markdown_path = writer.checkpoint(report)

        target = _confirm_bound_target(collector, target)
        driver_collector = driver_collector or DriverBoundCollector(
            roots=collector.roots, run_command=collector.run_command
        )
        telemetry_before = driver_collector.telemetry(target)
        vulkan = driver_collector.vulkan_identity(target)
        report.measurements["driver_bound"] = {
            "telemetry_before": telemetry_before,
            "vulkan": vulkan,
        }
        report.matrix["telemetry"] = MatrixEntry(
            Status.PASS if telemetry_before.get("available") else Status.UNAVAILABLE,
            telemetry_before.get("backend") or telemetry_before.get("reason") or "telemetry backend unavailable",
        )
        vulkan_status = {
            "PASS": Status.PASS,
            "INCONCLUSIVE": Status.INCONCLUSIVE,
        }.get(vulkan.get("status"), Status.UNAVAILABLE)
        report.matrix["vulkan"] = MatrixEntry(
            vulkan_status,
            (
                f"exact {vulkan.get('mapping_source')} match at {target.bdf}"
                if vulkan.get("exact_match") else vulkan.get("reason", "mapping unavailable")
            ),
        )
        report.matrix["compute"] = MatrixEntry(
            Status.NOT_RUN, "independent compute isolation requires the Phase-4 helper"
        )

        vram_log: Path | None = None
        if no_vram:
            vram = {"status": "NOT_RUN", "reason": "--no-vram requested"}
            report.matrix["vram_correctness"] = MatrixEntry(Status.NOT_RUN, "--no-vram requested")
        elif not vulkan.get("exact_match"):
            vram = {"status": "UNAVAILABLE", "reason": "EXACT_DEVICE_MAPPING_NOT_PROVEN"}
            report.matrix["vram_correctness"] = MatrixEntry(
                Status.UNAVAILABLE, "EXACT_DEVICE_MAPPING_NOT_PROVEN; no allocation started"
            )
        elif not vulkan.get("legacy_safe"):
            vram = {"status": "BLOCKED", "reason": vulkan.get("legacy_reason")}
            report.matrix["vram_correctness"] = MatrixEntry(
                Status.BLOCKED, "LEGACY_DEVICE_INDEX_AMBIGUOUS; no allocation started"
            )
        else:
            target = _confirm_bound_target(collector, target)
            report.stage = Stage.S4_VRAM_COMPUTE
            report.stage_history.append(Stage.S4_VRAM_COMPUTE)
            report.matrix["vram_correctness"] = MatrixEntry(
                Status.NOT_RUN, "legacy screen about to start after exact singleton mapping"
            )
            json_path, markdown_path = writer.checkpoint(report)
            vram_log = (
                collector.roots.run / "gpu-triage-work" / f"{writer.stem}-memtest-vulkan.log"
            )
            vram = driver_collector.legacy_memtest(target, vram_seconds, vram_log, vulkan)
            vram_status = {
                "PASS": Status.PASS,
                "FAIL": Status.FAIL,
                "INCONCLUSIVE": Status.INCONCLUSIVE,
                "UNAVAILABLE": Status.UNAVAILABLE,
                "BLOCKED": Status.BLOCKED,
            }.get(vram.get("status"), Status.INCONCLUSIVE)
            report.matrix["vram_correctness"] = MatrixEntry(
                vram_status,
                "LEGACY_SCREEN" + (f": {vram.get('reason')}" if vram.get("reason") else ""),
            )

        telemetry_after = driver_collector.telemetry(target)
        aer_after = collector.aer(bdf)
        aer_delta = aer_counter_delta(aer, aer_after)
        aer_assessment = classify_aer_delta(aer_delta)
        kernel_after = collector.kernel_log()
        kernel_delta_available = (
            kernel.get("source") == kernel_after.get("source")
            and kernel.get("rc") == 0
            and kernel_after.get("rc") == 0
        )
        new_kernel = (
            new_log_lines(kernel.get("output", ""), kernel_after.get("output", ""))
            if kernel_delta_available else []
        )
        relevant_new_kernel = collector.relevant_kernel_lines("\n".join(new_kernel), target)
        failure_signals = kernel_failure_signals(relevant_new_kernel)

        telemetry_available = telemetry_before.get("available") or telemetry_after.get("available")
        report.matrix["telemetry"] = MatrixEntry(
            Status.PASS if telemetry_available else Status.UNAVAILABLE,
            telemetry_after.get("backend") or telemetry_before.get("backend") or "telemetry unavailable",
        )
        aer_status = Status(aer_assessment["status"])
        if aer_status is Status.FAIL:
            aer_detail = f"{len(aer_assessment['severe'])} new nonfatal/fatal counter(s)"
        elif aer_status is Status.WARN:
            aer_detail = f"{len(aer_assessment['correctable'])} new correctable counter(s)"
        else:
            aer_detail = "no positive counted error delta during Stage 3/4"
        report.matrix["aer"] = MatrixEntry(aer_status, aer_detail)
        if failure_signals:
            report.matrix["driver_init"] = MatrixEntry(
                Status.FAIL, "new kernel failure signal(s): " + ", ".join(failure_signals)
            )
            if "device_lost" in failure_signals and report.matrix["vram_correctness"].status is Status.PASS:
                report.matrix["vram_correctness"] = MatrixEntry(
                    Status.INCONCLUSIVE, "device lost signal prevents a VRAM-only verdict"
                )

        report.measurements["aer_after"] = aer_after
        report.measurements["aer_delta"] = aer_delta
        report.measurements["driver_bound"].update({
            "telemetry_after": telemetry_after,
            "vram": vram,
            "kernel_delta_available": kernel_delta_available,
            "kernel_before_source": kernel.get("source"),
            "kernel_after_source": kernel_after.get("source"),
            "kernel_new_relevant_lines": relevant_new_kernel,
            "kernel_failure_signals": failure_signals,
        })
        if relevant_new_kernel:
            report.observations.append({
                "level": "FAIL" if failure_signals else "WARN",
                "message": (
                    f"{len(relevant_new_kernel)} new relevant kernel line(s) in the driver-bound window; "
                    "see kernel sidecar."
                ),
            })
        if not kernel_delta_available:
            report.observations.append({
                "level": "WARN",
                "message": "Kernel before/after sources were unavailable or changed; no new-line attribution was attempted.",
            })
        if aer_assessment["correctable"]:
            report.observations.append({
                "level": "WARN",
                "message": "New correctable AER counters were observed and are not attributed as endpoint-fatal errors.",
            })
        if aer_assessment["severe"]:
            report.observations.append({
                "level": "FAIL",
                "message": "New nonfatal/fatal AER counters were observed; endpoint and upstream paths remain distinct in JSON.",
            })

        report.sidecars = write_driver_sidecars(
            writer,
            report.sidecars,
            kernel_before=kernel,
            kernel_after=kernel_after,
            vram_log=vram_log,
        )
        legacy_completed = vram.get("kind") == "LEGACY_SCREEN"
        report.interpretation = [
            "The target remained on the vendor-expected driver; gpu-triage performed no module or binding action.",
            (
                "Vulkan mapped exactly by full PCI/DRM identity."
                if vulkan.get("exact_match") else
                "Vulkan identity was not proven exactly; no legacy memory allocation was permitted."
            ),
            (
                "Legacy memtest results are a screen of the combined memory path, not physical-package attribution."
                if legacy_completed else
                "No legacy memory workload completed with a safely attributable result."
            ),
            "Physical VRAM package remains UNKNOWN.",
        ]
        report.overall = derive_overall(report.matrix, full_mode=False)
        report.stage = Stage.COMPLETE_FAIL if report.overall is Overall.FAIL else Stage.COMPLETE_INCOMPLETE
        report.stage_history.append(report.stage)
        json_path, markdown_path = writer.checkpoint(report)
        return report, json_path, markdown_path
    except Exception as exc:
        failed_stage = report.stage.value
        report.stage = Stage.ABORTED
        if not report.stage_history or report.stage_history[-1] is not Stage.ABORTED:
            report.stage_history.append(Stage.ABORTED)
        report.overall = Overall.INCOMPLETE
        report.observations.append({
            "level": "ERROR",
            "message": f"Run aborted during {failed_stage}: {type(exc).__name__}: {exc}",
        })
        checkpoint_detail = ""
        try:
            json_path, _ = writer.checkpoint(report)
            checkpoint_detail = f" Last checkpoint: {json_path}"
        except (OSError, ReportWriteError, ValueError) as checkpoint_exc:
            checkpoint_detail = f" Checkpoint persistence also failed: {checkpoint_exc}"
        if isinstance(exc, SafeTriageError):
            raise SafeTriageError(f"{exc}.{checkpoint_detail}") from exc
        raise SafeTriageError(
            f"Triage aborted during {failed_stage}: {type(exc).__name__}: {exc}.{checkpoint_detail}"
        ) from exc
