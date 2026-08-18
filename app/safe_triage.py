"""Stage-0/1 safe-triage orchestration."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Any

from collectors import ReadOnlyCollector, SUPPORTED_VENDORS, positive_counter_paths
from reporting import CheckpointWriter, ReportWriteError, validate_report_directory, write_sidecars
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


def run_pre_driver_triage(
    *,
    gpu_arg: str | None,
    report_dir_arg: str | None,
    repo_root: Path,
    collector: ReadOnlyCollector | None = None,
) -> tuple[TriageReport, Path, Path]:
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
        schema=2,
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
        report.overall = derive_overall(matrix, full_mode=False)
        report.stage = Stage.COMPLETE_INCOMPLETE
        report.stage_history.append(Stage.COMPLETE_INCOMPLETE)
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
