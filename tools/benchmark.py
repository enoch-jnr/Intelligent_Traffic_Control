"""Reproducible GUI-vs-headless benchmark using the real YOLO detector.

Examples::

    python -m tools.benchmark --duration 300 --out docs/benchmarks/before.json
    python -m tools.benchmark --only gui-mock --duration 60 --capture docs/ui/after/1280x720.png

Every run uses the same config and three looping video files. GUI runs use the
offscreen Qt platform so they are repeatable on a headless runner; painting,
frame conversion, log/table updates and the preview scheduler are still real.
CPU is the sum of the process tree normalised to the whole machine (100 % means
every logical core busy), not ``psutil``'s per-core percentage.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import psutil  # noqa: E402 - optional developer dependency

from app.config import AppConfig, load_config  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402

MODES = ("headless-dry", "headless-mock", "gui-dry", "gui-mock")


class _Sampler:
    def __init__(self) -> None:
        self.process = psutil.Process()
        self.process.cpu_percent(None)
        for child in self.process.children(recursive=True):
            try:
                child.cpu_percent(None)
            except psutil.Error:
                pass

    def read(self) -> dict[str, Any]:
        processes = [self.process]
        try:
            processes.extend(self.process.children(recursive=True))
        except psutil.Error:
            pass
        cpu = 0.0
        rss = 0
        for process in processes:
            try:
                cpu += process.cpu_percent(None)
                rss += process.memory_info().rss
            except psutil.Error:
                continue
        return {
            "tree_cpu_percent": round(cpu / max(1, os.cpu_count() or 1), 2),
            "tree_rss_mb": round(rss / (1024 * 1024), 1),
            "system_memory_percent": round(psutil.virtual_memory().percent, 1),
        }


def _summary(samples: list[dict[str, Any]], warmup_s: float, started: float) -> dict:
    kept = [s for s in samples if s["elapsed_s"] >= warmup_s]
    if not kept:
        kept = samples

    def mean(key: str) -> Optional[float]:
        values = [s[key] for s in kept if isinstance(s.get(key), (int, float))]
        return round(sum(values) / len(values), 2) if values else None

    def peak(key: str) -> Optional[float]:
        values = [s[key] for s in kept if isinstance(s.get(key), (int, float))]
        return round(max(values), 2) if values else None

    return {
        "samples": len(kept),
        "mean_tree_cpu_percent": mean("tree_cpu_percent"),
        "peak_tree_cpu_percent": peak("tree_cpu_percent"),
        "mean_tree_rss_mb": mean("tree_rss_mb"),
        "peak_tree_rss_mb": peak("tree_rss_mb"),
        "first_tree_rss_mb": kept[0]["tree_rss_mb"] if kept else None,
        "last_tree_rss_mb": kept[-1]["tree_rss_mb"] if kept else None,
        "mean_source_fps": mean("source_fps"),
        "mean_processed_fps": mean("processed_fps"),
        "mean_inference_ms": mean("inference_ms"),
        "mean_frame_age_s": mean("frame_age_s"),
        "mean_ui_render_fps": mean("ui_render_fps"),
        "dropped_frames_last": kept[-1]["dropped_frames"] if kept else None,
        "heartbeat_count": kept[-1]["heartbeats"] if kept else None,
        "heartbeat_mean_gap_s": mean("heartbeat_gap_s"),
        "heartbeat_max_gap_s": peak("heartbeat_max_gap_s"),
        "gate_flaps": kept[-1]["gate_flaps"] if kept else None,
        "camera_flaps": kept[-1]["camera_flaps"] if kept else None,
    }


def _machine() -> dict:
    import torch

    return {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cores": os.cpu_count(),
        "physical_cores": psutil.cpu_count(logical=False),
        "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "torch_threads": torch.get_num_threads(),
        "cuda": bool(torch.cuda.is_available()),
    }


def _gateway_config(config: AppConfig, port: int) -> AppConfig:
    return replace(config, gateway=replace(config.gateway, host="127.0.0.1", port=port))


def _make_orchestrator(
    config: AppConfig, *, dry_run: bool, on_tick: Callable[[dict], None]
) -> Orchestrator:
    return Orchestrator(config, display=False, dry_run=dry_run, on_tick=on_tick)


def _run_headless(
    config: AppConfig,
    *,
    duration: float,
    dry_run: bool,
    gateway_port: int,
    sampler: _Sampler,
):
    gate_states: list[bool] = []
    camera_states: dict[str, list[str]] = {}
    entries: list[dict] = []

    def on_tick(entry: dict) -> None:
        entries.append(entry)
        gate = bool(entry.get("health_gate_open"))
        if not gate_states or gate_states[-1] != gate:
            gate_states.append(gate)
        for camera_id, camera in (entry.get("cameras") or {}).items():
            states = camera_states.setdefault(camera_id, [])
            state = str(camera.get("state"))
            if not states or states[-1] != state:
                states.append(state)

    selected = config
    if not dry_run:
        selected = _gateway_config(config, gateway_port)
    orchestrator = _make_orchestrator(
        selected, dry_run=dry_run, on_tick=on_tick
    )
    orchestrator.start()
    started = time.monotonic()
    samples: list[dict[str, Any]] = []
    last_gate = None
    last_counts = None
    try:
        while time.monotonic() - started < duration:
            time.sleep(5.0)
            usage = sampler.read()
            metrics = {
                p.camera_id: p.latest_metrics() for p in orchestrator.pipelines
            }
            health = orchestrator.manager.health_all()
            sample = {
                "elapsed_s": round(time.monotonic() - started, 1),
                **usage,
                "source_fps": _mean(
                    [snapshot.fps for snapshot in health.values()]
                ),
                "processed_fps": _mean(
                    [m.processing_fps for m in metrics.values()]
                ),
                "inference_ms": _mean(
                    [
                        getattr(m, "inference_s", m.latency_s) * 1000
                        for m in metrics.values()
                    ]
                ),
                "frame_age_s": _mean(
                    [
                        m.frame_age_s
                        for m in metrics.values()
                        if m.frame_age_s is not None
                    ]
                ),
                "dropped_frames": _dropped(orchestrator),
                "ui_render_fps": None,
                "gate_open": bool(orchestrator.health_gate_open()),
                "gate_flaps": _flaps(gate_states),
                "camera_flaps": _flaps(
                    [state for states in camera_states.values() for state in states]
                ),
                "heartbeats": None,
                "heartbeat_gap_s": None,
                "heartbeat_max_gap_s": None,
            }
            heartbeat_times = _heartbeat_times(orchestrator.gateway)
            if heartbeat_times:
                gaps = [
                    b - a for a, b in zip(heartbeat_times, heartbeat_times[1:])
                ]
                sample["heartbeats"] = len(heartbeat_times)
                sample["heartbeat_gap_s"] = round(_mean(gaps), 3)
                sample["heartbeat_max_gap_s"] = round(max(gaps), 3)
            samples.append(sample)
            last_gate = sample["gate_open"]
            last_counts = len(entries)
    finally:
        orchestrator.stop()
    return samples, {
        "gate_transitions": gate_states,
        "camera_states": camera_states,
        "last_gate": last_gate,
        "entries": last_counts,
    }


def _run_gui(
    config: AppConfig,
    *,
    duration: float,
    dry_run: bool,
    gateway_port: int,
    sampler: _Sampler,
    capture: Optional[str],
):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from app.controller import AppController
    from ui.main_window import MainWindow
    from ui.tiles import CameraTile

    paint_count = [0]
    original_paint = CameraTile.paintEvent

    def counted_paint(self, event):
        paint_count[0] += 1
        return original_paint(self, event)

    CameraTile.paintEvent = counted_paint

    app = QApplication.instance() or QApplication([])
    selected = config if dry_run else _gateway_config(config, gateway_port)
    controller = AppController(
        selected, dry_run=dry_run, mock_gateway=not dry_run, annotate_frames=True
    )
    settings = QSettings(
        str(Path(tempfile.mkdtemp(prefix="smarti_bench_")) / "ui.ini"),
        QSettings.Format.IniFormat,
    )
    window = MainWindow(controller, settings=settings)
    window.show()
    app.processEvents()

    original_tick = controller._on_tick
    gate_states: list[bool] = []
    camera_states: dict[str, list[str]] = {}
    entries: list[dict] = []

    def on_tick(entry: dict) -> None:
        original_tick(entry)
        entries.append(entry)
        gate = bool(entry.get("health_gate_open"))
        if not gate_states or gate_states[-1] != gate:
            gate_states.append(gate)
        for camera_id, camera in (entry.get("cameras") or {}).items():
            states = camera_states.setdefault(camera_id, [])
            state = str(camera.get("state"))
            if not states or states[-1] != state:
                states.append(state)

    controller.orchestrator.on_tick = on_tick
    controller.start()
    started = time.monotonic()
    samples: list[dict[str, Any]] = []
    render_base = paint_count[0]
    render_last_at = started
    try:
        while time.monotonic() - started < duration:
            app.processEvents()
            time.sleep(0.01)
            now = time.monotonic()
            if now - started >= len(samples) * 5.0 + 5.0:
                usage = sampler.read()
                metrics = {
                    p.camera_id: p.latest_metrics()
                    for p in controller.orchestrator.pipelines
                }
                health = controller.orchestrator.manager.health_all()
                elapsed = now - started
                renders = paint_count[0] - render_base
                render_fps = renders / max(0.001, now - render_last_at)
                render_base = paint_count[0]
                render_last_at = now
                sample = {
                    "elapsed_s": round(elapsed, 1),
                    **usage,
                    "source_fps": _mean(
                        [snapshot.fps for snapshot in health.values()]
                    ),
                    "processed_fps": _mean(
                        [m.processing_fps for m in metrics.values()]
                    ),
                    "inference_ms": _mean(
                        [
                            getattr(m, "inference_s", m.latency_s) * 1000
                            for m in metrics.values()
                        ]
                    ),
                    "frame_age_s": _mean(
                        [
                            m.frame_age_s
                            for m in metrics.values()
                            if m.frame_age_s is not None
                        ]
                    ),
                    "dropped_frames": _dropped(controller.orchestrator),
                    "ui_render_fps": round(render_fps, 2),
                    "gate_open": bool(controller.snapshot().gate_open),
                    "gate_flaps": _flaps(gate_states),
                    "camera_flaps": _flaps(
                        [
                            state
                            for states in camera_states.values()
                            for state in states
                        ]
                    ),
                    "heartbeats": None,
                    "heartbeat_gap_s": None,
                    "heartbeat_max_gap_s": None,
                }
                heartbeat_times = _heartbeat_times(controller.orchestrator.gateway)
                if heartbeat_times:
                    gaps = [
                        b - a for a, b in zip(heartbeat_times, heartbeat_times[1:])
                    ]
                    sample["heartbeats"] = len(heartbeat_times)
                    sample["heartbeat_gap_s"] = round(_mean(gaps), 3)
                    sample["heartbeat_max_gap_s"] = round(max(gaps), 3)
                samples.append(sample)
        if capture:
            target = Path(capture)
            target.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(target))
    finally:
        CameraTile.paintEvent = original_paint
        window.hide()
        controller.shutdown()
        app.processEvents()
    return samples, {
        "gate_transitions": gate_states,
        "camera_states": camera_states,
        "last_gate": samples[-1]["gate_open"] if samples else None,
        "entries": len(entries),
    }


def _mean(values) -> Optional[float]:
    values = [value for value in values if value is not None]
    return round(sum(values) / len(values), 3) if values else None


def _flaps(states: list) -> int:
    return max(0, len(states) - 1)


def _dropped(orchestrator: Orchestrator) -> int:
    return int(
        sum(getattr(p, "dropped_frames", 0) for p in orchestrator.pipelines)
    )    def _heartbeat_times(gateway) -> list[float]:
    times = getattr(gateway, "heartbeat_times", None)
    return list(times) if times is not None else []


def run_one(
    name: str,
    config: AppConfig,
    *,
    duration: float,
    warmup_s: float,
    sampler: _Sampler,
    capture: Optional[str],
) -> dict:
    from gateway.mock_gateway import MockGateway

    gateway_cm = None
    gateway = None
    try:
        if name.endswith("mock"):
            gateway_cm = MockGateway(port=0)
            gateway = gateway_cm.__enter__()
        port = gateway.port if gateway is not None else 0
        dry_run = name.endswith("dry")
        started = time.monotonic()
        if name.startswith("gui"):
            samples, diagnostics = _run_gui(
                config,
                duration=duration,
                dry_run=dry_run,
                gateway_port=port,
                sampler=sampler,
                capture=capture,
            )
        else:
            samples, diagnostics = _run_headless(
                config,
                duration=duration,
                dry_run=dry_run,
                gateway_port=port,
                sampler=sampler,
            )
        heartbeat_times = _heartbeat_times(gateway) if gateway is not None else []
        heartbeat_gaps = [
            b - a for a, b in zip(heartbeat_times, heartbeat_times[1:])
        ]
        return {
            "name": name,
            "duration_s": duration,
            "wall_s": round(time.monotonic() - started, 1),
            "dry_run": dry_run,
            "mock_gateway": not dry_run,
            "summary": _summary(samples, warmup_s, started),
            "heartbeat": {
                "count": len(heartbeat_times),
                "mean_gap_s": round(_mean(heartbeat_gaps) or 0.0, 3),
                "max_gap_s": round(max(heartbeat_gaps) if heartbeat_gaps else 0.0, 3),
            },
            "samples": samples,
            **diagnostics,
        }
    finally:
        if gateway_cm is not None:
            gateway_cm.__exit__(None, None, None)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument("--warmup", type=float, default=15.0)
    parser.add_argument("--out", default="docs/benchmarks/latest.json")
    parser.add_argument(
        "--only",
        action="append",
        choices=MODES,
        help="run only these modes (repeatable)",
    )
    parser.add_argument("--capture", help="save a GUI screenshot after the run")
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    selected = tuple(args.only or MODES)
    sampler = _Sampler()
    results = []
    for name in selected:
        print(f"\n=== {name} ({args.duration:.0f}s) ===", flush=True)
        result = run_one(
            name,
            config,
            duration=args.duration,
            warmup_s=args.warmup,
            sampler=sampler,
            capture=args.capture if name.startswith("gui") else None,
        )
        summary = result["summary"]
        print(
            json.dumps(
                {
                    key: summary.get(key)
                    for key in (
                        "mean_tree_cpu_percent",
                        "peak_tree_cpu_percent",
                        "mean_tree_rss_mb",
                        "mean_source_fps",
                        "mean_processed_fps",
                        "mean_inference_ms",
                        "mean_ui_render_fps",
                        "heartbeat_mean_gap_s",
                        "heartbeat_max_gap_s",
                        "gate_flaps",
                        "camera_flaps",
                    )
                }
            ),
            flush=True,
        )
        results.append(result)

    output = {
        "label": args.label,
        "machine": _machine(),
        "config": str(Path(args.config).resolve()),
        "capture": str(Path(args.capture).resolve()) if args.capture else None,
        "results": results,
    }
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\nwrote {target}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
