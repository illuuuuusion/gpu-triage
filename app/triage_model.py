"""Shared state and result types for safe GPU triage.

This module deliberately contains no operating-system access.  Keeping the
vocabulary separate makes it possible to test every transition without a GPU.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    NOT_RUN = "NOT_RUN"
    UNSAFE_SKIPPED = "UNSAFE_SKIPPED"
    UNAVAILABLE = "UNAVAILABLE"
    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"
    UNKNOWN = "UNKNOWN"


class Overall(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCOMPLETE = "INCOMPLETE"


class Stage(str, Enum):
    START = "START"
    S0_ENVIRONMENT = "S0_ENVIRONMENT"
    S1_PRE_DRIVER = "S1_PRE_DRIVER"
    COMPLETE_INCOMPLETE = "COMPLETE_INCOMPLETE"
    ABORTED = "ABORTED"


class DriverState(str, Enum):
    QUARANTINED_BDF = "QUARANTINED_BDF"
    INTENTIONAL_GLOBAL_BLACKLIST = "INTENTIONAL_GLOBAL_BLACKLIST"
    BOUND_EXPECTED = "BOUND_EXPECTED"
    BOUND_OTHER = "BOUND_OTHER"
    UNBOUND_UNEXPLAINED = "UNBOUND_UNEXPLAINED"
    DISPLAY_RISK = "DISPLAY_RISK"


@dataclass(frozen=True)
class MatrixEntry:
    status: Status
    detail: str = ""


@dataclass
class TriageReport:
    schema: int
    tool: str
    timestamp: str
    stage: Stage
    stage_history: list[Stage]
    target: dict[str, Any]
    environment: dict[str, Any]
    safety: dict[str, Any]
    measurements: dict[str, Any]
    observations: list[dict[str, Any]] = field(default_factory=list)
    interpretation: list[str] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    matrix: dict[str, MatrixEntry] = field(default_factory=dict)
    overall: Overall = Overall.INCOMPLETE
    sidecars: dict[str, str] = field(default_factory=dict)
    persistence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def derive_overall(matrix: dict[str, MatrixEntry], *, full_mode: bool) -> Overall:
    """Apply the three-value overall contract.

    A clear negative result wins.  PASS is reserved for a future full-mode run
    in which every required row completed successfully.
    """
    if any(item.status is Status.FAIL for item in matrix.values()):
        return Overall.FAIL
    if full_mode and matrix and all(item.status is Status.PASS for item in matrix.values()):
        return Overall.PASS
    return Overall.INCOMPLETE
