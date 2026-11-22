"""The only API the operator GUI talks to. No Qt imports.

Commands start/stop the host, hold or release advisory requests, reset analysis
state, and export logs/diagnostics. Reads return a frozen :class:`UiSnapshot`
polled at the UI's own rate, plus bounded log and event feeds.

The controller runs the existing orchestrator on a worker thread. Every command
is either a flag the decision loop consumes at the top of its next tick (reset,
hold, release) or a lifecycle operation; none of them can open the heartbeat
gate or bypass a health check.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from app.config import AppConfig
from app.events import EventFeed, LogBroadcaster, LogLine, UiEvent
from app.exporters import (
    export_diagnostics,
    export_entries,
    export_log_file,
    export_text,
    load_entries,
    tail_entries,
)
from app.orchestrator import Orchestrator
from app.snapshots import (
    CameraView,
    RecommendationView,
    UiSnapshot,
    UnitView,
    frozen_mapping,
)
from app.system_info import process_usage, system_usage
from camera.file_source import FileSource

_log = logging.getLogger("app.controller")

LOG_CAPACITY = 5000
EVENT_CAPACITY = 1000
DECISION_CAPACITY = 500


class AppController:
    """Commands and snapshots for the optional GUI layer."""

    def __init__(
        self,
        config: AppConfig,
        *,
        config_path: Optional[str] = None,
        display: bool = False,
        dry_run: bool = False,
        mock_gateway: bool = False,
        detector_factory: Optional[Callable[[Any], Any]] = None,
        gateway: Optional[Any] = None,
        annotate_frames: bool = True,
    ) -> None:
        self.config = config
        self.config_path = config_path
        self.dry_run = dry_run
        self.mock_gateway = mock_gateway

        self._lock = threading.RLock()
        self._logs = LogBroadcaster(capacity=LOG_CAPACITY)
        self._events = EventFeed(capacity=EVENT_CAPACITY)
        self._decisions: deque[dict] = deque(maxlen=DECISION_CAPACITY)
        self._last_tick: Optional[dict] = None
        self._tick_count = 0
        self._thread: Optional[threading.Thread] = None
        self._started_at: Optional[float] = None
        self._session_started_iso: Optional[str] = None
        self._last_camera_states: dict[str, str] = {}
        self._last_gate: Optional[bool] = None
        self._ever_connected = False
        self._closed = False
        self._root_logger = logging.getLogger()
        self._handler_attached = False

        self.orchestrator = Orchestrator(
            config,
            display=display,
            dry_run=dry_run,
            detector_factory=detector_factory,
            gateway=gateway,
            on_tick=self._on_tick,
            annotate_frames=annotate_frames,
            draw_stats=False,
        )
        self.attach_logging()

    # -- logging / feeds ---------------------------------------------------

    def attach_logging(self) -> None:
        if not self._handler_attached:
            self._root_logger.addHandler(self._logs)
            self._handler_attached = True

    def detach_logging(self) -> None:
        if self._handler_attached:
            self._root_logger.removeHandler(self._logs)
            self._handler_attached = False

    def log_lines(self, since_seq: int = 0) -> tuple[list[LogLine], int]:
        return self._logs.drain(since_seq)

    def events(self, since_seq: int = 0) -> tuple[list[UiEvent], int]:
        return self._events.drain(since_seq)

    def decisions(self) -> list[dict]:
        with self._lock:
            return [dict(entry) for entry in self._decisions]

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def start(self) -> bool:
        with self._lock:
            if self._closed:
                raise RuntimeError("controller is shut down")
            if self.running:
                return False
            if self._started_at is None:
                self._started_at = time.monotonic()
                self._session_started_iso = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
            self._thread = threading.Thread(
                target=self._run, name="app-controller", daemon=True
            )
            self._thread.start()
        self._events.publish("system", "host", "start", "running")
        return True

    def _run(self) -> None:
        try:
            self.orchestrator.run()
        except Exception:  # noqa: BLE001 - surfaced in the log, never silent
            _log.exception("orchestrator failed")
        finally:
            self._events.publish("system", "host", "stopped", "")

    def stop(self) -> None:
        self.orchestrator.stop()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=10.0)
        self._events.publish("system", "host", "stop", "stopped")

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
        self.stop()
        self.detach_logging()
        self._logs.close()
        self._events.close()
        with self._lock:
            self._closed = True
        _log.info("controller shut down")

    # -- operator commands -------------------------------------------------

    def reset(self) -> None:
        self.orchestrator.request_reset()
        self._events.publish("operator", "analysis", "reset requested")

    def hold_requests(self) -> None:
        self.orchestrator.hold_requests()
        self._events.publish("operator", "requests", "held", "no requests sent")

    def resume_requests(self) -> None:
        self.orchestrator.resume_requests()
        self._events.publish("operator", "requests", "resumed")

    def release_to_autonomous(self) -> None:
        self.orchestrator.release_to_autonomous()
        self._events.publish(
            "operator", "heartbeat gate", "released", "controller autonomous"
        )

    def resume_control(self) -> None:
        self.orchestrator.resume_control()
        self._events.publish("operator", "heartbeat gate", "resumed", "normal gating")

    @property
    def held(self) -> bool:
        return self.orchestrator.requests_held

    @property
    def released(self) -> bool:
        return self.orchestrator.operator_released

    def set_overlays(self, *, polygon: bool = True, count_line: bool = True) -> None:
        self.orchestrator.set_overlays(polygon=polygon, count_line=count_line)

    def set_camera_overlays(
        self, camera_id: str, *, polygon: bool = True, count_line: bool = True
    ) -> None:
        self.orchestrator.set_camera_overlays(
            camera_id, polygon=polygon, count_line=count_line
        )

    def set_flip(self, camera_id: str, flip: bool) -> None:
        source = self.orchestrator.sources.get(camera_id)
        if source is not None:
            source.flip = bool(flip)

    # -- playback (file sources only) -------------------------------------

    def file_camera_ids(self) -> tuple[str, ...]:
        return tuple(
            camera.id for camera in self.config.cameras if camera.source == "file"
        )

    def _file_sources(self) -> list[FileSource]:
        return [
            source
            for source in self.orchestrator.sources.values()
            if isinstance(source, FileSource)
        ]

    def pause_playback(self) -> None:
        for source in self._file_sources():
            source.pause()

    def resume_playback(self) -> None:
        for source in self._file_sources():
            source.resume()

    def restart_playback(self) -> None:
        for source in self._file_sources():
            source.restart()

    def set_playback_speed(self, factor: float) -> None:
        for source in self._file_sources():
            source.set_speed(factor)

    # -- read side ---------------------------------------------------------

    def frame(
        self, camera_id: str, max_size: int = 640, *, if_version: int | None = None
    ):
        return self.orchestrator.annotated_frame(
            camera_id, max_size=max_size, if_version=if_version
        )

    def frame_version(self, camera_id: str) -> int:
        for pipeline in self.orchestrator.pipelines:
            if pipeline.camera_id == camera_id:
                return pipeline.annotated_version()
        return 0

    def snapshot(self) -> UiSnapshot:
        orchestrator = self.orchestrator
        health = orchestrator.manager.health_all()
        pipeline_metrics = {
            pipeline.camera_id: pipeline.latest_metrics()
            for pipeline in orchestrator.pipelines
        }
        metrics = {
            pipeline.approach_id: pipeline.latest_metrics()
            for pipeline in orchestrator.pipelines
        }
        with self._lock:
            entry = self._last_tick
            tick_count = self._tick_count
            started_at = self._started_at
            session_iso = self._session_started_iso

        sources = {camera.id: camera.source for camera in self.config.cameras}
        pipelines = {pipeline.camera_id: pipeline for pipeline in orchestrator.pipelines}
        cameras = tuple(
            CameraView(
                camera_id=camera_id,
                approach=next(
                    (
                        camera.approach
                        for camera in self.config.cameras
                        if camera.id == camera_id
                    ),
                    "",
                ),
                source=sources.get(camera_id, ""),
                state=snapshot.state.value,
                fps=snapshot.fps,
                frame_age_s=snapshot.last_frame_age_s,
                reconnects=snapshot.reconnects,
                processed_fps=(
                    pipeline_metrics[camera_id].processing_fps
                    if camera_id in pipeline_metrics
                    else 0.0
                ),
                inference_ms=(
                    round(pipeline_metrics[camera_id].inference_s * 1000, 1)
                    if camera_id in pipeline_metrics
                    else 0.0
                ),
                dropped_frames=(
                    pipelines[camera_id].dropped_frames
                    if camera_id in pipelines
                    else 0
                ),
                effective_fps=(
                    pipelines[camera_id].target_fps
                    if camera_id in pipelines
                    else 0.0
                ),
                overloaded=(
                    pipelines[camera_id].overloaded
                    if camera_id in pipelines
                    else False
                ),
            )
            for camera_id, snapshot in sorted(health.items())
        )

        entry = entry or {}
        units_data = entry.get("units") or {}
        decision = entry.get("decision") or {}
        units = tuple(
            UnitView(
                unit_id=unit.id,
                approaches=tuple(unit.approaches),
                score=_optional_float(
                    (units_data.get(unit.id) or {}).get("score")
                ),
                expected_red_s=_optional_float(
                    (units_data.get(unit.id) or {}).get("expected_red_s")
                ),
            )
            for unit in self.config.units
        )

        recommendation = None
        raw_recommendation = entry.get("recommendation")
        if raw_recommendation:
            request = entry.get("request") or {}
            recommendation = RecommendationView(
                unit_id=str(raw_recommendation.get("unit_id", "")),
                wire_movement=str(
                    raw_recommendation.get("wire_movement")
                    or orchestrator.adapter.movement_for(
                        str(raw_recommendation.get("unit_id", ""))
                    )
                ),
                green_time=int(raw_recommendation.get("green_time", 0)),
                reasons=tuple(raw_recommendation.get("reasons") or ()),
                expected_red_s=frozen_mapping(
                    raw_recommendation.get("expected_red_s") or {}
                ),
                sent=bool(request.get("sent")),
                request_status=request.get("status") or request.get("reason"),
                accepted=request.get("accepted"),
                latency_s=_optional_float(request.get("latency_s")),
            )

        started = self.running
        gate_open = orchestrator.health_gate_open() if started else False
        gate_reason = orchestrator.gate_reason()
        gateway = orchestrator.gateway
        if self.dry_run:
            gateway_state = "DRY RUN"
        elif gateway is None:
            gateway_state = "OFF"
        elif gateway.connected:
            gateway_state = "CONNECTED"
            self._ever_connected = True
        elif started:
            gateway_state = "CONNECTING" if not self._ever_connected else "OFFLINE"
        else:
            gateway_state = "OFFLINE"

        cpu_percent, memory_percent = system_usage()
        app_cpu_percent, app_memory_mb = process_usage()
        return UiSnapshot(
            mode=self.config.gateway_mode,
            dry_run=self.dry_run,
            three_phase=self.config.three_phase_enabled,
            mock_gateway=self.mock_gateway,
            simulation=bool(
                self.dry_run or self.mock_gateway or self.config.three_phase_enabled
            ),
            gateway_state=gateway_state,
            gateway_host=self.config.gateway.host,
            gateway_port=int(self.config.gateway.port),
            gate_open=gate_open,
            gate_reason=gate_reason,
            cameras=cameras,
            approaches=tuple(
                metrics[key] for key in sorted(metrics)
            ),
            units=units,
            unit_scores=frozen_mapping(
                decision.get("unit_scores") or {}
            ),
            expected_red_s=frozen_mapping(
                decision.get("expected_red_s") or {}
            ),
            recommendation=recommendation,
            decision_reasons=tuple(decision.get("reasons") or ()),
            started=started,
            held=orchestrator.requests_held,
            released=orchestrator.operator_released,
            uptime_s=(
                0.0 if started_at is None else round(time.monotonic() - started_at, 1)
            ),
            cpu_percent=cpu_percent,
            memory_percent=memory_percent,
            log_path=str(self.config.logging.decision_log),
            tick_count=tick_count,
            last_tick_iso=entry.get("ts"),
            session_started_iso=session_iso,
            file_sources=any(
                camera.source == "file" for camera in self.config.cameras
            ),
            performance_profile=self.config.performance.label,
            app_cpu_percent=app_cpu_percent,
            app_memory_mb=app_memory_mb,
        )

    # -- exports -----------------------------------------------------------

    def export_log(
        self,
        path: str | Path,
        fmt: str = "jsonl",
        range_key: str = "all",
        *,
        selected: Optional[Sequence[Mapping[str, Any]]] = None,
        text_lines: Optional[Iterable[str]] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> int:
        """Export decisions as JSONL/CSV, or the filtered log text."""
        if fmt.lower() == "text":
            if text_lines is None:
                lines, _ = self._logs.drain(0)
                text_lines = [
                    _format_log_line(line) for line in lines
                ]
            return export_text(text_lines, path)

        if selected is None:
            return export_log_file(
                self.config.logging.decision_log,
                path,
                fmt,
                range_key,
                progress=progress,
            )
        entries = [dict(entry) for entry in selected]
        return export_entries(entries, path, fmt, progress=progress)

    def export_diagnostics(
        self,
        path: str | Path,
        *,
        versions: Optional[Mapping[str, str]] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> Path:
        entries = tail_entries(self.config.logging.decision_log, limit=500)
        lines, _ = self._logs.drain(0)
        health = {
            camera_id: {
                "state": snapshot.state.value,
                "fps": snapshot.fps,
                "frame_age_s": snapshot.last_frame_age_s,
                "reconnects": snapshot.reconnects,
            }
            for camera_id, snapshot in self.orchestrator.manager.health_all().items()
        }
        if progress is not None:
            progress(0, 1)
        target = export_diagnostics(
            path,
            config=self.config,
            entries=entries,
            log_lines=[_format_log_line(line) for line in lines],
            camera_health=health,
            versions=versions,
            config_path=self.config_path,
        )
        if progress is not None:
            progress(1, 1)
        return target

    # -- tick handling -----------------------------------------------------

    def _on_tick(self, entry: dict) -> None:
        with self._lock:
            self._last_tick = entry
            self._tick_count += 1
            self._decisions.append(entry)

        gate = bool(entry.get("health_gate_open"))
        if gate != self._last_gate:
            self._last_gate = gate
            self._events.publish(
                "gate",
                "heartbeat",
                "OPEN" if gate else "CLOSED",
                "; ".join(entry.get("decision", {}).get("reasons") or ())[:160],
            )

        recommendation = entry.get("recommendation")
        if recommendation:
            request = entry.get("request") or {}
            status = request.get("status") or request.get("reason") or "not sent"
            self._events.publish(
                "decision",
                str(recommendation.get("unit_id", "")),
                f"recommend {recommendation.get('green_time', '?')}s",
                str(status),
                {"ts": entry.get("ts")},
            )

        cameras = entry.get("cameras") or {}
        for camera_id, camera in cameras.items():
            state = str(camera.get("state", ""))
            if self._last_camera_states.get(camera_id) != state:
                self._last_camera_states[camera_id] = state
                self._events.publish(
                    "camera", camera_id, state, f"fps {camera.get('fps', 0)}"
                )


def _optional_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_log_line(line: LogLine) -> str:
    stamp = datetime.fromtimestamp(line.ts).strftime("%Y-%m-%d %H:%M:%S")
    return f"{stamp} {line.level:<7} {line.logger}: {line.message}"
