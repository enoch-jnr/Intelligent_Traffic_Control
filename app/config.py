"""Typed configuration loaded from YAML.

Only ``config.example.yaml`` is committed. The real ``config.yaml`` is ignored
because it holds RTSP credentials (or references them through ``${VAR}``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from app.system_info import total_memory_gb
from camera.base import FrameSource
from camera.file_source import FileSource
from camera.rtsp import RtspSource
from camera.webcam import WebcamSource
from decision.models import WIRE_MOVEMENTS, ServingUnit
from decision.settings import DecisionSettings, Weights
from gateway.adapter import (
    COMPAT_MODE,
    MODES,
    THREE_PHASE_MODE,
    THREE_PHASE_UNITS,
)
from gateway.client import GatewayConfig
from pipeline.camera_pipeline import DEFAULT_POLYGON, PipelineConfig

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: The only approaches accepted after the SEC/MAIN_A/MAIN_B role swap.
APPROACHES_V2: tuple[str, ...] = ("MAIN_A", "MAIN_B", "SEC")

#: Retired labels. A config that contains one is refused, never translated.
_RETIRED_APPROACHES = {"MAIN", "SEC_A", "SEC_B"}
_RETIRED_CAMERA_IDS = {"cam_main", "cam_sec_a", "cam_sec_b"}
_RETIRED_UNIT_IDS = {"SEC_A", "SEC_B"}
_RETIRED_MOVEMENTS = {"SECONDARY_A", "SECONDARY_B"}
_RETIRED_UI_TOKENS = ("approach-main", "approach-sec-a", "approach-sec-b")

_MIGRATION = (
    "The roles changed: old MAIN is now SEC, and old SEC_A/SEC_B are now "
    "MAIN_A/MAIN_B. Compat units are MAIN=[MAIN_A, MAIN_B] and "
    "SECONDARY=[SEC]. See docs/MIGRATION_RENAME.md."
)


class ConfigError(ValueError):
    """Raised for malformed or inconsistent configuration."""


PERFORMANCE_PRESETS: dict[str, dict[str, float | int]] = {
    "low": {
        "target_fps": 3.0,
        "imgsz": 480,
        "preview_fps": 5.0,
        "preview_scale": 0.5,
        "threads": 1,
    },
    "balanced": {
        "target_fps": 5.0,
        "imgsz": 640,
        "preview_fps": 8.0,
        "preview_scale": 0.75,
        "threads": 1,
    },
    "high": {
        "target_fps": 8.0,
        "imgsz": 640,
        "preview_fps": 10.0,
        "preview_scale": 1.0,
        "threads": 2,
    },
}


@dataclass(frozen=True)
class CameraConfig:
    id: str
    approach: str
    source: str
    url: Optional[str] = None
    index: int = 0
    path: Optional[str] = None
    flip: bool = False
    target_fps: float = 8.0
    polygon: tuple[tuple[float, float], ...] = DEFAULT_POLYGON
    count_line: float = 0.4
    width: Optional[int] = None
    height: Optional[int] = None
    stale_after_s: float = 2.0
    loop: bool = True
    realtime: bool = True
    movement_threshold: float = 5.0
    max_track_age_s: float = 5.0
    window_seconds: float = 10.0

    def pipeline_config(self) -> PipelineConfig:
        return PipelineConfig(
            polygon=self.polygon,
            count_line_fraction=self.count_line,
            movement_threshold=self.movement_threshold,
            target_fps=self.target_fps,
            stale_after_s=self.stale_after_s,
            max_track_age_s=self.max_track_age_s,
            window_seconds=self.window_seconds,
        )


@dataclass(frozen=True)
class DetectionConfig:
    model: str = "yolo11n.pt"
    device: str = "auto"
    confidence: float = 0.3
    imgsz: Optional[int] = None


@dataclass(frozen=True)
class PerformanceConfig:
    """Resolved inference and preview budget.

    ``requested_profile`` keeps the config value (including ``auto``) for the
    UI; ``profile`` is the preset actually applied.
    """

    profile: str = "low"
    requested_profile: str = "auto"
    target_fps: float = 3.0
    imgsz: int = 480
    preview_fps: float = 5.0
    preview_scale: float = 0.5
    threads: int = 1
    cv_threads: int = 1
    adaptive_fps: bool = True
    min_fps: float = 2.0
    analysis_max_width: int = 960

    @property
    def label(self) -> str:
        return (
            self.profile
            if self.requested_profile == self.profile
            else f"{self.profile} (auto)"
        )


@dataclass(frozen=True)
class HealthConfig:
    max_frame_age_s: float = 2.0
    tick_interval_s: float = 1.0
    tick_timeout_s: float = 2.0
    required_cameras: Optional[int] = None  # None = all configured cameras


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    decision_log: str = "logs/decisions.jsonl"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 3


@dataclass(frozen=True)
class AppConfig:
    cameras: tuple[CameraConfig, ...]
    units: tuple[ServingUnit, ...]
    decision: DecisionSettings
    gateway: GatewayConfig
    health: HealthConfig
    logging: LoggingConfig
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)
    display: bool = False
    gateway_mode: str = COMPAT_MODE
    gateway_movements: dict[str, str] = field(default_factory=dict)
    three_phase_enabled: bool = False

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(camera.id for camera in self.cameras)

    @property
    def required_camera_count(self) -> int:
        if self.health.required_cameras is None:
            return len(self.cameras)
        return min(self.health.required_cameras, len(self.cameras))


# -- loading --------------------------------------------------------------


def load_config(path: str | Path, env: Optional[Mapping[str, str]] = None) -> AppConfig:
    """Load and validate a YAML configuration file."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    return build_config(
        _expand_env(raw, env if env is not None else os.environ),
        base_dir=config_path.parent,
    )


