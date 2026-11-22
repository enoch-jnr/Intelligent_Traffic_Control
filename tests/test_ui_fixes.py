"""Regression tests for video clarity, layout, mode badge and pop-out removal."""

from __future__ import annotations

import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from app.config import build_config
from app.controller import AppController
from tests.test_orchestrator import FixedCountDetector
from ui.main_window import MainWindow, mode_badge_state
from ui.theme import DARK
from ui.tiles import CameraTile


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _config():
    return build_config(
        {
            "cameras": [
                {
                    "id": "cam_main_a",
                    "approach": "MAIN_A",
                    "source": "file",
                    "path": "missing.avi",
                },
                {
                    "id": "cam_main_b",
                    "approach": "MAIN_B",
                    "source": "file",
                    "path": "missing_a.avi",
                },
                {
                    "id": "cam_sec",
                    "approach": "SEC",
                    "source": "file",
                    "path": "missing_b.avi",
                },
            ],
            "decision": {
                "units": [
                    {"id": "MAIN", "approaches": ["MAIN_A", "MAIN_B"]},
                    {"id": "SECONDARY", "approaches": ["SEC"]},
                ]
            },
            "performance": {"profile": "low"},
        }
    )


def _window(qt_app, tmp_path):
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    controller = AppController(
        _config(),
        dry_run=True,
        detector_factory=lambda camera: FixedCountDetector(2),
    )
    window = MainWindow(controller, settings=settings)
    window.resize(1280, 720)
    window.show()
    qt_app.processEvents()
    return controller, window


def _patches() -> np.ndarray:
    image = np.zeros((360, 640, 3), np.uint8)
    image[:, :160] = (0, 0, 255)  # BGR red
    image[:, 160:320] = (0, 255, 0)  # green
    image[:, 320:480] = (255, 0, 0)  # blue
    image[:, 480:] = (255, 255, 255)  # white
    return image


def test_live_video_pixels_have_no_blue_wash(qt_app):
    tile = CameraTile("cam", "MAIN", "file", theme=DARK)
    tile.resize(640, 360)
    tile.set_frame(_patches(), frame_index=1)
    tile.set_health("LIVE", 8.0, 0.1, 0, started=True)
    tile.show()
    qt_app.processEvents()
    image = tile.grab().toImage()
    for x, expected in (
        (80, (255, 0, 0)),
        (240, (0, 255, 0)),
        (400, (0, 0, 255)),
        (560, (255, 255, 255)),
    ):
        actual = image.pixelColor(x, 180).getRgb()[:3]
        assert all(
            abs(channel - want) <= 2
            for channel, want in zip(actual, expected)
        ), (x, actual, expected)
    # LIVE must not receive the stale dim layer at all.
    assert image.pixelColor(240, 180).green() > 200
    tile.close()


def test_stale_video_is_dimmer_than_live(qt_app):
    tile = CameraTile("cam", "MAIN", "file", theme=DARK)
    tile.resize(640, 360)
    tile.set_frame(_patches(), frame_index=1)
    tile.set_health("LIVE", 8.0, 0.1, 0, started=True)
    tile.show()
    qt_app.processEvents()
    live = tile.grab().toImage().pixelColor(400, 180)
    tile.set_health("STALE", 0.0, 3.0, 0, started=True)
    qt_app.processEvents()
    stale = tile.grab().toImage().pixelColor(400, 180)
    assert live.blue() > 240
    assert stale.blue() < live.blue() - 40
    tile.close()


def test_frame_dirty_flag_skips_same_index(qt_app):
    tile = CameraTile("cam", "MAIN", "file", theme=DARK)
    frame = _patches()
    assert tile.set_frame(frame, frame_index=7) is True
    assert tile.set_frame(frame, frame_index=7) is False
    assert tile.rendered_frames() == 1


def test_no_header_or_banner_and_badge_is_always_visible(qt_app, tmp_path):
    controller, window = _window(qt_app, tmp_path)
    try:
        assert window.findChild(type(window.mode_badge), "appTitle") is None
        assert window.findChild(type(window.mode_badge), "banner") is None
        assert window.mode_badge.isVisible()
        assert window.mode_badge.text() == "SIMULATION:DRY-RUN"
        assert window.mode_badge.toolTip()
    finally:
        window.close()
        controller.shutdown()


