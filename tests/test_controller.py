"""Controller semantics without Qt, against the mock gateway."""

from __future__ import annotations

import logging
import time

import numpy as np

from app.config import build_config
from app.controller import AppController
from app.events import EventFeed, LogBroadcaster
from gateway.mock_gateway import MockGateway
from tests.conftest import wait_until
from tests.fakes import FakeBoxes, FakeResult


class BusyDetector:
    """Reports a configurable, changeable number of vehicles."""

    names = {2: "car"}

    def __init__(self) -> None:
        self.count = 3

    def detect(self, frame: np.ndarray) -> list[FakeResult]:
        boxes = FakeBoxes(
            ids=list(range(1, self.count + 1)),
            classes=[2] * self.count,
            confidences=[0.9] * self.count,
            coordinates=[
                [40.0 + 30 * index, 100.0, 70.0 + 30 * index, 150.0]
                for index in range(self.count)
            ],
        )
        return [FakeResult(boxes)]


def _config(tmp_path, gateway: MockGateway):
    video = tmp_path / "main.avi"
    writer = __import__("cv2").VideoWriter(
        str(video), __import__("cv2").VideoWriter_fourcc(*"MJPG"), 15.0, (320, 240)
    )
    for _ in range(120):
        image = np.zeros((240, 320, 3), dtype=np.uint8)
        image[100:150, 80:120] = (0, 255, 0)
        writer.write(image)
    writer.release()

    return build_config(
        {
            "cameras": [
                {
                    "id": "cam_main_a",
                    "approach": "MAIN_A",
                    "source": "file",
                    "path": str(video),
                    "loop": True,
                    "realtime": True,
                    "target_fps": 30,
                }
            ],
            "decision": {
                "units": [{"id": "MAIN", "approaches": ["MAIN_A"]}],
                "smoothing_window_s": 0,
                "min_interval_s": 0,
                "min_delta_s": 0,
                "idle_score_threshold": 0.2,
            },
            "gateway": {
                "host": "127.0.0.1",
                "port": gateway.port,
                "connect_timeout_s": 0.5,
                "handshake_timeout_s": 1.0,
                "request_timeout_s": 1.0,
                "heartbeat_interval_s": 0.05,
                "reconnect_min_s": 0.05,
                "reconnect_max_s": 0.2,
                "request_backoff_s": 0.05,
            },
            "health": {
                "tick_interval_s": 0.1,
                "max_frame_age_s": 2.0,
                "tick_timeout_s": 2.0,
            },
            "logging": {"decision_log": str(tmp_path / "decisions.jsonl")},
        }
    )


def _controller(tmp_path, gateway: MockGateway):
    detector = BusyDetector()
    controller = AppController(
        _config(tmp_path, gateway),
        config_path="config.yaml",
        dry_run=False,
        detector_factory=lambda camera: detector,
    )
    return controller, detector


def test_snapshot_is_frozen_and_reports_cameras(tmp_path):
    with MockGateway(port=0) as gateway:
        controller, _detector = _controller(tmp_path, gateway)
        try:
            controller.start()
            assert wait_until(
                lambda: controller.snapshot().approaches
                and controller.snapshot().approaches[0].healthy,
                timeout_s=6.0,
            )
            snapshot = controller.snapshot()
            assert snapshot.cameras[0].camera_id == "cam_main_a"
            assert snapshot.cameras[0].source == "file"
            assert snapshot.cameras[0].state == "LIVE"
            assert snapshot.approaches[0].approach_id == "MAIN_A"
            assert snapshot.mode == "compat_two_phase"
            assert snapshot.simulation is False
            assert snapshot.fresh_cameras == 1
            with __import__("pytest").raises(Exception):
                snapshot.mode = "changed"  # type: ignore[misc]
        finally:
            controller.shutdown()


