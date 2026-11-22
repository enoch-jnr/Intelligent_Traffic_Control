"""Tunable decision parameters (see docs/DECISION.md).

All values are starting points to be tuned on real footage, not calibrated
constants.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from decision.models import GREEN_TIME_MAX, GREEN_TIME_MIN


@dataclass(frozen=True)
class Weights:
    """Relative contribution of each demand signal."""

    queue: float = 1.0
    stationary: float = 1.5
    wait: float = 0.1
    congestion: float = 1.0


@dataclass(frozen=True)
class DecisionSettings:
    weights: Weights = field(default_factory=Weights)

    # green time = base + k * normalized_score, then clamped.
    # ``score_saturation`` is the demand score that counts as "fully loaded"
    # so green time tracks absolute demand, not just the winning share.
    base_green_s: float = 8.0
    k_green_s: float = 10.0
    score_saturation: float = 10.0
    min_green_s: int = GREEN_TIME_MIN
    max_green_s: int = GREEN_TIME_MAX

    # only request when the recommendation changed materially
    min_delta_s: int = 3
    min_interval_s: float = 8.0

    # fairness
    max_wait_before_force_s: float = 60.0

    # Clearance between two greens. Starting values that match the controller
    # README; they are likely to differ in final firmware.
    yellow_s: float = 3.0
    all_red_s: float = 1.0

    # Cap on how long a unit with demand may wait. ``max_red_s_by_unit``
    # overrides the scalar for named units (config accepts either form).
    max_red_s: float = 90.0
    max_red_s_by_unit: dict[str, float] = field(default_factory=dict)

    # How long a recommendation keeps counting as the expected green of its
    # unit before the default (base_green_s) is used instead.
    green_memory_s: float = 120.0

    # noise control
    smoothing_window_s: float = 5.0
    max_metrics_age_s: float = 2.0
    idle_score_threshold: float = 1.0

    # compat mode: how MAIN_A + MAIN_B demand becomes one MAIN demand
    aggregation: str = "sum"

    # health policy: by default any unhealthy approach suppresses output
    require_all_approaches_healthy: bool = True

    def __post_init__(self) -> None:
        if self.min_green_s < GREEN_TIME_MIN or self.max_green_s > GREEN_TIME_MAX:
            raise ValueError(
                f"green time must stay within the protocol range "
                f"{GREEN_TIME_MIN}-{GREEN_TIME_MAX} s"
            )
        if self.min_green_s > self.max_green_s:
            raise ValueError("min_green_s must not exceed max_green_s")
        if self.k_green_s < 0:
            raise ValueError("k_green_s must be non-negative")
        if self.score_saturation <= 0:
            raise ValueError("score_saturation must be positive")
        if self.min_interval_s < 0 or self.min_delta_s < 0:
            raise ValueError("interval and delta thresholds must be non-negative")
        if self.aggregation not in ("sum", "max"):
            raise ValueError("aggregation must be 'sum' or 'max'")
        if self.smoothing_window_s < 0:
            raise ValueError("smoothing_window_s must be non-negative")
        if self.yellow_s < 0 or self.all_red_s < 0:
            raise ValueError("yellow_s and all_red_s must be non-negative")
        if self.max_red_s <= 0:
            raise ValueError("max_red_s must be positive")
        for unit_id, cap in self.max_red_s_by_unit.items():
            if not isinstance(unit_id, str) or not unit_id:
                raise ValueError("max_red_s_by_unit keys must be unit ids")
            if cap <= 0:
                raise ValueError(
                    f"max_red_s for unit {unit_id!r} must be positive"
                )
        if self.green_memory_s < 0:
            raise ValueError("green_memory_s must be non-negative")


DEFAULT_SETTINGS = DecisionSettings()
