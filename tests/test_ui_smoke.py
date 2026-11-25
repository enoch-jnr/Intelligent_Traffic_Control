"""Offscreen smoke tests for the console shell (no display server needed)."""

from __future__ import annotations

import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from app.config import build_config
from app.controller import AppController
from app.snapshots import RecommendationView, frozen_mapping
from ui.dialogs import build_confirm
from ui.main_window import MainWindow
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
            "logging": {"decision_log": "logs/decisions.jsonl"},
        }
    )


def _window(qt_app, tmp_path):
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    controller = AppController(_config(), dry_run=True)
    window = MainWindow(controller, settings=settings, theme_name="dark")
    window.show()
    qt_app.processEvents()
    return controller, window


def test_shell_builds_with_menus_tiles_and_chips(qt_app, tmp_path):
    controller, window = _window(qt_app, tmp_path)
    try:
        titles = [action.text() for action in window.menuBar().actions()]
        assert titles == ["File", "View", "Cameras", "Log", "Settings", "Help"]
        assert window.windowTitle() == "Smarti AI Host — Traffic Control Console"
        assert set(window._tiles) == {"cam_main_a", "cam_main_b", "cam_sec"}
        assert window.camera_chip.text.text() == "0/3 cameras"
        assert window.mode_badge.text() == "SIMULATION:DRY-RUN"
        assert window.start_button.text() == "START"
        assert window.playback_section.isVisible()
    finally:
        window.close()
        controller.shutdown()


def test_theme_switch_changes_stylesheet(qt_app, tmp_path):
    controller, window = _window(qt_app, tmp_path)
    try:
        window.switch_theme("light")
        assert window.theme.name == "light"
        assert "#EEF2F5" in QApplication.instance().styleSheet()
        window.switch_theme("dark")
        assert "#222B38" in QApplication.instance().styleSheet()
    finally:
        window.close()
        controller.shutdown()


def test_consequence_dialogs_default_to_cancel(qt_app):
    box = build_confirm(
        None,
        "Reset analysis?",
        "Restart analysis state?",
        confirm_label="Reset",
    )
    try:
        assert box.defaultButton().text() == "Cancel"
        assert box.escapeButton().text() == "Cancel"
    finally:
        box.deleteLater()


def test_camera_tile_tracks_health_and_paints(qt_app):
    tile = CameraTile("cam", "MAIN", "file", theme=DARK)
    tile.resize(640, 360)
    tile.set_health("LIVE", 8.0, 0.1, 0, started=True)
    tile.set_frame(np.zeros((240, 320, 3), dtype=np.uint8))
    assert tile.status_word() == "LIVE"
    assert tile.has_frame()
    assert not tile.grab().isNull()

    tile.set_health("DISCONNECTED", 0.0, 3.2, 2, started=True)
    assert tile.status_word() == "DISCONNECTED"
    assert not tile.grab().isNull()

    tile.set_health("LIVE", 0.0, None, 0, started=False)
    assert tile.status_word() == "STOPPED"


def test_schematic_is_labelled_advisory_and_shows_units(qt_app, tmp_path):
    controller, window = _window(qt_app, tmp_path)
    try:
        tile = window.schematic_tile
        assert set(tile.cards) == {"MAIN_A", "MAIN_B", "SEC"}
        assert "ADVISORY" in tile.advisory.text()

        snapshot = controller.snapshot()
        snapshot = replace(
            snapshot,
            recommendation=RecommendationView(
                unit_id="MAIN",
                wire_movement="MAIN",
                green_time=15,
                reasons=("busy main road",),
                expected_red_s=frozen_mapping({"MAIN": 12.0}),
                sent=True,
                request_status="CONTROLLER_ACCEPTED",
                accepted=True,
            ),
            expected_red_s=frozen_mapping({"MAIN": 12.0, "SECONDARY": 22.0}),
            decision_reasons=("busy main road",),
        )
        tile.set_snapshot(snapshot)
        assert "MAIN 15s" in tile.advisory.text()
        assert "MAIN" in tile.red_label.text()
        assert "busy main road" in tile.reasons.toPlainText()
    finally:
        window.close()
        controller.shutdown()


def test_sidebar_sections_collapse_and_persist(qt_app, tmp_path):
    controller, window = _window(qt_app, tmp_path)
    try:
        window.controls_section.set_expanded(False)
        assert not window.controls_section.is_expanded()
        assert not window.controls_section.body.isVisible()
        window.controls_section.set_expanded(True)
        assert window.controls_section.body.isVisible()
    finally:
        window.close()
        controller.shutdown()