def build_config(raw: Mapping[str, Any], *, base_dir: Optional[Path] = None) -> AppConfig:
    """Build a validated :class:`AppConfig` from already-expanded data."""
    base = Path(base_dir or ".")

    camera_entries = raw.get("cameras") or []
    if not isinstance(camera_entries, list) or not camera_entries:
        raise ConfigError("'cameras' must be a non-empty list")

    cameras: list[CameraConfig] = []
    for entry in camera_entries:
        if not isinstance(entry, dict):
            raise ConfigError("each camera entry must be a mapping")
        cameras.append(_camera_from(entry, base))

    ids = [camera.id for camera in cameras]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"camera ids must be unique, got {ids}")

    if "approaches" in raw:
        raise ConfigError(
            "'approaches' is no longer supported; assign approaches to serving "
            "units under decision.units"
        )
    if "phase_mode" in raw:
        raise ConfigError(
            "'phase_mode' was replaced by gateway.mode "
            "(compat_two_phase | three_phase)"
        )

    _reject_retired_cameras(cameras)

    approach_owners: dict[str, str] = {}
    for camera in cameras:
        if camera.approach not in APPROACHES_V2:
            raise ConfigError(
                f"camera {camera.id!r} uses unsupported approach "
                f"{camera.approach!r}; expected one of {APPROACHES_V2}. "
                + _MIGRATION
            )
        if camera.approach in approach_owners:
            raise ConfigError(
                f"approach {camera.approach!r} has two cameras "
                f"({approach_owners[camera.approach]!r} and {camera.id!r}); "
                "one camera per approach is supported"
            )
        approach_owners[camera.approach] = camera.id

    decision_raw = raw.get("decision") or {}
    if not isinstance(decision_raw, dict):
        raise ConfigError("'decision' must be a mapping")
    units = _units_from(decision_raw.get("units"))
    _reject_retired_units(units)
    _validate_units(units, set(approach_owners))
    decision = _decision_settings(decision_raw, units)

    _reject_retired_movements(raw.get("gateway"))
    gateway_data, gateway_mode, three_phase_enabled, gateway_movements = (
        _gateway_options(raw.get("gateway"), units)
    )
    gateway = _build(GatewayConfig, gateway_data, "gateway")

    health_raw = raw.get("health") or {}
    health = _build(HealthConfig, health_raw, "health")

    logging_raw = raw.get("logging") or {}
    logging_config = _build(LoggingConfig, logging_raw, "logging")

    detection_raw = raw.get("detection") or {}
    detection = _build(DetectionConfig, detection_raw, "detection")
    performance = _performance(raw.get("performance"))

    display = bool(raw.get("display", False))

    return AppConfig(
        cameras=tuple(cameras),
        units=units,
        decision=decision,
        gateway=gateway,
        health=health,
        logging=logging_config,
        detection=detection,
        performance=performance,
        display=display,
        gateway_mode=gateway_mode,
        gateway_movements=gateway_movements,
        three_phase_enabled=three_phase_enabled,
    )


