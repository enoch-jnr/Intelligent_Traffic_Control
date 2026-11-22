"""Deterministic demand scoring and green-time recommendation.

Pure logic: no I/O, no clocks (``now`` is always supplied), no threads. The
orchestrator feeds it :class:`ApproachMetrics` and writes whatever it returns.

The engine allocates green to **serving units**, never to wire movements. A
unit is one or more named approaches; the layout comes from configuration
(three units for a three-phase T junction, two units with ``MAIN_A``/``MAIN_B``
merged for the current two-phase firmware). Nothing here knows or validates the
wire protocol names -- that is the protocol adapter's job at the edge.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

from decision.models import (
    CONGESTION_WEIGHTS,
    ApproachMetrics,
    Recommendation,
    ServingUnit,
)
from decision.settings import DecisionSettings, Weights

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    """Outcome of one evaluation cycle."""

    recommendation: Optional[Recommendation]
    reasons: tuple[str, ...]
    unit_scores: Mapping[str, float]
    expected_red_s: Mapping[str, float]
    healthy: bool

    @property
    def emitted(self) -> bool:
        return self.recommendation is not None


@dataclass
class _Sample:
    timestamp: float
    queue: float
    stationary: float
    crossings: float
    wait: float
    congestion: float


class DecisionEngine:
    """Turns per-approach metrics into at most one advisory recommendation."""

    def __init__(
        self,
        units: Sequence[ServingUnit],
        settings: Optional[DecisionSettings] = None,
    ) -> None:
        if not units:
            raise ValueError("at least one serving unit must be configured")

        self.units: dict[str, tuple[str, ...]] = {}
        seen: set[str] = set()
        for unit in units:
            if unit.id in self.units:
                raise ValueError(f"duplicate serving unit {unit.id!r}")
            for approach_id in unit.approaches:
                if approach_id in seen:
                    raise ValueError(
                        f"approach {approach_id!r} is assigned to more than one "
                        "serving unit"
                    )
                seen.add(approach_id)
            self.units[unit.id] = tuple(unit.approaches)

        self.approach_ids: tuple[str, ...] = tuple(seen)
        self.settings = settings or DecisionSettings()

        self._samples: dict[str, deque[_Sample]] = {
            approach_id: deque() for approach_id in self.approach_ids
        }
        self._started_at: Optional[float] = None
        self._last_served: dict[str, float] = {}
        self._last_request_at: Optional[float] = None
        self._last_green: Optional[int] = None
        self._last_unit: Optional[str] = None
        self._expected_green: dict[str, int] = {}
        self._expected_green_at: dict[str, float] = {}

    # -- public -----------------------------------------------------------

    def evaluate(
        self, metrics: Mapping[str, ApproachMetrics], now: float
    ) -> Decision:
        """Score units and return a decision for this instant."""
        if self._started_at is None:
            self._started_at = now
            for unit_id in self.units:
                self._last_served[unit_id] = now

        expected_red_s = self._expected_reds(now)
        healthy_ids, unhealthy_ids = self._classify_health(metrics, now)
        reasons: list[str] = []

        if unhealthy_ids:
            reasons.append("unhealthy approaches: " + ", ".join(sorted(unhealthy_ids)))
            if self.settings.require_all_approaches_healthy:
                reasons.append(
                    "policy: no recommendation while any approach is unhealthy"
                )
                return Decision(
                    None, tuple(reasons), {}, expected_red_s, healthy=False
                )

        if not healthy_ids:
            reasons.append("no healthy approaches")
            return Decision(
                None, tuple(reasons), {}, expected_red_s, healthy=False
            )

        healthy_set = set(healthy_ids)
        smoothed = {
            approach_id: self._smooth(metrics[approach_id], now)
            for approach_id in healthy_ids
        }

        scores: dict[str, float] = {}
        details: list[str] = []
        skipped: list[str] = []
        for unit_id, approach_ids in self.units.items():
            # Even in the degraded policy a unit is never scored from a subset
            # of its members: missing data is not zero traffic.
            if any(approach_id not in healthy_set for approach_id in approach_ids):
                skipped.append(unit_id)
                continue
            score, breakdown = self._score_unit(approach_ids, smoothed)
            scores[unit_id] = round(score, 3)
            details.append(f"{unit_id}={score:.2f} ({breakdown})")

        if skipped:
            reasons.append(
                "units with unhealthy approaches: " + ", ".join(skipped)
            )

        if not scores:
            reasons.append("no scored units")
            return Decision(
                None, tuple(reasons), {}, expected_red_s, healthy=False
            )

        if max(scores.values()) < self.settings.idle_score_threshold:
            reasons.append(
                f"idle: all unit scores below {self.settings.idle_score_threshold}"
            )
            return Decision(
                None, tuple(reasons), scores, expected_red_s, healthy=True
            )

        winner, forced = self._choose_winner(scores, now, expected_red_s)
        forced = forced and winner != self._last_unit
        if forced:
            cap = self._max_red_for(winner)
            if expected_red_s.get(winner, 0.0) > cap:
                reasons.append(
                    f"starvation guard: {winner} estimated red "
                    f"{expected_red_s[winner]:.1f}s exceeds cap {cap:.1f}s "
                    "with demand"
                )
            else:
                reasons.append(
                    f"starvation guard: {winner} waited more than "
                    f"{self.settings.max_wait_before_force_s:.0f}s with demand"
                )
        reasons.append("scores: " + "; ".join(details))

        green_time, clamp_note = self._green_time(scores[winner])
        reasons.append(clamp_note)

        blocked = self._change_gate(winner, green_time, now)
        if blocked is not None:
            reasons.append(blocked)
            return Decision(
                None, tuple(reasons), scores, expected_red_s, healthy=True
            )

        reasons.append(f"recommend {winner} green {green_time}s (advisory only)")
        expected_red_s = self._expected_reds(
            now, override=(winner, green_time)
        )
        recommendation = Recommendation(
            unit_id=winner,
            green_time=green_time,
            priority="NORMAL",
            reasons=tuple(reasons),
            scores=scores,
            expected_red_s=expected_red_s,
        )

        self._record_emission(winner, green_time, now)

        return Decision(
            recommendation=recommendation,
            reasons=tuple(reasons),
            unit_scores=scores,
            expected_red_s=expected_red_s,
            healthy=True,
        )

    def reset(self) -> None:
        """Forget smoothing and request history (used when the link restarts)."""
        for queue in self._samples.values():
            queue.clear()
        self._started_at = None
        self._last_served.clear()
        self._last_request_at = None
        self._last_green = None
        self._last_unit = None
        self._expected_green.clear()
        self._expected_green_at.clear()

    # -- health -----------------------------------------------------------

    def _classify_health(
        self, metrics: Mapping[str, ApproachMetrics], now: float
    ) -> tuple[list[str], list[str]]:
        healthy: list[str] = []
        unhealthy: list[str] = []

        for approach_id in self.approach_ids:
            snapshot = metrics.get(approach_id)
            if snapshot is None:
                unhealthy.append(approach_id)
                continue
            age = now - snapshot.timestamp
            if not snapshot.healthy or age > self.settings.max_metrics_age_s:
                unhealthy.append(approach_id)
            else:
                healthy.append(approach_id)
        return healthy, unhealthy

    # -- smoothing and scoring -------------------------------------------

    def _smooth(self, snapshot: ApproachMetrics, now: float) -> _Sample:
        sample = _Sample(
            timestamp=now,
            queue=float(snapshot.vehicle_count_in_region),
            stationary=float(snapshot.stationary_count),
            crossings=float(snapshot.crossings_last_window),
            wait=float(max(0.0, snapshot.max_wait_s)),
            congestion=CONGESTION_WEIGHTS.get(snapshot.congestion_level, 0.0),
        )
        queue = self._samples[snapshot.approach_id]
        queue.append(sample)

        window = self.settings.smoothing_window_s
        if window <= 0:
            queue.clear()
            return sample
        cutoff = now - window
        while queue and queue[0].timestamp < cutoff:
            queue.popleft()
        if not queue:
            return sample

        count = len(queue)
        return _Sample(
            timestamp=now,
            queue=sum(item.queue for item in queue) / count,
            stationary=sum(item.stationary for item in queue) / count,
            crossings=sum(item.crossings for item in queue) / count,
            wait=max(item.wait for item in queue),
            congestion=sum(item.congestion for item in queue) / count,
        )

    def _score_unit(
        self, approach_ids: Iterable[str], smoothed: Mapping[str, _Sample]
    ) -> tuple[float, str]:
        samples = [smoothed[approach_id] for approach_id in approach_ids]
        aggregate = self._aggregate
        weights: Weights = self.settings.weights

        queue = aggregate([sample.queue for sample in samples])
        stationary = aggregate([sample.stationary for sample in samples])
        congestion = aggregate([sample.congestion for sample in samples])
        # Waiting is a max-of-unit signal: one long-waiting approach matters
        # even when the others are empty.
        wait = max(sample.wait for sample in samples)

        score = (
            weights.queue * queue
            + weights.stationary * stationary
            + weights.wait * wait
            + weights.congestion * congestion
        )
        breakdown = (
            f"q={queue:.1f} s={stationary:.1f} w={wait:.1f}s c={congestion:.1f}"
        )
        return score, breakdown

    def _aggregate(self, values: list[float]) -> float:
        if not values:
            return 0.0
        if self.settings.aggregation == "max":
            return max(values)
        return sum(values)

    def _choose_winner(
        self,
        scores: Mapping[str, float],
        now: float,
        expected_red_s: Mapping[str, float],
    ) -> tuple[str, bool]:
        """Highest score, unless a starving unit with demand must go first."""
        starving = [
            unit_id
            for unit_id, score in scores.items()
            if score >= self.settings.idle_score_threshold
            and (
                now - self._last_served.get(unit_id, now)
                > self.settings.max_wait_before_force_s
                or expected_red_s.get(unit_id, 0.0)
                > self._max_red_for(unit_id)
            )
        ]
        if starving:
            # Deterministic: longest wait wins; the unit that was just served
            # loses ties so one busy approach cannot monopolise the junction.
            starving.sort(
                key=lambda unit_id: (
                    self._last_served.get(unit_id, now),
                    unit_id == self._last_unit,
                    list(self.units).index(unit_id),
                )
            )
            return starving[0], True

        # Deterministic tie-break on configured unit order.
        winner = max(
            scores,
            key=lambda unit_id: (scores[unit_id], -list(self.units).index(unit_id)),
        )
        return winner, False

    def _max_red_for(self, unit_id: str) -> float:
        return float(
            self.settings.max_red_s_by_unit.get(unit_id, self.settings.max_red_s)
        )

    def _expected_green_for(
        self,
        unit_id: str,
        now: float,
        override: Optional[tuple[str, int]] = None,
    ) -> int:
        """The green this unit is expected to receive in the current cycle."""
        if override is not None and override[0] == unit_id:
            return override[1]
        green = self._expected_green.get(unit_id)
        if green is not None:
            remembered_at = self._expected_green_at.get(unit_id, now)
            if now - remembered_at <= self.settings.green_memory_s:
                return green
        return max(
            self.settings.min_green_s,
            min(self.settings.max_green_s, int(round(self.settings.base_green_s))),
        )

    def _expected_reds(
        self,
        now: float,
        override: Optional[tuple[str, int]] = None,
    ) -> dict[str, float]:
        """Estimated red per unit: the other greens plus clearances between.

        This is a model only. The controller owns phase order and the actual
        yellow/all-red interval; the host never sends a red duration.
        """
        greens = {
            unit_id: self._expected_green_for(unit_id, now, override)
            for unit_id in self.units
        }
        clearance = self.settings.yellow_s + self.settings.all_red_s
        expected: dict[str, float] = {}
        for unit_id in self.units:
            others = [other for other in self.units if other != unit_id]
            expected[unit_id] = round(
                sum(greens[other] for other in others)
                + len(others) * clearance,
                1,
            )
        return expected

    def _green_time(self, winner_score: float) -> tuple[int, str]:
        normalized = min(1.0, max(0.0, winner_score) / self.settings.score_saturation)
        raw = self.settings.base_green_s + self.settings.k_green_s * normalized
        clamped = int(round(raw))
        note = f"green raw={raw:.1f}s"
        if clamped < self.settings.min_green_s:
            note += f", raised to configured min {self.settings.min_green_s}s"
            clamped = self.settings.min_green_s
        if clamped > self.settings.max_green_s:
            note += f", lowered to configured max {self.settings.max_green_s}s"
            clamped = self.settings.max_green_s
        clamped = max(5, min(30, clamped))
        return clamped, note

    def _change_gate(
        self, unit_id: str, green_time: int, now: float
    ) -> Optional[str]:
        if self._last_request_at is None:
            return None
        elapsed = now - self._last_request_at
        if elapsed < self.settings.min_interval_s:
            return (
                f"suppressed: {elapsed:.1f}s since last request "
                f"(min interval {self.settings.min_interval_s:.0f}s)"
            )
        if (
            unit_id == self._last_unit
            and self._last_green is not None
            and abs(green_time - self._last_green) <= self.settings.min_delta_s
        ):
            return (
                f"suppressed: {unit_id} green unchanged within "
                f"{self.settings.min_delta_s}s"
            )
        return None

    def _record_emission(self, unit_id: str, green_time: int, now: float) -> None:
        self._last_request_at = now
        self._last_green = green_time
        self._last_unit = unit_id
        self._last_served[unit_id] = now
        self._expected_green[unit_id] = green_time
        self._expected_green_at[unit_id] = now
