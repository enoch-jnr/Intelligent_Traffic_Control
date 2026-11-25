"""Pure translation between serving units and wire movements.

The decision engine speaks units; the gateway speaks movements. This module is
the only place that knows both, and it has no sockets, clocks or configuration
loading, so the mapping is cheap to test.

Two modes exist:

``compat_two_phase`` (default)
    The current firmware. Units are ``MAIN`` and ``SECONDARY`` and each unit id
    is also its wire movement. Approaches ``MAIN_A``/``MAIN_B`` share the
    ``MAIN`` unit and ``SEC`` is the ``SECONDARY`` unit. The controller cannot
    tell ``MAIN_A`` and ``MAIN_B`` apart.

``three_phase``
    The target T-junction layout. **The movement names below are proposals,
    not implemented firmware.** The current gateway rejects them. The mode must
    be enabled explicitly in configuration and is disabled by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from decision.models import Recommendation
from gateway.protocol import (
    GREEN_MAX,
    GREEN_MIN,
    MOVEMENTS,
    PRIORITY,
    ProtocolError,
    traffic_request_message,
)

COMPAT_MODE = "compat_two_phase"
THREE_PHASE_MODE = "three_phase"
MODES: tuple[str, ...] = (COMPAT_MODE, THREE_PHASE_MODE)

#: Unit ids that make up the T-junction target layout.
THREE_PHASE_UNITS: tuple[str, ...] = ("MAIN_A", "MAIN_B", "SEC")

#: Proposed wire names for the target layout. PROPOSALS ONLY: no firmware
#: accepts these today.
PROPOSED_THREE_PHASE_MOVEMENTS: Mapping[str, str] = {
    "MAIN_A": "MAIN_A",
    "MAIN_B": "MAIN_B",
    "SEC": "SECONDARY",
}


@dataclass(frozen=True)
class WireRequest:
    """One recommendation translated to the wire vocabulary."""

    unit_id: str
    movement: str
    green_time: int
    priority: str = PRIORITY


class ProtocolAdapter:
    """Maps unit ids to wire movements and builds protocol messages."""

    def __init__(
        self,
        mode: str,
        movements: Mapping[str, str],
        unit_ids: Sequence[str],
        *,
        three_phase_enabled: bool = False,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown gateway mode {mode!r}; expected {MODES}")
        if not unit_ids:
            raise ValueError("at least one unit is required")
        if len(set(unit_ids)) != len(unit_ids):
            raise ValueError(f"duplicate unit ids: {list(unit_ids)}")

        missing = [unit_id for unit_id in unit_ids if unit_id not in movements]
        if missing:
            raise ValueError(f"no wire movement configured for units: {missing}")

        self.mode = mode
        self.unit_ids: tuple[str, ...] = tuple(unit_ids)
        self.movements: dict[str, str] = {
            unit_id: str(movements[unit_id]) for unit_id in self.unit_ids
        }

        for unit_id, movement in self.movements.items():
            if not movement:
                raise ValueError(f"unit {unit_id!r} has an empty wire movement")
        if len(set(self.movements.values())) != len(self.movements):
            raise ValueError(
                f"wire movements must be unique, got {self.movements}"
            )

        if mode == COMPAT_MODE:
            unsupported = sorted(set(self.movements.values()) - set(MOVEMENTS))
            if unsupported:
                raise ValueError(
                    f"compat_two_phase cannot send {unsupported}; the current "
                    f"firmware accepts {MOVEMENTS}"
                )
        else:
            if not three_phase_enabled:
                raise ValueError(
                    "three_phase mode is disabled; set gateway.three_phase.enabled "
                    "to true and confirm the proposed movement names"
                )

    # -- mapping ----------------------------------------------------------

    @property
    def wire_movements(self) -> tuple[str, ...]:
        """Every movement this adapter may send, in unit order."""
        return tuple(self.movements[unit_id] for unit_id in self.unit_ids)

    def movement_for(self, unit_id: str) -> str:
        try:
            return self.movements[unit_id]
        except KeyError as exc:
            raise ProtocolError(
                f"unit {unit_id!r} has no wire movement in mode {self.mode!r}"
            ) from exc

    def to_wire(self, recommendation: Recommendation) -> WireRequest:
        """Translate a unit recommendation without touching the network."""
        movement = self.movement_for(recommendation.unit_id)
        if not GREEN_MIN <= recommendation.green_time <= GREEN_MAX:
            raise ProtocolError(
                f"green_time {recommendation.green_time} outside "
                f"{GREEN_MIN}-{GREEN_MAX}"
            )
        return WireRequest(
            unit_id=recommendation.unit_id,
            movement=movement,
            green_time=recommendation.green_time,
            priority=PRIORITY,
        )

    def build_message(
        self, recommendation: Recommendation, sequence: int
    ) -> dict:
        """Build the exact TRAFFIC_REQUEST that would be sent."""
        wire = self.to_wire(recommendation)
        return traffic_request_message(
            wire.movement,
            wire.green_time,
            sequence,
            movements=self.wire_movements,
        )

    # -- startup ----------------------------------------------------------

    def startup_warnings(self) -> tuple[str, ...]:
        if self.mode != THREE_PHASE_MODE:
            return ()
        names = ", ".join(self.wire_movements)
        return (
            "three_phase mode is enabled: sending proposed movements "
            f"{names}. These are NOT implemented in the current ESP32 gateway "
            "or Uno; requests will be rejected until firmware support lands.",
        )