def _reject_retired_cameras(cameras: list[CameraConfig]) -> None:
    for camera in cameras:
        if camera.id in _RETIRED_CAMERA_IDS or camera.approach in _RETIRED_APPROACHES:
            raise ConfigError(
                f"camera {camera.id!r} uses the retired approach layout "
                f"(id={camera.id!r}, approach={camera.approach!r}). "
                "Migration is not automatic. " + _MIGRATION
            )


def _reject_retired_units(units: tuple[ServingUnit, ...]) -> None:
    for unit in units:
        retired = [name for name in unit.approaches if name in _RETIRED_APPROACHES]
        if unit.id in _RETIRED_UNIT_IDS or retired:
            raise ConfigError(
                "decision.units uses the retired approach layout "
                f"(unit={unit.id!r}, approaches={list(unit.approaches)!r}). "
                + _MIGRATION
            )


def _reject_retired_movements(gateway: Any) -> None:
    if not isinstance(gateway, Mapping):
        return
    three = gateway.get("three_phase")
    if not isinstance(three, Mapping):
        return
    movements = three.get("movements")
    if not isinstance(movements, Mapping):
        return
    for key, value in movements.items():
        if key in _RETIRED_APPROACHES or value in _RETIRED_MOVEMENTS:
            raise ConfigError(
                "gateway.three_phase.movements uses retired names "
                f"({key!r}: {value!r}). The proposed movement names are "
                "MAIN_A, MAIN_B and SECONDARY. " + _MIGRATION
            )


def _camera_from(entry: Mapping[str, Any], base_dir: Path) -> CameraConfig:
    allowed = {
        "id",
        "approach",
        "source",
        "url",
        "index",
        "path",
        "flip",
        "target_fps",
        "polygon",
        "count_line",
        "width",
        "height",
        "stale_after_s",
        "loop",
        "realtime",
        "movement_threshold",
        "max_track_age_s",
        "window_seconds",
    }
    unknown = set(entry) - allowed
    if unknown:
        raise ConfigError(f"unknown camera keys: {sorted(unknown)}")

    camera_id = entry.get("id")
    source = entry.get("source")
    approach = entry.get("approach")
    if not camera_id or not isinstance(camera_id, str):
        raise ConfigError("each camera needs a string 'id'")
    if source not in ("rtsp", "webcam", "file"):
        raise ConfigError(
            f"camera {camera_id!r} has invalid source {source!r} "
            "(expected rtsp, webcam or file)"
        )
    if not approach or not isinstance(approach, str):
        raise ConfigError(f"camera {camera_id!r} needs an 'approach'")

    polygon = _polygon(entry.get("polygon"), camera_id)

    path = entry.get("path")
    if source == "file":
        if not path:
            raise ConfigError(f"camera {camera_id!r} is a file source without 'path'")
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        path = str(candidate)

    url = entry.get("url")
    if source == "rtsp" and not url:
        raise ConfigError(f"camera {camera_id!r} is an rtsp source without 'url'")

    count_line = float(entry.get("count_line", 0.4))
    if not 0.0 <= count_line <= 1.0:
        raise ConfigError(f"camera {camera_id!r} count_line must be within [0, 1]")

    target_fps = float(entry.get("target_fps", 8.0))
    if target_fps <= 0:
        raise ConfigError(f"camera {camera_id!r} target_fps must be positive")

    return CameraConfig(
        id=camera_id,
        approach=approach,
        source=source,
        url=url,
        index=int(entry.get("index", 0)),
        path=path,
        flip=bool(entry.get("flip", False)),
        target_fps=target_fps,
        polygon=polygon,
        count_line=count_line,
        width=entry.get("width"),
        height=entry.get("height"),
        stale_after_s=float(entry.get("stale_after_s", 2.0)),
        loop=bool(entry.get("loop", True)),
        realtime=bool(entry.get("realtime", True)),
        movement_threshold=float(entry.get("movement_threshold", 5.0)),
        max_track_age_s=float(entry.get("max_track_age_s", 5.0)),
        window_seconds=float(entry.get("window_seconds", 10.0)),
    )


