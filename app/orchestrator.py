"""Wires cameras, pipelines, the decision engine and the gateway together.

One loop ticks at ``health.tick_interval_s`` (default 1 s):

1. collect each approach's latest :class:`ApproachMetrics`;
2. evaluate them (pure decision logic);
3. send at most one advisory request, if the gateway is connected;
4. append one JSONL entry with metrics, health, reasons, request and reply.

The heartbeat gate is computed from the same state: the decision loop must have
ticked within ``tick_timeout_s`` and at least ``required_camera_count`` cameras
must have delivered a fresh frame. Vision trouble therefore makes the host go
quiet instead of guessing.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import cv2
import numpy as np

from app.config import AppConfig, CameraConfig, build_source
from app.decision_log import DecisionLog
from camera.manager import CameraManager
from decision.engine import Decision, DecisionEngine
from decision.models import APPROACH_LAYOUT, ApproachMetrics, Recommendation
from detection.vehicle_detector import VehicleDetector, configure_runtime_threads
from gateway.adapter import ProtocolAdapter
from gateway.client import GatewayClient, RequestResult
from pipeline.camera_pipeline import CameraPipeline

_log = logging.getLogger("app.orchestrator")

DetectorFactory = Callable[[CameraConfig], Any]


class Orchestrator:
    """Runs the advisory control loop for one intersection."""

    def __init__(
        self,
        config: AppConfig,
        *,
        display: bool = False,
        dry_run: bool = False,
        detector_factory: Optional[DetectorFactory] = None,
        gateway: Optional[GatewayClient] = None,
        on_tick: Optional[Callable[[dict], None]] = None,
        annotate_frames: bool = False,
        draw_stats: bool = True,
    ) -> None:
        self.config = config
        self.display = display or config.display
        self.dry_run = dry_run
        # The GUI needs annotated copies but must not open the OpenCV window.
        self.annotate_frames = annotate_frames
        self.on_tick = on_tick

        self.sources = {
            camera.id: build_source(
                camera,
                analysis_max_width=config.performance.analysis_max_width,
            )
            for camera in config.cameras
        }
        self.manager = CameraManager(self.sources)

        factory = detector_factory or self._default_detector
        self.pipelines: list[CameraPipeline] = []
        for camera in config.cameras:
            pipeline_config = camera.pipeline_config()
            pipeline_config = replace(
                pipeline_config,
                target_fps=min(
                    camera.target_fps, config.performance.target_fps
                ),
                adaptive_fps=config.performance.adaptive_fps,
                min_fps=config.performance.min_fps,
            )
            self.pipelines.append(
                CameraPipeline(
                    camera.id,
                    camera.approach,
                    self.sources[camera.id],
                    factory(camera),
                    pipeline_config,
                    annotate=self.display or annotate_frames,
                    draw_stats=draw_stats,
                )
            )

        self.engine = DecisionEngine(config.units, config.decision)
        self.adapter = ProtocolAdapter(
            config.gateway_mode,
            config.gateway_movements,
            [unit.id for unit in config.units],
            three_phase_enabled=config.three_phase_enabled,
        )
        if gateway is not None:
            self.gateway: Optional[GatewayClient] = gateway
        elif dry_run:
            self.gateway = None
        else:
            self.gateway = GatewayClient(
                config.gateway,
                self.health_gate_open,
                name="traffic",
                movements=self.adapter.wire_movements,
            )

        self.decision_log: Optional[DecisionLog] = None
        self._stop = threading.Event()
        self._started = False
        self._last_tick_at: Optional[float] = None
        self._gate_state: Optional[bool] = None
        self._gate_reason = "not started"
        self._requests_held = False
        self._operator_released = False
        self._reset_requested = threading.Event()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        configure_runtime_threads(
            self.config.performance.threads,
            self.config.performance.cv_threads,
        )
        self._stop.clear()
        self._last_tick_at = None
        self._gate_state = None

        for warning in self.adapter.startup_warnings():
            _log.warning(warning)

        if self.config.logging.decision_log:
            self.decision_log = DecisionLog(
                self.config.logging.decision_log,
                max_bytes=self.config.logging.max_bytes,
                backup_count=self.config.logging.backup_count,
            )
            _log.info("writing decisions to %s", self.decision_log.path)

        self.manager.start_all()
        for pipeline in self.pipelines:
            pipeline.start()
        if self.gateway is not None:
            self.gateway.start()

        self._started = True
        _log.info(
            "orchestrator started: %d camera(s), mode=%s, dry_run=%s, display=%s",
            len(self.pipelines),
            self.config.gateway_mode,
            self.dry_run,
            self.display,
        )

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()

        # Signal every worker first, then join: a slow frame in one pipeline
        # must not serialise shutdown behind the others.
        for pipeline in self.pipelines:
            pipeline.request_stop()
        for pipeline in self.pipelines:
            pipeline.stop()
        self.manager.stop_all()
        if self.gateway is not None:
            self.gateway.stop()
        if self.display:
            cv2.destroyAllWindows()
        if self.decision_log is not None:
            self.decision_log.close()
            self.decision_log = None

        self._started = False
        _log.info("orchestrator stopped")

    # -- operator controls (safe to call from another thread) --------------

    def hold_requests(self) -> None:
        """Keep analysing and heartbeating, but stop sending requests."""
        if not self._requests_held:
            self._requests_held = True
            _log.warning(
                "operator hold: analysis continues, traffic requests suppressed"
            )

    def resume_requests(self) -> None:
        if self._requests_held:
            self._requests_held = False
            _log.info("operator hold cleared: requests resume")

    @property
    def requests_held(self) -> bool:
        return self._requests_held

    def request_reset(self) -> None:
        """Ask the decision loop to clear analysis state and restart the link."""
        self._reset_requested.set()

    def release_to_autonomous(self) -> None:
        """Close the heartbeat gate on purpose so the Uno runs autonomously."""
        if not self._operator_released:
            self._operator_released = True
            self._gate_reason = "Operator release: controller runs autonomously"
            _log.warning(
                "operator release: heartbeat gate CLOSED; the controller "
                "runs its autonomous cycle"
            )

    def resume_control(self) -> None:
        if self._operator_released:
            self._operator_released = False
            self._gate_reason = "operator release cleared"
            _log.info("operator release cleared: normal health gating resumes")

    @property
    def operator_released(self) -> bool:
        return self._operator_released

    def set_overlays(self, *, polygon: bool = True, count_line: bool = True) -> None:
        for pipeline in self.pipelines:
            pipeline.set_overlays(polygon=polygon, count_line=count_line)

    def set_camera_overlays(
        self, camera_id: str, *, polygon: bool = True, count_line: bool = True
    ) -> None:
        for pipeline in self.pipelines:
            if pipeline.camera_id == camera_id:
                pipeline.set_overlays(polygon=polygon, count_line=count_line)
                return

    def annotated_frame(
        self, camera_id: str, max_size: int = 640, if_version: Optional[int] = None
    ):
        """Return a downscaled annotated frame, never the raw capture buffer."""
        for pipeline in self.pipelines:
            if pipeline.camera_id == camera_id:
                return pipeline.latest_annotated(
                    max_size=max_size, if_version=if_version
                )
        return None

    def gate_reason(self) -> str:
        """Plain-language reason for the current gate state."""
        if self._operator_released:
            return "Operator release: controller runs autonomously"
        if not self._started:
            return "stopped"
        if self.health_gate_open():
            return "decision loop and cameras healthy"
        return self._gate_reason

    def _apply_reset(self) -> None:
        self._reset_requested.clear()
        self.engine.reset()
        for pipeline in self.pipelines:
            # The pipeline worker performs the reset on its own thread.
            pipeline.request_state_reset()
        if self.gateway is not None:
            self.gateway.restart_session()
        _log.info(
            "operator reset: smoothing, counters and tracks cleared; "
            "gateway session restarting"
        )

    def run(self, duration_s: Optional[float] = None) -> None:
        """Block until Ctrl-C, ``q`` in the display, or ``duration_s`` elapses."""
        self.start()
        interval = max(0.05, self.config.health.tick_interval_s)
        deadline = None if duration_s is None else time.monotonic() + duration_s

        try:
            next_tick = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    break
                if now >= next_tick:
                    self.tick(now)
                    next_tick += interval
                    if next_tick < now:
                        next_tick = now + interval

                if self.display:
                    self._show()
                    if (cv2.waitKey(1) & 0xFF) in (ord("q"), 27):
                        break
                    self._stop.wait(0.03)
                else:
                    self._stop.wait(0.05)
        finally:
            self.stop()

    # -- one cycle --------------------------------------------------------

    def tick(self, now: Optional[float] = None) -> dict:
        """Evaluate once, maybe send a request, and write one log entry."""
        if now is None:
            now = time.monotonic()

        if self._reset_requested.is_set():
            self._apply_reset()

        metrics: dict[str, ApproachMetrics] = {
            pipeline.approach_id: pipeline.latest_metrics()
            for pipeline in self.pipelines
        }
        decision = self.engine.evaluate(metrics, now)
        self._last_tick_at = now

        gate_open = self.health_gate_open()
        self._log_gate_transition(gate_open, metrics)

        request_info: Optional[dict] = None
        if decision.recommendation is not None:
            request_info = self._send(decision.recommendation)

        entry = self._build_entry(
            now, metrics, decision, gate_open, request_info
        )
        if self.decision_log is not None:
            self.decision_log.write(entry)
        if self.on_tick is not None:
            try:
                self.on_tick(entry)
            except Exception:  # noqa: BLE001 - the GUI must not break control
                _log.exception("on_tick callback failed")
        return entry

    def _send(self, recommendation: Recommendation) -> dict:
        wire = self.adapter.to_wire(recommendation)
        if self._requests_held:
            return {
                "sent": False,
                "reason": "operator_hold",
                "unit_id": wire.unit_id,
                "movement": wire.movement,
                "green_time": wire.green_time,
            }
        if self.dry_run or self.gateway is None:
            return {
                "sent": False,
                "reason": "dry_run",
                "unit_id": wire.unit_id,
                "movement": wire.movement,
            }

        result: RequestResult = self.gateway.request_green(
            wire.movement, wire.green_time
        )
        if result.ok:
            _log.info(
                "request %s/%s %ss accepted (sequence %s, %s ms)",
                wire.unit_id,
                wire.movement,
                wire.green_time,
                result.sequence,
                round(result.latency_s * 1000),
            )
        else:
            _log.warning(
                "request %s/%s %ss not accepted: %s (%s)",
                wire.unit_id,
                wire.movement,
                wire.green_time,
                result.status,
                result.reason,
            )

        return {
            "sent": True,
            "unit_id": wire.unit_id,
            "movement": wire.movement,
            "green_time": wire.green_time,
            "priority": "NORMAL",
            "sequence": result.sequence,
            "accepted": result.ok,
            "status": result.status,
            "latency_s": result.latency_s,
            "reply": result.raw,
        }

    # -- health gate ------------------------------------------------------

    def health_gate_open(self) -> bool:
        """True while the host is healthy enough to keep the link alive."""
        if self._operator_released:
            return False
        now = time.monotonic()
        if self._last_tick_at is None:
            return False
        if now - self._last_tick_at > self.config.health.tick_timeout_s:
            return False
        live = self.manager.live_camera_ids(
            max_age_s=self.config.health.max_frame_age_s
        )
        return len(live) >= self.config.required_camera_count

    def _log_gate_transition(
        self, gate_open: bool, metrics: dict[str, ApproachMetrics]
    ) -> None:
        if gate_open == self._gate_state:
            return
        self._gate_state = gate_open
        if gate_open:
            self._gate_reason = "decision loop and cameras healthy"
            _log.info("heartbeat gate OPEN: decision loop and cameras healthy")
            return

        reasons: list[str] = []
        if self._last_tick_at is not None:
            age = time.monotonic() - self._last_tick_at
            if age > self.config.health.tick_timeout_s:
                reasons.append(f"decision loop last ticked {age:.1f}s ago")
        unhealthy = sorted(
            approach_id
            for approach_id, snapshot in metrics.items()
            if not snapshot.healthy
        )
        if unhealthy:
            reasons.append("unhealthy approaches: " + ", ".join(unhealthy))
        live = self.manager.live_camera_ids(
            max_age_s=self.config.health.max_frame_age_s
        )
        if len(live) < self.config.required_camera_count:
            reasons.append(
                f"{len(live)}/{self.config.required_camera_count} cameras fresh"
            )
        self._gate_reason = "; ".join(reasons) or "not ready"
        _log.warning(
            "heartbeat gate CLOSED: %s; the controller runs autonomously",
            self._gate_reason,
        )

    # -- logging ----------------------------------------------------------

    def _build_entry(
        self,
        now: float,
        metrics: dict[str, ApproachMetrics],
        decision: Decision,
        gate_open: bool,
        request_info: Optional[dict],
    ) -> dict:
        health = self.manager.health_all()
        recommendation = decision.recommendation

        recommendation_log: Optional[dict] = None
        if recommendation is not None:
            recommendation_log = recommendation.to_log_dict()
            recommendation_log["wire_movement"] = self.adapter.movement_for(
                recommendation.unit_id
            )

        return {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "monotonic_s": round(now, 3),
            "approach_layout": APPROACH_LAYOUT,
            "mode": self.config.gateway_mode,
            "health_gate_open": gate_open,
            "cameras": {
                camera_id: {
                    "state": snapshot.state.value,
                    "fps": snapshot.fps,
                    "frame_age_s": (
                        None
                        if snapshot.last_frame_age_s is None
                        else round(snapshot.last_frame_age_s, 3)
                    ),
                    "reconnects": snapshot.reconnects,
                }
                for camera_id, snapshot in health.items()
            },
            "approaches": {
                approach_id: snapshot.to_log_dict()
                for approach_id, snapshot in metrics.items()
            },
            "units": {
                unit.id: {
                    "approaches": list(unit.approaches),
                    "score": (
                        None
                        if unit.id not in decision.unit_scores
                        else round(decision.unit_scores[unit.id], 3)
                    ),
                    "expected_red_s": decision.expected_red_s.get(unit.id),
                }
                for unit in self.config.units
            },
            "decision": {
                "healthy": decision.healthy,
                "emitted": decision.emitted,
                "unit_scores": {
                    unit_id: round(score, 3)
                    for unit_id, score in decision.unit_scores.items()
                },
                "expected_red_s": {
                    unit_id: round(value, 1)
                    for unit_id, value in decision.expected_red_s.items()
                },
                "reasons": list(decision.reasons),
            },
            "recommendation": recommendation_log,
            "request": request_info,
        }

    # -- display ----------------------------------------------------------

    def _show(self) -> None:
        tiles: list[np.ndarray] = []
        for pipeline in self.pipelines:
            frame = pipeline.latest_annotated()
            if frame is None:
                frame = np.zeros((360, 480, 3), dtype=np.uint8)
                cv2.putText(
                    frame,
                    f"{pipeline.camera_id}: waiting for frames",
                    (12, 180),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (200, 200, 200),
                    1,
                )
            else:
                frame = cv2.resize(frame, (480, 360))
            tiles.append(frame)

        if not tiles:
            return
        canvas = tiles[0] if len(tiles) == 1 else np.hstack(tiles)
        if canvas.shape[1] > 1900:
            scale = 1900 / canvas.shape[1]
            canvas = cv2.resize(canvas, (1900, int(canvas.shape[0] * scale)))
        cv2.imshow("Smarti traffic", canvas)

    # -- helpers ----------------------------------------------------------

    def _default_detector(self, camera: CameraConfig) -> VehicleDetector:
        detection = self.config.detection
        return VehicleDetector(
            detection.model,
            device=None if detection.device == "auto" else detection.device,
            confidence=detection.confidence,
            imgsz=detection.imgsz or self.config.performance.imgsz,
        )