def test_hold_suppresses_requests_but_keeps_analysis(tmp_path):
    with MockGateway(port=0) as gateway:
        controller, detector = _controller(tmp_path, gateway)
        try:
            controller.start()
            assert wait_until(lambda: gateway.traffic_requests, timeout_s=8.0)
            controller.hold_requests()
            before = len(gateway.traffic_requests)
            detector.count = 5
            assert wait_until(
                lambda: any(
                    (entry.get("request") or {}).get("reason") == "operator_hold"
                    for entry in controller.decisions()
                ),
                timeout_s=6.0,
            )
            assert len(gateway.traffic_requests) == before
            assert controller.snapshot().held is True

            controller.resume_requests()
            detector.count = 2
            assert wait_until(
                lambda: len(gateway.traffic_requests) > before, timeout_s=6.0
            )
        finally:
            controller.shutdown()


def test_release_closes_the_gate_and_stops_heartbeats(tmp_path):
    with MockGateway(port=0) as gateway:
        controller, _detector = _controller(tmp_path, gateway)
        try:
            controller.start()
            assert wait_until(
                lambda: controller.snapshot().gate_open, timeout_s=8.0
            )
            assert wait_until(lambda: gateway.heartbeats >= 2, timeout_s=5.0)
            controller.release_to_autonomous()
            assert wait_until(
                lambda: controller.snapshot().gate_open is False, timeout_s=3.0
            )
            reason = controller.snapshot().gate_reason
            assert "Operator release" in reason

            time.sleep(0.3)
            before = gateway.heartbeats
            time.sleep(0.4)
            assert gateway.heartbeats == before, "heartbeats continued after release"

            controller.resume_control()
            assert wait_until(
                lambda: controller.snapshot().gate_open, timeout_s=4.0
            )
            assert wait_until(
                lambda: gateway.heartbeats > before, timeout_s=4.0
            )
        finally:
            controller.shutdown()


def test_reset_restarts_gateway_session_and_sequence(tmp_path):
    with MockGateway(port=0) as gateway:
        controller, detector = _controller(tmp_path, gateway)
        try:
            controller.start()
            assert wait_until(lambda: gateway.traffic_requests, timeout_s=8.0)
            first_session = max(item["session"] for item in gateway.traffic_requests)
            controller.reset()
            assert wait_until(
                lambda: gateway.sessions > first_session, timeout_s=6.0
            )

            detector.count = 6
            assert wait_until(
                lambda: any(
                    item["session"] > first_session for item in gateway.traffic_requests
                ),
                timeout_s=8.0,
            )
            new_session = [
                item
                for item in gateway.traffic_requests
                if item["session"] > first_session
            ]
            assert new_session[0]["sequence"] == 1
        finally:
            controller.shutdown()


def test_annotated_frame_is_downscaled(tmp_path):
    with MockGateway(port=0) as gateway:
        controller, _detector = _controller(tmp_path, gateway)
        try:
            controller.start()
            assert wait_until(
                lambda: controller.frame("cam_main_a", max_size=160) is not None,
                timeout_s=6.0,
            )
            frame = controller.frame("cam_main_a", max_size=160)
            assert frame is not None
            assert max(frame.shape[:2]) <= 160
        finally:
            controller.shutdown()


def test_shutdown_joins_threads_and_flushes(tmp_path):
    with MockGateway(port=0) as gateway:
        controller, _detector = _controller(tmp_path, gateway)
        controller.start()
        assert wait_until(lambda: gateway.heartbeats >= 1, timeout_s=6.0)
        controller.shutdown()

        assert controller.running is False
        assert not controller._handler_attached
        before = gateway.heartbeats
        time.sleep(0.3)
        assert gateway.heartbeats == before


def test_log_and_event_feeds_are_bounded():
    broadcaster = LogBroadcaster(capacity=3)
    logger = logging.getLogger("tests.bounded")
    logger.addHandler(broadcaster)
    try:
        for index in range(7):
            logger.warning("line %s", index)
        lines, latest = broadcaster.drain()
        assert [line.message for line in lines] == ["line 4", "line 5", "line 6"]
        assert latest == 7
    finally:
        logger.removeHandler(broadcaster)
        broadcaster.close()

    feed = EventFeed(capacity=2)
    for index in range(5):
        feed.publish("test", "subject", f"action {index}")
    events, latest = feed.drain()
    assert [event.action for event in events] == ["action 3", "action 4"]
    assert latest == 5