def _polygon(value: Any, camera_id: str) -> tuple[tuple[float, float], ...]:
    if value is None:
        return DEFAULT_POLYGON
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        raise ConfigError(f"camera {camera_id!r} polygon needs at least 3 points")
    points: list[tuple[float, float]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ConfigError(f"camera {camera_id!r} polygon points must be [x, y]")
        x, y = float(point[0]), float(point[1])
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ConfigError(
                f"camera {camera_id!r} polygon coordinates must be normalized"
            )
        points.append((x, y))
    return tuple(points)


def _units_from(value: Any) -> tuple[ServingUnit, ...]:
    """Parse ``decision.units``: the engine's serving layout."""
    if not isinstance(value, list) or not value:
        raise ConfigError("decision.units must be a non-empty list")

    units: list[ServingUnit] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ConfigError("each decision.units entry must be a mapping")
        unknown = set(entry) - {"id", "approaches"}
        if unknown:
            raise ConfigError(
                f"unknown decision.units keys: {sorted(unknown)}"
            )
        unit_id = entry.get("id")
        approaches = entry.get("approaches")
        if not isinstance(unit_id, str) or not unit_id:
            raise ConfigError("each decision unit needs a non-empty string 'id'")
        if not isinstance(approaches, list) or not approaches:
            raise ConfigError(
                f"decision unit {unit_id!r} needs a non-empty 'approaches' list"
            )
        if not all(isinstance(item, str) and item for item in approaches):
            raise ConfigError(
                f"decision unit {unit_id!r} approaches must be non-empty strings"
            )
        units.append(ServingUnit(unit_id, tuple(approaches)))

    ids = [unit.id for unit in units]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"serving unit ids must be unique, got {ids}")

    assigned: dict[str, str] = {}
    for unit in units:
        for approach_id in unit.approaches:
            if approach_id in assigned:
                raise ConfigError(
                    f"approach {approach_id!r} is assigned to both "
                    f"{assigned[approach_id]!r} and {unit.id!r}"
                )
            assigned[approach_id] = unit.id

    return tuple(units)


def _validate_units(
    units: tuple[ServingUnit, ...], camera_approaches: set[str]
) -> None:
    """Every camera approach must be in exactly one unit, and vice versa."""
    unit_approaches = {
        approach_id for unit in units for approach_id in unit.approaches
    }
    missing = sorted(camera_approaches - unit_approaches)
    if missing:
        raise ConfigError(
            "camera approaches missing from decision.units: " + ", ".join(missing)
        )
    orphaned = sorted(unit_approaches - camera_approaches)
    if orphaned:
        raise ConfigError(
            "decision.units reference approaches with no camera: "
            + ", ".join(orphaned)
        )


def _gateway_options(
    raw: Any, units: tuple[ServingUnit, ...]
) -> tuple[dict[str, Any], str, bool, dict[str, str]]:
    """Split ``gateway`` into client settings, mode and unit->movement map."""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ConfigError("'gateway' must be a mapping")
    data = dict(raw)

    mode = str(data.pop("mode", COMPAT_MODE))
    if mode not in MODES:
        raise ConfigError(
            f"gateway.mode must be one of {MODES}, got {mode!r}"
        )

    three_raw = data.pop("three_phase", None) or {}
    if not isinstance(three_raw, Mapping):
        raise ConfigError("gateway.three_phase must be a mapping")
    unknown = set(three_raw) - {"enabled", "movements"}
    if unknown:
        raise ConfigError(
            f"unknown keys in 'gateway.three_phase': {sorted(unknown)}"
        )
    enabled = bool(three_raw.get("enabled", False))
    movements_raw = three_raw.get("movements") or {}
    if not isinstance(movements_raw, Mapping):
        raise ConfigError("gateway.three_phase.movements must be a mapping")

    unit_ids = [unit.id for unit in units]

    if mode == COMPAT_MODE:
        if enabled:
            raise ConfigError(
                "gateway.three_phase.enabled is true but gateway.mode is "
                "'compat_two_phase'; choose gateway.mode: three_phase"
            )
        unsupported = [unit_id for unit_id in unit_ids if unit_id not in WIRE_MOVEMENTS]
        if unsupported:
            raise ConfigError(
                f"compat_two_phase units must be {WIRE_MOVEMENTS}; got "
                f"{unsupported}. Put MAIN_A/MAIN_B in the MAIN unit and SEC "
                "in SECONDARY, or set gateway.mode: three_phase (needs new "
                "firmware)"
            )
        allowed = {
            "MAIN": {"MAIN_A", "MAIN_B"},
            "SECONDARY": {"SEC"},
        }
        for unit in units:
            wrong = set(unit.approaches) - allowed.get(unit.id, set())
            if wrong:
                raise ConfigError(
                    f"compat unit {unit.id!r} cannot contain "
                    f"{sorted(wrong)}; MAIN may contain MAIN_A/MAIN_B and "
                    "SECONDARY may contain SEC only. " + _MIGRATION
                )
        movements = {unit_id: unit_id for unit_id in unit_ids}
        return data, mode, False, movements

    # -- three_phase: disabled by default and needs explicit names ---------
    if not enabled:
        raise ConfigError(
            "gateway.mode is 'three_phase' but gateway.three_phase.enabled is "
            "false. Three-phase control needs firmware support that does not "
            "exist yet; the current gateway only accepts MAIN and SECONDARY"
        )
    if len(unit_ids) != len(THREE_PHASE_UNITS) or set(unit_ids) != set(
        THREE_PHASE_UNITS
    ):
        raise ConfigError(
            f"three_phase mode requires exactly the units {THREE_PHASE_UNITS}, "
            f"got {unit_ids}"
        )
    missing = [unit_id for unit_id in THREE_PHASE_UNITS if unit_id not in movements_raw]
    if missing:
        raise ConfigError(
            "gateway.three_phase.movements must explicitly name every unit; "
            f"missing {missing}. Proposed names are MAIN_A/MAIN_B/SECONDARY, "
            "but nothing accepts them yet"
        )
    extra = set(movements_raw) - set(THREE_PHASE_UNITS)
    if extra:
        raise ConfigError(
            f"gateway.three_phase.movements has unknown units: {sorted(extra)}"
        )

    movements: dict[str, str] = {}
    for unit_id in THREE_PHASE_UNITS:
        value = movements_raw[unit_id]
        if not isinstance(value, str) or not value:
            raise ConfigError(
                f"gateway.three_phase.movements[{unit_id!r}] must be a non-empty "
                "string"
            )
        movements[unit_id] = value
    if len(set(movements.values())) != len(movements):
        raise ConfigError(
            "gateway.three_phase.movements values must be unique, got "
            f"{movements}"
        )
    return data, mode, True, movements


