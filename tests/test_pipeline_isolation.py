"""Two pipelines fed different videos must not share any tracking state."""

from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from camera.file_source import FileSource
from pipeline.camera_pipeline import CameraPipeline, PipelineConfig
from tests.fakes import GreenRectDetector


def _frames(path: Path) -> list[np.ndarray]:
    capture = cv2.VideoCapture(str(path))
    frames: list[np.ndarray] = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(frame)
    capture.release()
    assert frames, f"no frames decoded from {path}"
    return frames


def _build_pipeline(camera_id: str, approach_id: str, path: Path) -> CameraPipeline:
    source = FileSource(camera_id, path, loop=False, realtime=False)
    return CameraPipeline(
        camera_id,
        approach_id,
        source,
        GreenRectDetector(track_id=1),
        PipelineConfig(
            count_line_fraction=0.8,
            target_fps=1000.0,
            window_seconds=30.0,
        ),
    )


def test_two_pipelines_keep_track_and_counter_state_separate(make_video):
    # A crosses the counting line; B stays in the region but above the line.
    crossing_path = make_video(
        "crossing.avi", start_y=20, step_y=15, frames=25, moving=False
    )
    idle_path = make_video(
        "idle.avi", start_x=100, start_y=72, step_y=0, frames=25, moving=False
    )

    pipeline_a = _build_pipeline("cam_a", "MAIN_A", crossing_path)
    pipeline_b = _build_pipeline("cam_b", "SEC", idle_path)

    # Structural isolation: nothing shared.
    assert pipeline_a.tracker is not pipeline_b.tracker
    assert pipeline_a.counter is not pipeline_b.counter
    assert pipeline_a.detector is not pipeline_b.detector
    assert pipeline_a.congestion is not pipeline_b.congestion

    for index, frame in enumerate(_frames(crossing_path)):
        pipeline_a.process_frame(
            _frame_wrapper("cam_a", frame, index)
        )
    for index, frame in enumerate(_frames(idle_path)):
        pipeline_b.process_frame(
            _frame_wrapper("cam_b", frame, index)
        )

    # A saw one crossing for track id 1.
    assert pipeline_a.counter.get_statistics()["vehicles"] == 1
    assert 1 in pipeline_a.counter.counted_ids

    # B sees the same track id in its own region, above the line: no crossing,
    # and no inherited "already counted" state from A.
    assert pipeline_b.counter.get_statistics()["vehicles"] == 0
    assert 1 not in pipeline_b.counter.counted_ids

    # Per-approach metrics stay separate and carry the right identity.
    metrics_a = pipeline_a.latest_metrics()
    metrics_b = pipeline_b.latest_metrics()
    assert metrics_a.approach_id == "MAIN_A"
    assert metrics_b.approach_id == "SEC"
    assert metrics_a.camera_id == "cam_a"
    assert metrics_b.camera_id == "cam_b"
    assert metrics_a.vehicle_count_in_region >= 1
    assert metrics_b.vehicle_count_in_region >= 1

    # Tracker histories are independent objects with different centers.
    assert pipeline_a.tracker.previous_centers is not pipeline_b.tracker.previous_centers
    assert pipeline_a.tracker.previous_centers[1] != pipeline_b.tracker.previous_centers[1]


def _frame_wrapper(camera_id: str, image: np.ndarray, index: int):
    from camera.base import Frame

    return Frame(
        camera_id=camera_id,
        image=image,
        captured_at=time.monotonic(),
        index=index + 1,
    )
