"""Fail-closed, provenance-bound ASIC channel/lane inference.

The native helper reports allocation-relative byte offsets and XOR bit
positions.  Those values have no generic physical meaning.  This module only
translates them when exactly one repository profile matches the complete ASIC
identity, declares the same input semantics, cites its mapping rules, and
passes its embedded known-fault fixtures.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


PROFILE_SCHEMA = 1
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
CONFIDENCE = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
ADDRESS_SPACES = {"allocation_relative", "virtual", "physical"}
SOURCE_KINDS = {"primary_source", "documented_experiment"}
EXPERIMENTS = {"host_transfer", "gpu_local_copy", "compute_kat", "vram_pattern"}


class ProfileValidationError(ValueError):
    """An ASIC profile cannot safely participate in inference."""


def unknown_inference(reason: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "UNKNOWN",
        "channel": "UNKNOWN",
        "lane": "UNKNOWN",
        "reason": reason,
        "mapping_profile": None,
        "mapping_version": None,
        "mapping_confidence": None,
        "confidence": None,
    }
    result.update(extra)
    return result


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileValidationError(f"{name} must be an object")
    return value


def _exact_keys(value: dict[str, Any], name: str, keys: set[str]) -> None:
    missing = keys - value.keys()
    extra = value.keys() - keys
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if extra:
            detail.append("unknown " + ", ".join(sorted(extra)))
        raise ProfileValidationError(f"{name}: {'; '.join(detail)}")


def _list(value: Any, name: str, *, nonempty: bool = True) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        suffix = "a non-empty array" if nonempty else "an array"
        raise ProfileValidationError(f"{name} must be {suffix}")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _integer(value: Any, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ProfileValidationError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ProfileValidationError(f"{name} must be <= {maximum}")
    return value


def _hex_id(value: Any, name: str, width: int) -> int:
    if not isinstance(value, str) or not re.fullmatch(rf"0x[0-9a-fA-F]{{{width}}}", value):
        raise ProfileValidationError(f"{name} must be 0x plus exactly {width} hexadecimal digits")
    return int(value, 16)


def _source_ids(value: Any, name: str, available: set[str]) -> list[str]:
    items = _list(value, name)
    if any(not isinstance(item, str) or item not in available for item in items):
        raise ProfileValidationError(f"{name} references an unknown source")
    if len(set(items)) != len(items):
        raise ProfileValidationError(f"{name} contains duplicate source IDs")
    return items


def _selector_value(selector: dict[str, Any], offset: int) -> int:
    value = 0
    for output_bit, group in enumerate(selector["bit_groups"]):
        parity = 0
        for input_bit in group:
            parity ^= (offset >> input_bit) & 1
        value |= parity << output_bit
    return value


def _map_case(mapping: dict[str, Any], offset: int, xor_bits: list[int]) -> tuple[list[str], list[str]]:
    channel_rule = mapping["channel"]
    lane_rule = mapping["lane"]
    channel_key = str(_selector_value(channel_rule, offset))
    channel = channel_rule["values"].get(channel_key)
    lanes = [lane_rule["values"].get(str(bit)) for bit in xor_bits]
    if channel is None or any(lane is None for lane in lanes):
        raise ProfileValidationError("mapping does not cover a known-fault fixture")
    return [channel], sorted(set(lanes))


def validate_profile(raw: Any, *, origin: str = "<memory>") -> dict[str, Any]:
    """Validate one declarative profile and execute its known-fault fixtures."""
    profile = _object(raw, origin)
    _exact_keys(profile, origin, {
        "schema", "profile_id", "mapping_version", "confidence", "asic",
        "input", "sources", "mapping", "known_fault_validation",
    })
    if profile.get("schema") != PROFILE_SCHEMA:
        raise ProfileValidationError(f"{origin}: schema must be {PROFILE_SCHEMA}")
    profile_id = _text(profile.get("profile_id"), f"{origin}.profile_id")
    if not PROFILE_ID_RE.fullmatch(profile_id):
        raise ProfileValidationError(f"{origin}.profile_id has an invalid format")
    version = _text(profile.get("mapping_version"), f"{origin}.mapping_version")
    if not SEMVER_RE.fullmatch(version):
        raise ProfileValidationError(f"{origin}.mapping_version must be semantic versioning")
    confidence = profile.get("confidence")
    if confidence not in CONFIDENCE:
        raise ProfileValidationError(f"{origin}.confidence must be LOW, MEDIUM or HIGH")

    asic = _object(profile.get("asic"), f"{origin}.asic")
    _exact_keys(asic, f"{origin}.asic", {"vendor_id", "device_id", "revision_ids"})
    vendor_id = _hex_id(asic.get("vendor_id"), f"{origin}.asic.vendor_id", 4)
    device_id = _hex_id(asic.get("device_id"), f"{origin}.asic.device_id", 4)
    revisions = [
        _hex_id(item, f"{origin}.asic.revision_ids", 2)
        for item in _list(asic.get("revision_ids"), f"{origin}.asic.revision_ids")
    ]
    if len(set(revisions)) != len(revisions):
        raise ProfileValidationError(f"{origin}.asic.revision_ids contains duplicates")

    input_semantics = _object(profile.get("input"), f"{origin}.input")
    _exact_keys(input_semantics, f"{origin}.input", {
        "address_space", "offset_unit_bytes", "word_width_bits", "definition",
        "helper_schema", "helper_version", "pattern_version", "experiments",
    })
    address_space = input_semantics.get("address_space")
    if address_space not in ADDRESS_SPACES:
        raise ProfileValidationError(f"{origin}.input.address_space is invalid")
    offset_unit_bytes = _integer(
        input_semantics.get("offset_unit_bytes"), f"{origin}.input.offset_unit_bytes", minimum=1
    )
    word_width_bits = _integer(
        input_semantics.get("word_width_bits"), f"{origin}.input.word_width_bits", minimum=1, maximum=64
    )
    _integer(input_semantics.get("helper_schema"), f"{origin}.input.helper_schema", minimum=1)
    helper_version = _text(input_semantics.get("helper_version"), f"{origin}.input.helper_version")
    if not SEMVER_RE.fullmatch(helper_version):
        raise ProfileValidationError(f"{origin}.input.helper_version must be semantic versioning")
    _integer(input_semantics.get("pattern_version"), f"{origin}.input.pattern_version", minimum=1)
    applicable_experiments = _list(input_semantics.get("experiments"), f"{origin}.input.experiments")
    if any(item not in EXPERIMENTS for item in applicable_experiments):
        raise ProfileValidationError(f"{origin}.input.experiments names an unsupported experiment")
    if len(set(applicable_experiments)) != len(applicable_experiments):
        raise ProfileValidationError(f"{origin}.input.experiments contains duplicates")
    _text(input_semantics.get("definition"), f"{origin}.input.definition")

    sources = _list(profile.get("sources"), f"{origin}.sources")
    source_ids: set[str] = set()
    for number, item in enumerate(sources):
        source = _object(item, f"{origin}.sources[{number}]")
        _exact_keys(source, f"{origin}.sources[{number}]", {"id", "kind", "title", "locator"})
        source_id = _text(source.get("id"), f"{origin}.sources[{number}].id")
        if source_id in source_ids:
            raise ProfileValidationError(f"{origin}.sources contains duplicate IDs")
        source_ids.add(source_id)
        if source.get("kind") not in SOURCE_KINDS:
            raise ProfileValidationError(f"{origin}.sources[{number}].kind is invalid")
        _text(source.get("title"), f"{origin}.sources[{number}].title")
        _text(source.get("locator"), f"{origin}.sources[{number}].locator")

    mapping = _object(profile.get("mapping"), f"{origin}.mapping")
    _exact_keys(mapping, f"{origin}.mapping", {"channel", "lane"})
    channel = _object(mapping.get("channel"), f"{origin}.mapping.channel")
    _exact_keys(channel, f"{origin}.mapping.channel", {"source", "source_ids", "bit_groups", "values"})
    if channel.get("source") != "input_offset":
        raise ProfileValidationError(f"{origin}.mapping.channel.source must be input_offset")
    _source_ids(channel.get("source_ids"), f"{origin}.mapping.channel.source_ids", source_ids)
    groups = _list(channel.get("bit_groups"), f"{origin}.mapping.channel.bit_groups")
    if len(groups) > 6:
        raise ProfileValidationError(f"{origin}.mapping.channel supports at most six output bits")
    normalized_groups: list[list[int]] = []
    for number, group in enumerate(groups):
        bits = [
            _integer(bit, f"{origin}.mapping.channel.bit_groups[{number}]", maximum=63)
            for bit in _list(group, f"{origin}.mapping.channel.bit_groups[{number}]")
        ]
        if len(set(bits)) != len(bits):
            raise ProfileValidationError(f"{origin}.mapping.channel.bit_groups[{number}] has duplicates")
        normalized_groups.append(bits)
    values = _object(channel.get("values"), f"{origin}.mapping.channel.values")
    expected_keys = {str(value) for value in range(1 << len(groups))}
    if set(values) != expected_keys or any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise ProfileValidationError(f"{origin}.mapping.channel.values must cover every selector value")

    lane = _object(mapping.get("lane"), f"{origin}.mapping.lane")
    _exact_keys(lane, f"{origin}.mapping.lane", {"source", "source_ids", "values"})
    if lane.get("source") != "xor_bit_index":
        raise ProfileValidationError(f"{origin}.mapping.lane.source must be xor_bit_index")
    _source_ids(lane.get("source_ids"), f"{origin}.mapping.lane.source_ids", source_ids)
    lane_values = _object(lane.get("values"), f"{origin}.mapping.lane.values")
    expected_lane_keys = {str(value) for value in range(word_width_bits)}
    if set(lane_values) != expected_lane_keys or any(
        not isinstance(value, str) or not value.strip() for value in lane_values.values()
    ):
        raise ProfileValidationError(f"{origin}.mapping.lane.values must cover every word bit")

    validation = _object(profile.get("known_fault_validation"), f"{origin}.known_fault_validation")
    _exact_keys(validation, f"{origin}.known_fault_validation", {"source_ids", "cases"})
    validation_sources = _source_ids(
        validation.get("source_ids"), f"{origin}.known_fault_validation.source_ids", source_ids
    )
    cases = _list(validation.get("cases"), f"{origin}.known_fault_validation.cases")
    case_ids: set[str] = set()
    normalized_mapping = {
        "channel": {**channel, "bit_groups": normalized_groups},
        "lane": lane,
    }
    for number, item in enumerate(cases):
        case = _object(item, f"{origin}.known_fault_validation.cases[{number}]")
        _exact_keys(case, f"{origin}.known_fault_validation.cases[{number}]", {
            "id", "source_ids", "experiment", "offset", "xor_bits",
            "expected_channels", "expected_lanes",
        })
        case_id = _text(case.get("id"), f"{origin}.known_fault_validation.cases[{number}].id")
        if case_id in case_ids:
            raise ProfileValidationError(f"{origin}.known_fault_validation has duplicate case IDs")
        case_ids.add(case_id)
        _source_ids(
            case.get("source_ids"),
            f"{origin}.known_fault_validation.cases[{number}].source_ids",
            source_ids,
        )
        if case.get("experiment") not in applicable_experiments:
            raise ProfileValidationError(f"{origin}.known_fault_validation case uses an inapplicable experiment")
        offset = _integer(case.get("offset"), f"{origin}.known_fault_validation.cases[{number}].offset")
        if offset % offset_unit_bytes:
            raise ProfileValidationError(f"{origin}.known_fault_validation case offset is not unit-aligned")
        xor_bits = [
            _integer(bit, f"{origin}.known_fault_validation.cases[{number}].xor_bits", maximum=word_width_bits - 1)
            for bit in _list(case.get("xor_bits"), f"{origin}.known_fault_validation.cases[{number}].xor_bits")
        ]
        if len(set(xor_bits)) != len(xor_bits):
            raise ProfileValidationError(f"{origin}.known_fault_validation case has duplicate XOR bits")
        expected_channels = sorted(
            _text(item, f"{origin}.known_fault_validation.expected_channels")
            for item in _list(case.get("expected_channels"), f"{origin}.known_fault_validation.expected_channels")
        )
        expected_lanes = sorted(
            _text(item, f"{origin}.known_fault_validation.expected_lanes")
            for item in _list(case.get("expected_lanes"), f"{origin}.known_fault_validation.expected_lanes")
        )
        actual_channels, actual_lanes = _map_case(normalized_mapping, offset, xor_bits)
        if actual_channels != expected_channels or actual_lanes != expected_lanes:
            raise ProfileValidationError(f"{origin}: known-fault fixture {case_id} does not match the rules")

    normalized = dict(profile)
    normalized.update({
        "_vendor_id": vendor_id,
        "_device_id": device_id,
        "_revision_ids": revisions,
        "_mapping": normalized_mapping,
        "_source_ids": sorted(source_ids),
        "_validation_source_ids": validation_sources,
        "_validation_cases": len(cases),
    })
    return normalized


def load_profiles(profile_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Load every production profile; one bad file invalidates the catalog use."""
    profiles: list[dict[str, Any]] = []
    errors: list[str] = []
    if not profile_dir.is_dir():
        return profiles, errors
    for path in sorted(profile_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            profiles.append(validate_profile(raw, origin=path.name))
        except (OSError, json.JSONDecodeError, ProfileValidationError) as exc:
            errors.append(f"{path.name}: {exc}")
    identities = [(item["profile_id"], item["mapping_version"]) for item in profiles]
    if len(set(identities)) != len(identities):
        errors.append("duplicate profile_id/mapping_version in ASIC catalog")
    return profiles, errors


def infer_channel_lane(
    profile_dir: Path,
    *,
    vendor_id: int,
    device_id: int,
    revision: int | None,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Return bounded hypotheses or literal UNKNOWN; never guess a mapping."""
    profiles, errors = load_profiles(profile_dir)
    if errors:
        return unknown_inference("INVALID_ASIC_PROFILE_CATALOG", profile_errors=errors[:8])
    matching = [
        item for item in profiles
        if item["_vendor_id"] == vendor_id
        and item["_device_id"] == device_id
        and revision in item["_revision_ids"]
    ]
    if not matching:
        return unknown_inference("NO_VALIDATED_ASIC_PROFILE")
    if len(matching) != 1:
        return unknown_inference(
            "AMBIGUOUS_ASIC_PROFILE",
            matching_profiles=[f"{item['profile_id']}@{item['mapping_version']}" for item in matching],
        )
    profile = matching[0]
    profile_info = {
        "id": profile["profile_id"],
        "version": profile["mapping_version"],
        "confidence": profile["confidence"],
        "source_ids": profile["_source_ids"],
        "sources": [
            {
                "id": source["id"],
                "kind": source["kind"],
                "title": source["title"],
                "locator": source["locator"],
            }
            for source in profile["sources"]
        ],
        "known_fault_cases": profile["_validation_cases"],
        "known_fault_source_ids": profile["_validation_source_ids"],
    }
    input_semantics = profile["input"]
    meta = evidence.get("meta", {})
    if evidence.get("kind") != "PHASE4_HELPER":
        return unknown_inference("UNSUPPORTED_EVIDENCE_SOURCE", mapping_profile=profile_info)
    if (
        meta.get("offset_space") != input_semantics["address_space"]
        or meta.get("offset_unit_bytes") != input_semantics["offset_unit_bytes"]
        or meta.get("schema") != input_semantics["helper_schema"]
        or meta.get("version") != input_semantics["helper_version"]
        or meta.get("pattern_version") != input_semantics["pattern_version"]
    ):
        return unknown_inference("INPUT_SEMANTICS_MISMATCH", mapping_profile=profile_info)
    identity = evidence.get("identity", {})
    if (
        identity.get("exact_match") is not True
        or identity.get("vendor_id") != vendor_id
        or identity.get("device_id") != device_id
    ):
        return unknown_inference("EXACT_HELPER_IDENTITY_NOT_PROVEN", mapping_profile=profile_info)
    if evidence.get("device_lost"):
        return unknown_inference("DEVICE_LOST_INVALIDATES_INFERENCE", mapping_profile=profile_info)
    all_records = evidence.get("error_records")
    if not isinstance(all_records, list) or not all_records:
        return unknown_inference("NO_ERROR_EVIDENCE", mapping_profile=profile_info)
    records = [record for record in all_records if record.get("experiment") in input_semantics["experiments"]]
    if not records:
        return unknown_inference("NO_APPLICABLE_ERROR_EVIDENCE", mapping_profile=profile_info)

    channels: Counter[str] = Counter()
    lanes: Counter[str] = Counter()
    mapping = profile["_mapping"]
    word_width = input_semantics["word_width_bits"]
    try:
        for number, record in enumerate(records):
            if record.get("width_bits") != word_width:
                raise ProfileValidationError(f"error record {number} has a different word width")
            offset = _integer(record.get("offset"), f"error_records[{number}].offset")
            if offset % input_semantics["offset_unit_bytes"]:
                raise ProfileValidationError(f"error record {number} is not unit-aligned")
            xor_bits = sorted(set(record.get("bits_0_to_1", []) + record.get("bits_1_to_0", [])))
            if not xor_bits:
                raise ProfileValidationError(f"error record {number} has no XOR bits")
            mapped_channels, mapped_lanes = _map_case(mapping, offset, xor_bits)
            channels.update(mapped_channels)
            lanes.update(mapped_lanes)
    except (AttributeError, TypeError, ProfileValidationError) as exc:
        return unknown_inference("EVIDENCE_NOT_MAPPABLE", mapping_profile=profile_info, detail=str(exc))

    summary = evidence.get("error_summary", {})
    reproducible = summary.get("reproducible", {}) if isinstance(summary, dict) else {}
    repeated = any(isinstance(value, int) and value > 0 for value in reproducible.values())
    effective_confidence = profile["confidence"] if repeated else "LOW"
    total = summary.get("total") if isinstance(summary.get("total"), int) else len(records)
    sorted_channels = sorted(channels.items(), key=lambda item: (-item[1], item[0]))
    sorted_lanes = sorted(lanes.items(), key=lambda item: (-item[1], item[0]))
    return {
        "status": "HYPOTHESIS",
        "channel": ", ".join(name for name, _count in sorted_channels) if channels else "UNKNOWN",
        "lane": ", ".join(name for name, _count in sorted_lanes) if lanes else "UNKNOWN",
        "channels": [
            {"name": name, "records": count}
            for name, count in sorted_channels
        ],
        "lanes": [
            {"name": name, "bit_errors": count}
            for name, count in sorted_lanes
        ],
        "reason": "VALIDATED_PROFILE_APPLIED",
        "mapping_profile": profile_info,
        "mapping_version": profile["mapping_version"],
        "mapping_confidence": profile["confidence"],
        "confidence": effective_confidence,
        "evidence": {
            "records_used": len(records),
            "records_available": len(all_records),
            "total_errors": total,
            "record_coverage_complete": total == len(records),
            "reproducible": repeated,
            "offset_space": meta.get("offset_space"),
        },
        "limitation": "Channel/lane is a profile-bound hypothesis; it is not a physical package identification.",
    }
