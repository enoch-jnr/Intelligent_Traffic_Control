"""Performance profile, load shedding and streaming export tests."""

from __future__ import annotations

import json
import time

import numpy as np

from app.config import build_config
from app.exporters import export_log_file, iter_entries
from app.orchestrator import Orchestrator
from camera.file_source import FileSource
from pipeline.camera_pipeline import CameraPipeline, PipelineConfig
from tests.fakes import FakeResult


def _raw(**overrides):
    raw = {
        "cameras": [
            {
                "id": "cam_main_a",
                "approach": "MAIN_A",
                "source": "file",
                "path": "missing.avi",
                "target_fps": 30,
            }
        ],
        "decision": {"units": [{"id": "MAIN", "approaches": ["MAIN_A"]}]},
    }
    raw.update(overrides)
    return raw


def test_performance_presets_and_overrides():
    low = build_config(_raw(performance={"profile": "low"}))
    assert low.performance.profile == "low"
    assert (low.performance.target_fps, low.performance.imgsz) == (3.0, 480)
    assert (low.performance.preview_fps, low.performance.preview_scale) == (5.0, 0.5)
    assert low.performance.threads == 1
    assert low.performance.analysis_max_width == 960

    high = build_config(
        _raw(
            performance={
                "profile": "high",
                "target_fps": 6,
                "imgsz": 512,
                "threads": 3,
            }
        )
    )
    assert high.performance.target_fps == 6.0
    assert high.performance.imgsz == 512
    assert high.performance.analysis_max_width == 1024
    assert high.performance.threads == 3


def test_auto_profile_uses_memory(monkeypatch):
    monkeypatch.setattr("app.config.total_memory_gb", lambda: 6.3)
    small = build_config(_raw(performance={"profile": "auto"}))
    assert small.performance.profile == "low"
    assert small.performance.requested_profile == "auto"

    monkeypatch.setattr("app.config.total_memory_gb", lambda: 16.0)
    large = build_config(_raw(performance={"profile": "auto"}))
    assert large.performance.profile == "balanced"


def test_invalid_performance_values_are_rejected():
    for raw in (
        {"profile": "turbo"},
        {"profile": "low", "target_fps": 0},
        {"profile": "low", "imgsz": 200},
        {"profile": "low", "preview_scale": 2},
        {"profile": "low", "threads": 0},
        {"profile": "low", "min_fps": 10, "target_fps": 3},
    ):
        try:
            build_config(_raw(performance=raw))
        except Exception:
            continue
        raise AssertionError(f"accepted invalid performance settings: {raw}")


def test_pipeline_target_is_capped_by_profile():
    config = build_config(_raw(performance={"profile": "low"}))
    orchestrator = Orchestrator(
        config, display=False, detector_factory=lambda _camera: object()
    )
    assert orchestrator.pipelines[0].target_fps == 3.0


def test_runtime_thread_caps_are_applied(monkeypatch):
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "app.orchestrator.configure_runtime_threads",
        lambda torch_threads, cv_threads: calls.append(
            (torch_threads, cv_threads)
        ),
    )
    config = build_config(
        _raw(performance={"profile": "low", "threads": 2, "cv_threads": 1})
    )
    orchestrator = Orchestrator(
        config, display=False, detector_factory=lambda _camera: object()
    )
    orchestrator.start()
    try:
        assert calls == [(2, 1)]
    finally:
        orchestrator.stop()


class _SlowDetector:
    names = {2: "car"}

    def __init__(self, delay: float = 0.32) -> None:
        self.delay = delay

    def detect(self, _frame):
        time.sleep(self.delay)
        return [FakeResult(None)]


def _frame(index: int):
    from camera.base import Frame

    return Frame(
        camera_id="cam",
        image=np.zeros((240, 320, 3), dtype=np.uint8),
        captured_at=time.monotonic(),
        index=index,
    )


def test_load_shedding_steps_down_and_logs(caplog):
    pipeline = CameraPipeline(
        "cam",
        "MAIN_A",
        FileSource("cam", "missing.avi", realtime=False),
        _SlowDetector(),
        PipelineConfig(target_fps=4, min_fps=2, adaptive_fps=True),
    )
    with caplog.at_level("WARNING"):
        for index in range(1, 7):
            pipeline.process_frame(_frame(index))
    assert pipeline.target_fps == 3.0
    assert pipeline.overloaded is True
    assert "overloaded: reduced" in caplog.text


def test_dropped_frames_count_source_gaps():
    pipeline = CameraPipeline(
        "cam",
        "MAIN_A",
        FileSource("cam", "missing.avi", realtime=False),
        _SlowDetector(delay=0),
        PipelineConfig(target_fps=30, adaptive_fps=False),
    )
    pipeline.process_frame(_frame(1))
    pipeline.process_frame(_frame(5))
    assert pipeline.dropped_frames == 3


def test_export_log_file_streams_and_filters(tmp_path):
    from datetime import datetime, timedelta, timezone

    source = tmp_path / "decisions.jsonl"
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(
        timespec="milliseconds"
    )
    source.write_text(
        json.dumps({"ts": now, "recommendation": {"unit_id": "MAIN"}})
        + "\n"
        + json.dumps({"ts": old, "recommendation": {"unit_id": "OLD"}})
        + "\n",
        encoding="utf-8",
    )
    target = tmp_path / "out.csv"
    count = export_log_file(source, target, "csv", "1h")
    assert count == 1
    assert "MAIN" in target.read_text(encoding="utf-8")
    assert "OLD" not in target.read_text(encoding="utf-8")
    assert len(list(iter_entries(source))) == 2
