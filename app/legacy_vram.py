"""Strictly gated adapter for the transitional memtest_vulkan screen."""

from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


MEMTEST_ENV = "GPU_TRIAGE_MEMTEST"
MAX_CAPTURE_CHARS = 2 * 1024 * 1024
MAX_WORK_LOG_CHARS = 8 * 1024 * 1024


def find_memtest() -> str | None:
    override = os.environ.get(MEMTEST_ENV)
    if override:
        path = Path(override).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    for name in ("memtest_vulkan", "memtest_vulkan_verbose"):
        found = shutil.which(name)
        if found:
            return found
    return None


def missing_reason() -> str:
    override = os.environ.get(MEMTEST_ENV)
    if override:
        return f"{MEMTEST_ENV}={override!r} is not an executable file"
    return "memtest_vulkan not found on PATH; prepare the driver-bound runtime profile"


def parse_device(line: str) -> tuple[int, str] | None:
    match = re.search(r"^\s*(\d+):\s+Bus=0x([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})\b", line)
    if not match:
        return None
    return int(match.group(1)), f"{match.group(2).lower()}:{match.group(3).lower()}"


def _bus_device(bdf: str) -> str:
    match = re.fullmatch(r"[0-9a-f]{4}:([0-9a-f]{2}):([0-9a-f]{2})\.[0-7]", bdf)
    return f"{match.group(1)}:{match.group(2)}" if match else bdf


def run_legacy_memtest(
    target_bdf: str,
    seconds: int,
    log_path: Path,
    mapping: dict[str, Any],
) -> dict[str, Any]:
    """Run memtest only after a full-BDF singleton Vulkan proof.

    memtest_vulkan exposes only bus:device.  The proof is therefore deliberately
    stricter than its own selector: the same Vulkan/ICD view must contain one
    hardware PhysicalDevice and that device must already have matched the full
    domain:bus:device.function plus PCI IDs.
    """
    if not (
        re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", target_bdf)
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
    executable = find_memtest()
    if not executable:
        return {"status": "UNAVAILABLE", "reason": missing_reason()}

    target_key = _bus_device(target_bdf)
    started = time.monotonic()
    output_tail = ""
    parse_buffer = ""
    selected = False
    selected_index: int | None = None
    selection_sent = False
    listed: dict[int, str] = {}
    target_matches: list[int] = []
    unsafe_prompt = False
    test_deadline: float | None = None
    logged_chars = 0
    log_truncated = False
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        try:
            process = subprocess.Popen(
                [executable], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=False, bufsize=0, start_new_session=True,
            )
        except OSError as exc:
            return {"status": "UNAVAILABLE", "reason": str(exc)}
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        init_deadline = started + 25
        interrupted = False

        def consume(data: str) -> None:
            nonlocal parse_buffer, selected, selected_index, selection_sent
            nonlocal unsafe_prompt, test_deadline
            nonlocal output_tail, logged_chars, log_truncated
            if not data:
                return
            output_tail = (output_tail + data)[-MAX_CAPTURE_CHARS:]
            if logged_chars < MAX_WORK_LOG_CHARS:
                writable = data[: MAX_WORK_LOG_CHARS - logged_chars]
                log.write(writable)
                logged_chars += len(writable)
                if len(writable) < len(data):
                    log.write("\n[gpu-triage: live memtest log truncated]\n")
                    log_truncated = True
            elif not log_truncated:
                log.write("\n[gpu-triage: live memtest log truncated]\n")
                log_truncated = True
            log.flush()
            parse_buffer += data
            lines = parse_buffer.split("\n")
            parse_buffer = lines.pop()
            for line in lines:
                parsed = parse_device(line)
                if parsed:
                    index, address = parsed
                    listed[index] = address
                    if address == target_key and index not in target_matches:
                        target_matches.append(index)
                if line.startswith("Testing ") or "Standard 5-minute test of" in line:
                    selected = True
                    test_deadline = test_deadline or time.monotonic() + max(1, seconds)
            if "Override index to test:" in parse_buffer or "Override index to test:" in data:
                if not selection_sent and len(listed) == 1 and len(target_matches) == 1 and process.stdin:
                    try:
                        process.stdin.write(f"{target_matches[0]}\n".encode())
                        process.stdin.flush()
                        selected_index = target_matches[0]
                        selection_sent = True
                    except (BrokenPipeError, OSError):
                        unsafe_prompt = True
                elif not selection_sent:
                    unsafe_prompt = True

        while process.poll() is None:
            now = time.monotonic()
            if unsafe_prompt or now >= (test_deadline if test_deadline is not None else init_deadline):
                try:
                    os.killpg(process.pid, signal.SIGINT)
                    interrupted = True
                except ProcessLookupError:
                    pass
                break
            for key, _ in selector.select(timeout=0.25):
                try:
                    raw = os.read(key.fileobj.fileno(), 65536)
                except OSError:
                    raw = b""
                consume(raw.decode(errors="replace"))
        try:
            tail, _ = process.communicate(timeout=10 if interrupted else 3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                tail, _ = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                tail, _ = process.communicate()
        if tail:
            consume(tail.decode(errors="replace") if isinstance(tail, bytes) else tail)

    output = output_tail
    lowered = output.lower()
    reason = None
    if len(listed) != 1 or len(target_matches) != 1 or selected_index is None:
        status = "INCONCLUSIVE"
        reason = "LEGACY_DEVICE_LIST_DID_NOT_PRESERVE_SINGLETON_MAPPING"
    elif "vk_error_device_lost" in lowered or "device lost" in lowered:
        status = "INCONCLUSIVE"
        reason = "VULKAN_DEVICE_LOST"
    elif any(word in lowered for word in ("early exit", "runtime error", "initialization_failed", "incompatible_driver")):
        status = "INCONCLUSIVE"
        reason = "LEGACY_RUNTIME_FAILURE"
    elif not selected:
        status = "INCONCLUSIVE"
        reason = "LEGACY_TEST_NOT_STARTED"
    elif "error found" in lowered:
        status = "FAIL"
    elif "no any errors" in lowered and "passed" in lowered:
        status = "PASS"
    else:
        status = "INCONCLUSIVE"
        reason = "LEGACY_RESULT_NOT_RECOGNIZED"
    return {
        "status": status,
        "reason": reason,
        "kind": "LEGACY_SCREEN",
        "seconds": round(time.monotonic() - started, 2),
        "target_bdf": target_bdf,
        "selected_index": selected_index,
        "devices_seen": listed,
        "error_summaries": [
            line for line in output.splitlines()
            if "Error found" in line or "Errors address range" in line
        ][:50],
        "log": str(log_path),
        "log_truncated": log_truncated,
    }
