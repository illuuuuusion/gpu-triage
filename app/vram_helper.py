"""Protocol and runner for the native Phase-4 Vulkan helper.

The helper owns physical-device enumeration and exact BDF matching.  This
module treats its JSONL as untrusted input: a malformed or incomplete stream
can never turn into PASS and is retained as a bounded sidecar by reporting.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from collectors import PciTarget


SCHEMA = 1
MAX_JSONL_BYTES = 16 * 1024 * 1024
EXPERIMENTS = ("host_transfer", "gpu_local_copy", "compute_kat", "vram_pattern")
VALID_STATUS = {"PASS", "FAIL", "INCONCLUSIVE", "UNAVAILABLE", "BLOCKED"}
FULL_BDF_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
Runner = Callable[[list[str], float], dict[str, Any]]


class HelperProtocolError(ValueError):
    pass


def find_helper(repo_root: Path | None = None) -> str | None:
    override = os.environ.get("GPU_TRIAGE_VRAM_HELPER")
    if override:
        path = Path(override).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    candidates: list[Path] = []
    if repo_root is not None:
        candidates.extend([
            repo_root / "offline/helper/gpu-triage-vram-helper",
            repo_root / "vram-helper/build/gpu-triage-vram-helper",
        ])
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("gpu-triage-vram-helper")


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise HelperProtocolError(f"{name} must be an integer >= {minimum}")
    return value


def parse_helper_jsonl(
    text: str,
    *,
    target: PciTarget,
    max_error_records: int,
) -> dict[str, Any]:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) > MAX_JSONL_BYTES:
        raise HelperProtocolError("helper output exceeds the 16 MiB protocol limit")
    events: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HelperProtocolError(f"invalid JSON on helper line {number}: {exc.msg}") from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise HelperProtocolError(f"helper line {number} is not a typed object")
        events.append(event)
    if not events:
        raise HelperProtocolError("helper emitted no JSONL events")

    metas = [item for item in events if item["type"] == "meta"]
    identities = [item for item in events if item["type"] == "identity"]
    summaries = [item for item in events if item["type"] == "summary"]
    if len(metas) != 1 or len(identities) != 1 or len(summaries) != 1:
        raise HelperProtocolError("helper stream requires exactly one meta, identity and summary event")
    meta, identity, summary = metas[0], identities[0], summaries[0]
    if meta.get("schema") != SCHEMA or meta.get("helper") != "gpu-triage-vram-helper":
        raise HelperProtocolError("unsupported helper schema or identity")
    expected_ids = (target.vendor_id, target.device_id)
    actual_ids = (identity.get("vendor_id"), identity.get("device_id"))
    if (
        identity.get("exact_match") is not True
        or identity.get("bdf") != target.bdf
        or actual_ids != expected_ids
        or identity.get("mapping_source") not in {"VK_EXT_pci_bus_info", "VK_EXT_physical_device_drm"}
    ):
        raise HelperProtocolError("helper did not prove the exact target BDF and PCI IDs")

    error_events = [item for item in events if item["type"] == "error"]
    if len(error_events) > max_error_records:
        raise HelperProtocolError("helper exceeded the requested error-record limit")
    required_error = {
        "allocation", "offset", "width_bits", "expected", "actual", "xor",
        "bits_0_to_1", "bits_1_to_0", "pattern", "seed", "pass", "reread",
        "timestamp_ms",
    }
    for event in error_events:
        missing = required_error - event.keys()
        if missing:
            raise HelperProtocolError("error record lacks " + ", ".join(sorted(missing)))
        _integer(event["allocation"], "allocation")
        _integer(event["offset"], "offset")
        _integer(event["width_bits"], "width_bits", minimum=1)
        _integer(event["seed"], "seed")
        _integer(event["pass"], "pass")
        _integer(event["reread"], "reread")
        _integer(event["timestamp_ms"], "timestamp_ms")
        if not isinstance(event["bits_0_to_1"], list) or not isinstance(event["bits_1_to_0"], list):
            raise HelperProtocolError("directional bit fields must be arrays")

    experiment_events = [item for item in events if item["type"] == "experiment"]
    experiments: dict[str, dict[str, Any]] = {}
    for event in experiment_events:
        name, status = event.get("name"), event.get("status")
        if name not in EXPERIMENTS or status not in VALID_STATUS or name in experiments:
            raise HelperProtocolError("invalid or duplicate experiment result")
        experiments[name] = event
    if set(experiments) != set(EXPERIMENTS):
        raise HelperProtocolError("helper did not report all four independent experiments")
    if summary.get("status") not in VALID_STATUS:
        raise HelperProtocolError("invalid helper summary status")
    if summary.get("experiments") != {name: experiments[name]["status"] for name in EXPERIMENTS}:
        raise HelperProtocolError("summary/experiment status mismatch")
    totals = summary.get("error_summary")
    if not isinstance(totals, dict):
        raise HelperProtocolError("summary lacks structured error aggregation")
    total_errors = _integer(totals.get("total"), "error_summary.total")
    recorded = _integer(totals.get("recorded"), "error_summary.recorded")
    if recorded != len(error_events) or recorded > total_errors:
        raise HelperProtocolError("error summary counts do not match the stream")
    limits = summary.get("limits")
    if not isinstance(limits, dict):
        raise HelperProtocolError("summary lacks applied limits")
    for name in ("seconds", "bytes", "max_error_records"):
        _integer(limits.get(name), f"limits.{name}", minimum=1)
    temperature = summary.get("temperature")
    if not isinstance(temperature, dict) or temperature.get("status") not in {"PASS", "UNAVAILABLE", "LIMIT_REACHED"}:
        raise HelperProtocolError("temperature state is missing or invalid")

    return {
        "status": summary["status"],
        "kind": "PHASE4_HELPER",
        "meta": meta,
        "identity": identity,
        "experiments": experiments,
        "error_records": error_events,
        "error_summary": totals,
        "limits": limits,
        "temperature": temperature,
        "device_lost": bool(summary.get("device_lost")),
    }


def run_vram_helper(
    target: PciTarget,
    *,
    seconds: int,
    max_bytes: int,
    max_error_records: int,
    max_vram_percent: int,
    max_temp_mc: int | None,
    log_path: Path,
    run_command: Runner,
    executable: str,
) -> dict[str, Any]:
    if not FULL_BDF_RE.fullmatch(target.bdf):
        return {"status": "BLOCKED", "reason": "INVALID_TARGET_BDF", "kind": "PHASE4_HELPER"}
    command = [
        executable,
        "--gpu", target.bdf,
        "--vendor", f"0x{target.vendor_id:04x}",
        "--device", f"0x{target.device_id:04x}",
        "--seconds", str(seconds),
        "--max-bytes", str(max_bytes),
        "--max-errors", str(max_error_records),
        "--max-vram-percent", str(max_vram_percent),
    ]
    if max_temp_mc is not None:
        command += ["--max-temp-mc", str(max_temp_mc)]
    result = run_command(command, seconds + 30)
    output = result.get("output", "")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output[:MAX_JSONL_BYTES], encoding="utf-8")
    try:
        parsed = parse_helper_jsonl(output, target=target, max_error_records=max_error_records)
    except HelperProtocolError as exc:
        return {
            "status": "INCONCLUSIVE" if result.get("rc") is not None else "UNAVAILABLE",
            "reason": f"HELPER_PROTOCOL_ERROR: {exc}",
            "kind": "PHASE4_HELPER",
            "command": {key: result.get(key) for key in ("cmd", "rc", "seconds", "timeout")},
        }
    limits = parsed["limits"]
    if (
        limits["seconds"] != seconds
        or limits["bytes"] > max_bytes
        or limits["max_error_records"] != max_error_records
        or limits.get("max_vram_percent") != max_vram_percent
    ):
        parsed["status"] = "INCONCLUSIVE"
        parsed["reason"] = "HELPER_LIMIT_CONTRACT_MISMATCH"
    parsed["command"] = {key: result.get(key) for key in ("cmd", "rc", "seconds", "timeout")}
    if parsed["device_lost"]:
        parsed["status"] = "INCONCLUSIVE"
        parsed["reason"] = "VULKAN_DEVICE_LOST"
    elif result.get("rc") not in (0, 1):
        parsed["status"] = "INCONCLUSIVE"
        parsed["reason"] = f"HELPER_EXIT_{result.get('rc')}"
    return parsed
