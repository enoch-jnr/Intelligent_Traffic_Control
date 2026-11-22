"""Data models shared by the pipeline, decision engine and logging.

These types are deliberately I/O-free so the decision engine can be tested
without cameras, network or time sleeps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

# Congestion contributes to demand on this scale.
CONGESTION_WEIGHTS: dict[str, float] = {"Low": 0.0, "Moderate": 1.0, "High": 2.0}

#: Movements the current firmware accepts. No other value is sent by default.
WIRE_MOVEMENTS: tuple[str, ...] = ("MAIN", "SECONDARY")

#: Version of the approach/unit layout written to new decision-log records.
APPROACH_LAYOUT: str = "v2"

#: Hard protocol range for ``green_time``.
GREEN_TIME_MIN = 5
GREEN_TIME_MAX = 30


@dataclass(frozen=True)
class ApproachMetrics:
    """One approach's perception snapshot, published at ~1 Hz."""

    approach_id: str
    camera_id: str
    timestamp: float
    healthy: bool
    vehicle_count_in_region: int = 0
    stationary_count: int = 0
    crossings_last_window: int = 0
    congestion_level: str = "Low"
    mean_wait_s: float = 0.0
    max_wait_s: float = 0.0
    frame_age_s: float = 0.0
    frame_index: int = 0
    processing_fps: float = 0.0
    latency_s: float = 0.0
    inference_s: float = 0.0

    def to_log_dict(self) -> dict:
        return {
            "approach_id": self.approach_id,
            "camera_id": self.camera_id,
            "timestamp": round(self.timestamp, 3),
            "healthy": self.healthy,
            "vehicle_count_in_region": self.vehicle_count_in_region,
            "stationary_count": self.stationary_count,
            "crossings_last_window": self.crossings_last_window,
            "congestion_level": self.congestion_level,
            "mean_wait_s": self.mean_wait_s,
            "max_wait_s": self.max_wait_s,
            "frame_age_s": self.frame_age_s,
            "frame_index": self.frame_index,
            "processing_fps": self.processing_fps,
            "latency_s": self.latency_s,
            "inference_s": self.inference_s,
        }

    def with_health(self, healthy: bool, frame_age_s: float) -> "ApproachMetrics":
        return ApproachMetrics(
            approach_id=self.approach_id,
            camera_id=self.camera_id,
            timestamp=self.timestamp,
            healthy=healthy,
            vehicle_count_in_region=self.vehicle_count_in_region,
            stationary_count=self.stationary_count,
            crossings_last_window=self.crossings_last_window,
            congestion_level=self.congestion_level,
            mean_wait_s=self.mean_wait_s,
            max_wait_s=self.max_wait_s,
            frame_age_s=round(frame_age_s, 3),
            frame_index=self.frame_index,
            processing_fps=self.processing_fps,
            latency_s=self.latency_s,
            inference_s=self.inference_s,
        )


@dataclass(frozen=True)
class ServingUnit:
    """The thing green time is allocated to: one or more approaches.

    A unit is a decision-engine concept only. Whether its id is also a wire
    movement is decided by the protocol adapter, not by the engine.
    """

    id: str
    approaches: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("a serving unit needs a non-empty id")
        if not self.approaches:
            raise ValueError(f"serving unit {self.id!r} has no approaches")

    @classmethod
    def from_config(cls, entry: Mapping) -> "ServingUnit":
        """Build from a validated config entry (``{id, approaches}``)."""
        return cls(
            id=str(entry["id"]),
            approaches=tuple(str(item) for item in entry["approaches"]),
        )

    def to_log_dict(self) -> dict:
        return {"id": self.id, "approaches": list(self.approaches)}


@dataclass(frozen=True)
class Recommendation:
    """A green-time recommendation. Advisory only: never a phase change."""

    unit_id: str
    green_time: int
    priority: str = "NORMAL"
    reasons: tuple[str, ...] = field(default_factory=tuple)
    scores: Mapping[str, float] = field(default_factory=dict)
    expected_red_s: Mapping[str, float] = field(default_factory=dict)

    def to_log_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "green_time": self.green_time,
            "priority": self.priority,
            "reasons": list(self.reasons),
            "scores": {key: round(value, 3) for key, value in self.scores.items()},
            "expected_red_s": {
                key: round(value, 1)
                for key, value in self.expected_red_s.items()
            },
        }