def _decision_settings(
    raw: Mapping[str, Any], units: tuple[ServingUnit, ...]
) -> DecisionSettings:
    allowed = {
        "weights",
        "base_green_s",
        "k_green_s",
        "score_saturation",
        "min_green_s",
        "max_green_s",
        "min_delta_s",
        "min_interval_s",
        "max_wait_before_force_s",
        "smoothing_window_s",
        "max_metrics_age_s",
        "idle_score_threshold",
        "aggregation",
        "require_all_approaches_healthy",
        "units",
        "yellow_s",
        "all_red_s",
        "max_red_s",
        "green_memory_s",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"unknown decision keys: {sorted(unknown)}")

    weights_raw = raw.get("weights") or {}
    weights = _build(Weights, weights_raw, "decision.weights")

    aggregation = str(raw.get("aggregation", "sum"))
    if aggregation not in ("sum", "max"):
        raise ConfigError("decision.aggregation must be 'sum' or 'max'")

    kwargs: dict[str, Any] = {
        "weights": weights,
        "aggregation": aggregation,
    }
    reserved = {"weights", "aggregation", "units", "max_red_s"}
    for key in allowed - reserved:
        if key in raw:
            kwargs[key] = raw[key]

    if "max_red_s" in raw:
        max_red_raw = raw["max_red_s"]
        unit_ids = [unit.id for unit in units]
        if isinstance(max_red_raw, Mapping):
            missing = [unit_id for unit_id in unit_ids if unit_id not in max_red_raw]
            extra = set(max_red_raw) - set(unit_ids)
            if missing:
                raise ConfigError(
                    "decision.max_red_s must cap every unit; missing "
                    f"{missing} (or use a single number for all units)"
                )
            if extra:
                raise ConfigError(
                    "decision.max_red_s names unknown units: "
                    f"{sorted(extra)}"
                )
            caps: dict[str, float] = {}
            for unit_id in unit_ids:
                cap = max_red_raw[unit_id]
                if isinstance(cap, bool) or not isinstance(cap, (int, float)):
                    raise ConfigError(
                        f"decision.max_red_s[{unit_id!r}] must be a number"
                    )
                caps[unit_id] = float(cap)
            kwargs["max_red_s_by_unit"] = caps
            kwargs["max_red_s"] = max(caps.values())
        elif isinstance(max_red_raw, bool) or not isinstance(
            max_red_raw, (int, float)
        ):
            raise ConfigError(
                "decision.max_red_s must be a number or a unit->seconds mapping"
            )
        else:
            kwargs["max_red_s"] = float(max_red_raw)

    try:
        return DecisionSettings(**kwargs)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid decision settings: {exc}") from exc