def test_mode_badge_exact_text_for_each_combination(qt_app, tmp_path):
    controller, window = _window(qt_app, tmp_path)
    try:
        base = controller.snapshot()
        cases = [
            ({"dry_run": True, "mock_gateway": False}, "SIMULATION:DRY-RUN"),
            ({"dry_run": False, "mock_gateway": True}, "SIMULATION:MOCK-GATEWAY"),
            (
                {"dry_run": False, "mock_gateway": False, "three_phase": True},
                "SIMULATION:THREE-PHASE",
            ),
            (
                {
                    "dry_run": False,
                    "mock_gateway": False,
                    "three_phase": False,
                    "file_sources": True,
                },
                "SIMULATION:FILE-SOURCES",
            ),
            (
                {
                    "dry_run": False,
                    "mock_gateway": False,
                    "three_phase": False,
                    "file_sources": False,
                },
                "LIVE:COMPAT-2PH",
            ),
        ]
        for changes, expected in cases:
            text, tone, tooltip = mode_badge_state(replace(base, **changes))
            assert text == expected
            assert tone in ("simulation", "live")
            assert tooltip
    finally:
        window.close()
        controller.shutdown()


def test_layout_gives_log_and_decisions_room_at_1280x720(qt_app, tmp_path):
    controller, window = _window(qt_app, tmp_path)
    try:
        window._apply_dock_height(312)
        qt_app.processEvents()
        assert window.log_dock.height() >= 312
        assert window.log_panel.live.view.viewport().height() >= 240

        window.log_panel.setCurrentWidget(window.log_panel.decisions)
        qt_app.processEvents()
        table = window.log_panel.decisions.table
        assert table.viewport().height() >= 8 * table.verticalHeader().defaultSectionSize()
        assert table.isVisible()
    finally:
        window.close()
        controller.shutdown()


def test_double_click_never_opens_a_window_or_offers_fullscreen(qt_app, tmp_path):
    controller, window = _window(qt_app, tmp_path)
    try:
        tile = window._tiles["cam_main_a"]
        before = set(QApplication.topLevelWidgets())
        event = QMouseEvent(
            QEvent.Type.MouseButtonDblClick,
            QPointF(5, 5),
            QPointF(5, 5),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        tile.mouseDoubleClickEvent(event)
        qt_app.processEvents()
        assert set(QApplication.topLevelWidgets()) == before
        assert not hasattr(tile, "fullscreen_requested")
        assert not hasattr(tile, "double_clicked")
        assert not hasattr(window, "_fullscreen_camera")

        menu, *_rest = tile._make_context_menu()
        labels = [action.text() for action in menu.actions()]
        assert "Show polygon" in labels
        assert "Show counting line" in labels
        assert "Fullscreen" not in labels
        menu.deleteLater()
    finally:
        window.close()
        controller.shutdown()


def test_single_scheduler_and_quality_mapping(qt_app, tmp_path):
    controller, window = _window(qt_app, tmp_path)
    try:
        assert window._scheduler_timer.interval() == 50
        assert not hasattr(window, "_video_timer")
        assert not hasattr(window, "_panel_timer")
        window.set_preview_quality("low")
        assert (window._preview_fps, window._preview_scale) == (4.0, 0.5)
        window.set_preview_quality("medium")
        assert (window._preview_fps, window._preview_scale) == (8.0, 0.75)
        window.set_preview_quality("high")
        assert (window._preview_fps, window._preview_scale) == (12.0, 1.0)
    finally:
        window.close()
        controller.shutdown()


def test_preview_off_converts_and_paints_no_frames(qt_app, tmp_path, monkeypatch):
    controller, window = _window(qt_app, tmp_path)
    try:
        window.set_video_preview(False)
        called = []
        monkeypatch.setattr(
            controller, "frame", lambda *args, **kwargs: called.append(1)
        )
        window._refresh_video()
        assert called == []
        assert window._tiles["cam_main_a"]._image is None
    finally:
        window.close()
        controller.shutdown()
