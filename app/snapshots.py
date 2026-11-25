"""Immutable views of the running host for the operator GUI.

These dataclasses contain only copied values or immutable tuples/mappings, so
the GUI can render a snapshot while the decision loop keeps running. They do not
import Qt and they do not expose the orchestrator itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional

from decision.models import ApproachMetrics


@dataclass(frozen=True)
class CameraView:
    camera_id: str
    approach: str
    source: str
    state: str
    fps: float
    frame_age_s: Optional[float]
    reconnects: int
    processed_fps: float = 0.0
    inference_ms: float = 0.0
    dropped_frames: int = 0
    effective_fps: float = 0.0
    overloaded: bool = False


@dataclass(frozen=True)
class UnitView:
    unit_id: str
    approaches: tuple[str, ...]
    score: Optional[float]
    expected_red_s: Optional[float]


@dataclass(frozen=True)
class WireLineView:
    """One exact JSON line sent to or received from the gateway."""

    at: str
    direction: str
    json: str


@dataclass(frozen=True)
class RecommendationView:
    """The last advisory recommendation and the controller's reply."""

    unit_id: str
    wire_movement: str
    green_time: int
    reasons: tuple[str, ...] = ()
    expected_red_s: Mapping[str, float] = field(default_factory=dict)
    sent: bool = False
    request_status: Optional[str] = None
    accepted: Optional[bool] = None
    latency_s: Optional[float] = None


@dataclass(frozen=True)
class UiSnapshot:
    """Everything a panel needs for one refresh, copied at one instant."""

    mode: str
    dry_run: bool
    three_phase: bool
    mock_gateway: bool
    simulation: bool
    gateway_state: str
    gateway_host: str
    gateway_port: int
    gate_open: bool
    gate_reason: str
    cameras: tuple[CameraView, ...]
    approaches: tuple[ApproachMetrics, ...]
    units: tuple[UnitView, ...]
    unit_scores: Mapping[str, float]
    expected_red_s: Mapping[str, float]
    recommendation: Optional[RecommendationView]
    decision_reasons: tuple[str, ...]
    started: bool
    held: bool
    released: bool
    uptime_s: float
    cpu_percent: Optional[float]
    memory_percent: Optional[float]
    log_path: str
    tick_count: int = 0
    last_tick_iso: Optional[str] = None
    session_started_iso: Optional[str] = None
    file_sources: bool = False
    performance_profile: str = "low"
    app_cpu_percent: Optional[float] = None
    app_memory_mb: Optional[float] = None

    @property
    def fresh_cameras(self) -> int:
        return sum(1 for camera in self.cameras if camera.state == "LIVE")

    @property
    def required_cameras(self) -> int:
        return len(self.cameras)

    def approach(self, approach_id: str) -> Optional[ApproachMetrics]:
        for metrics in self.approaches:
            if metrics.approach_id == approach_id:
                return metrics
        return None


def frozen_mapping(values: Mapping[str, float]) -> Mapping[str, float]:
    """Copy a mapping into a read-only proxy for a snapshot."""
    return MappingProxyType(dict(values))