def _performance(raw: Any) -> PerformanceConfig:
    """Resolve the preset and apply explicit performance overrides."""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ConfigError("'performance' must be a mapping")
    allowed = {
        "profile",
        "target_fps",
        "imgsz",
        "preview_fps",
        "preview_scale",
        "threads",
        "cv_threads",
        "adaptive_fps",
        "min_fps",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"unknown keys in 'performance': {sorted(unknown)}")

    requested = str(raw.get("profile", "auto")).lower()
    if requested not in ("auto", "low", "balanced", "high"):
        raise ConfigError(
            "performance.profile must be auto, low, balanced or high, "
            f"got {requested!r}"
        )
    resolved = requested
    if resolved == "auto":
        memory = total_memory_gb()
        resolved = "low" if memory is None or memory <= 8.5 else "balanced"

    values: dict[str, Any] = dict(PERFORMANCE_PRESETS[resolved])
    for key in (
        "target_fps",
        "imgsz",
        "preview_fps",
        "preview_scale",
        "threads",
        "cv_threads",
        "adaptive_fps",
        "min_fps",
    ):
        if key in raw:
            values[key] = raw[key]

    target_fps = float(values["target_fps"])
    min_fps = float(values.get("min_fps", 2.0))
    imgsz = int(values["imgsz"])
    preview_fps = float(values["preview_fps"])
    preview_scale = float(values["preview_scale"])
    threads = int(values.get("threads", 1))
    cv_threads = int(values.get("cv_threads", 1))
    if target_fps <= 0:
        raise ConfigError("performance.target_fps must be positive")
    if not 1.0 <= min_fps <= target_fps:
        raise ConfigError("performance.min_fps must be within [1, target_fps]")
    if not 320 <= imgsz <= 1280:
        raise ConfigError("performance.imgsz must be within [320, 1280]")
    if not 1 <= preview_fps <= 30:
        raise ConfigError("performance.preview_fps must be within [1, 30]")
    if not 0.25 <= preview_scale <= 1.0:
        raise ConfigError("performance.preview_scale must be within [0.25, 1.0]")
    if threads < 1 or cv_threads < 1:
        raise ConfigError("performance thread counts must be at least 1")

    return PerformanceConfig(
        profile=resolved,
        requested_profile=requested,
        target_fps=target_fps,
        imgsz=imgsz,
        preview_fps=preview_fps,
        preview_scale=preview_scale,
        threads=threads,
        cv_threads=cv_threads,
        adaptive_fps=bool(values.get("adaptive_fps", True)),
        min_fps=min_fps,
        analysis_max_width=max(640, imgsz * 2),
    )


def _build(cls, data: Mapping[str, Any], section: str):
    if not isinstance(data, Mapping):
        raise ConfigError(f"'{section}' must be a mapping")
    names = set(getattr(cls, "__dataclass_fields__", {}))
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"unknown keys in '{section}': {sorted(unknown)}")
    try:
        return cls(**data)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid '{section}': {exc}") from exc


def _expand_env(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        missing: set[str] = set()

        def replace(match: re.Match) -> str:
            name = match.group(1)
            if name not in env:
                missing.add(name)
                return match.group(0)
            return env[name]

        expanded = _ENV_PATTERN.sub(replace, value)
        if missing:
            raise ConfigError(
                "environment variable(s) not set: " + ", ".join(sorted(missing))
            )
        return expanded
    if isinstance(value, dict):
        return {key: _expand_env(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item, env) for item in value]
    return value


# -- runtime construction -------------------------------------------------


def build_source(
    camera: CameraConfig, *, analysis_max_width: Optional[int] = None
) -> FrameSource:
    """Create the frame source described by a camera config."""
    if camera.source == "rtsp":
        return RtspSource(
            camera.id,
            camera.url or "",
            flip=camera.flip,
            width=camera.width,
            height=camera.height,
            stale_after_s=camera.stale_after_s,
            max_width=analysis_max_width,
        )
    if camera.source == "webcam":
        return WebcamSource(
            camera.id,
            camera.index,
            flip=camera.flip,
            width=camera.width,
            height=camera.height,
            stale_after_s=camera.stale_after_s,
            max_width=analysis_max_width,
        )
    if camera.source == "file":
        return FileSource(
            camera.id,
            camera.path or "",
            loop=camera.loop,
            realtime=camera.realtime,
            flip=camera.flip,
            stale_after_s=camera.stale_after_s,
            max_width=analysis_max_width,
        )
    raise ConfigError(f"unsupported camera source {camera.source!r}")
