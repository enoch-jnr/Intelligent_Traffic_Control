"""End-to-end: three simulated cameras -> decisions -> mock gateway -> JSONL.

No GPU, camera or real network: file sources replay synthetic clips and the
mock gateway implements the protocol rules.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from app.config import build_config
from app.orchestrator import Orchestrator
from gateway.mock_gateway import MockGateway
from tests.fakes import FakeBoxes, FakeResult


class AlternatingDetector:
    """Reports one vehicle, then two, so demand (and green time) changes."""

    names = {2: "car"}

    def __init__(self) -> None:
        self.calls = 0

    def detect(self, frame: np.ndarray) -> list[FakeResult]:
        self.calls += 1
        count = 2 if (self.calls // 8) % 2 == 0 else 1
        coordinates = [[80.0, 100.0, 120.0, 150.0], [160.0, 100.0, 200.0, 150.0]]
        boxes = FakeBoxes(
            ids=list(range(1, count + 1)),
            classes=[2] * count,
            confidences=[0.9] * count,
            coordinates=coordinates[:count],
        )
        return [FakeResult(boxes)]


class FixedCountDetector:
    """Reports a base number of vehicles, toggling by one every 8 calls."""

    names = {2: "car"}

    def __init__(self, base: int) -> None:
        self.base = base
        self.calls = 0

    def detect(self, frame: np.ndarray) -> list[FakeResult]:
        self.calls += 1
        count = self.base + (1 if (self.calls // 8) % 2 else 0)
        coordinates = [
            [40.0 + 40 * index, 100.0, 80.0 + 40 * index, 150.0]
            for index in range(count)
        ]
        boxes = FakeBoxes(
            ids=list(range(1, count + 1)),
            classes=[2] * count,
            confidences=[0.9] * count,
            coordinates=coordinates,
        )
        return [FakeResult(boxes)]


def _camera(camera_id: str, approach: str, path: Path) -> dict:
    return {
        "id": camera_id,
        "approach": approach,
        "source": "file",
        "path": str(path),
        "loop": True,
        "realtime": True,
        "target_fps": 30,
        "stale_after_s": 2.0,
    }


def _gateway_settings(gateway: MockGateway, **extra) -> dict:
    settings = {
        "host": "127.0.0.1",
        "port": gateway.port,
        "connect_timeout_s": 0.5,
        "handshake_timeout_s": 1.0,
        "request_timeout_s": 1.0,
        "heartbeat_interval_s": 0.05,
        "reconnect_min_s": 0.05,
        "reconnect_max_s": 0.2,
        "request_backoff_s": 0.05,
    }
    settings.update(extra)
    return settings



def test_three_cameras_to_mock_gateway(make_video, tmp_path):
    main_clip = make_video("main.avi", frames=40, start_x=100, start_y=20, step_y=8, moving=False)
    sec_a_clip = make_video("sec_a.avi", frames=40, start_x=80, start_y=20, step_y=8, moving=False)
    sec_b_clip = make_video("sec_b.avi", frames=40, start_x=120, start_y=20, step_y=8, moving=False)
    log_path = tmp_path / "decisions.jsonl"

    with MockGateway(port=0) as gateway:
        raw = {
            "cameras": [
                _camera("cam_main_a", "MAIN_A", main_clip),
                _camera("cam_main_b", "MAIN_B", sec_a_clip),
                _camera("cam_sec", "SEC", sec_b_clip),
            ],
            "decision": {
                "units": [
                    {"id": "MAIN", "approaches": ["MAIN_A", "MAIN_B"]},
                    {"id": "SECONDARY", "approaches": ["SEC"]},
                ],
                "smoothing_window_s": 0,
                "min_interval_s": 0,
                "min_delta_s": 0,
                "idle_score_threshold": 0.5,
            },
            "gateway": _gateway_settings(gateway),
            "health": {
                "tick_interval_s": 0.25,
                "max_frame_age_s": 2.0,
                "tick_timeout_s": 2.0,
            },
            "logging": {"decision_log": str(log_path)},
        }
        config = build_config(raw)
        orchestrator = Orchestrator(
            config,
            display=False,
            detector_factory=lambda camera: AlternatingDetector(),
        )
        orchestrator.run(duration_s=3.0)

        requests = gateway.traffic_requests

    # The gateway accepted real traffic requests with valid, increasing sequences.
    assert requests, "no traffic request reached the gateway"
    sequences = [item["sequence"] for item in requests]
    assert sequences == list(range(1, len(sequences) + 1))
    for item in requests:
        assert item["movement"] in ("MAIN", "SECONDARY")
        assert item["priority"] == "NORMAL"
        assert 5 <= item["green_time"] <= 30

    # Every cycle is logged, including cycles that emitted nothing.
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(entries) >= 5
    for entry in entries:
        assert entry["approach_layout"] == "v2"
        assert set(entry["approaches"]) == {"MAIN_A", "MAIN_B", "SEC"}
        assert set(entry["units"]) == {"MAIN", "SECONDARY"}
        assert set(entry["decision"]["unit_scores"]) <= set(entry["units"])
        assert entry["decision"]["reasons"]
        assert set(entry["decision"]["expected_red_s"]) == set(entry["units"])
        assert set(entry["cameras"]) == {"cam_main_a", "cam_main_b", "cam_sec"}
        if entry["recommendation"] is not None:
            assert entry["recommendation"]["unit_id"] in entry["units"]
            assert entry["recommendation"]["expected_red_s"] == (
                entry["decision"]["expected_red_s"]
            )

    accepted = [
        entry
        for entry in entries
        if entry["request"] and entry["request"].get("accepted")
    ]
    assert accepted, "no request was accepted by the mock gateway"
    assert entries[-1]["health_gate_open"] is True
    assert all(entry["approaches"]["SEC"]["healthy"] for entry in entries[-3:])

    # All three cameras were fresh and the gateway saw heartbeats.
    assert entries[-1]["cameras"]["cam_main_a"]["state"] == "LIVE"
    assert orchestrator.gateway is not None
    assert orchestrator.gateway.connected is False  # stopped cleanly


def test_three_phase_against_hypothetical_gateway(make_video, tmp_path):
    """The target layout, against a mock that accepts the proposed names.

    The mock is hypothetical: it stands in for firmware that does not exist.
    """
    clips = {
        "MAIN_A": make_video("main.avi", frames=40, start_x=100, start_y=20, step_y=8, moving=False),
        "MAIN_B": make_video("sec_a.avi", frames=40, start_x=80, start_y=20, step_y=8, moving=False),
        "SEC": make_video("sec_b.avi", frames=40, start_x=120, start_y=20, step_y=8, moving=False),
    }
    log_path = tmp_path / "decisions_three_phase.jsonl"
    proposed = {
        "MAIN_A": "MAIN_A",
        "MAIN_B": "MAIN_B",
        "SEC": "SECONDARY",
    }

    with MockGateway(
        port=0, movements=["MAIN_A", "MAIN_B", "SECONDARY"]
    ) as gateway:
        gateway_settings = _gateway_settings(gateway)
        gateway_settings["mode"] = "three_phase"
        gateway_settings["three_phase"] = {
            "enabled": True,
            "movements": proposed,
        }
        raw = {
            "cameras": [
                _camera("cam_main_a", "MAIN_A", clips["MAIN_A"]),
                _camera("cam_main_b", "MAIN_B", clips["MAIN_B"]),
                _camera("cam_sec", "SEC", clips["SEC"]),
            ],
            "decision": {
                "units": [
                    {"id": "MAIN_A", "approaches": ["MAIN_A"]},
                    {"id": "MAIN_B", "approaches": ["MAIN_B"]},
                    {"id": "SEC", "approaches": ["SEC"]},
                ],
                "smoothing_window_s": 0,
                "min_interval_s": 0,
                "min_delta_s": 0,
                "idle_score_threshold": 0.5,
            },
            "gateway": gateway_settings,
            "health": {
                "tick_interval_s": 0.25,
                "max_frame_age_s": 2.0,
                "tick_timeout_s": 2.0,
            },
            "logging": {"decision_log": str(log_path)},
        }
        config = build_config(raw)
        counts = {"MAIN_A": 4, "MAIN_B": 1, "SEC": 2}
        orchestrator = Orchestrator(
            config,
            display=False,
            detector_factory=lambda camera: FixedCountDetector(
                counts[camera.approach]
            ),
        )
        orchestrator.run(duration_s=3.0)
        requests = gateway.traffic_requests

    assert requests, "no request reached the hypothetical gateway"
    assert [item["sequence"] for item in requests] == list(
        range(1, len(requests) + 1)
    )
    # MAIN_A has the most demand, and its proposal name is what goes on the wire.
    assert {item["movement"] for item in requests} == {"MAIN_A"}
    assert all(item["priority"] == "NORMAL" for item in requests)
    assert all(5 <= item["green_time"] <= 30 for item in requests)

    entries = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert entries
    for entry in entries:
        assert entry["approach_layout"] == "v2"
        assert entry["mode"] == "three_phase"
        assert set(entry["units"]) == {"MAIN_A", "MAIN_B", "SEC"}
        assert set(entry["approaches"]) == {"MAIN_A", "MAIN_B", "SEC"}
        assert set(entry["decision"]["expected_red_s"]) == {
            "MAIN_A",
            "MAIN_B",
            "SEC",
        }
        for unit in entry["units"].values():
            assert unit["expected_red_s"] is not None
        if entry["recommendation"] is not None:
            assert entry["recommendation"]["unit_id"] == "MAIN_A"
            assert entry["recommendation"]["wire_movement"] == "MAIN_A"
    assert entries[-1]["health_gate_open"] is True
    assert orchestrator.gateway is not None
    assert orchestrator.gateway.connected is False
