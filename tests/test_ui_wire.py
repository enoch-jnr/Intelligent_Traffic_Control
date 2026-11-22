"""GUI wire-traffic window: exact text and the button that opens it."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from app.config import build_config
from app.controller import AppController
from app.snapshots import WireLineView
from ui.main_window import MainWindow
from ui.wire import WireTrafficDialog


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class _StubController:
    def __init__(self, entries):
        self.entries = entries

    def wire_transcript(self):
        return self.entries


def test_dialog_shows_exact_json_and_direction(qt_app):
    dialog = WireTrafficDialog(
        _StubController(
            (
                WireLineView(
                    at="2026-11-22T00:00:00.000Z",
                    direction="TX",
                    json='{"type":"TRAFFIC_REQUEST","movement":"MAIN"}',
                ),
                WireLineView(
                    at="2026-11-22T00:00:00.120Z",
                    direction="RX",
                    json='{"type":"GATEWAY_ACK","status":"CONTROLLER_ACCEPTED"}',
                ),
            )
        )
    )
    try:
        assert dialog.view.toPlainText() == (
            'TX  2026-11-22T00:00:00.000Z\n'
            '{"type":"TRAFFIC_REQUEST","movement":"MAIN"}\n\n'
            'RX  2026-11-22T00:00:00.120Z\n'
            '{"type":"GATEWAY_ACK","status":"CONTROLLER_ACCEPTED"}'
        )
        assert "2 line(s)" in dialog.status.text()
        assert dialog.copy_button.isEnabled()
    finally:
        dialog.close()


def test_wire_button_opens_the_window(qt_app, tmp_path):
    config = build_config(
        {
            "cameras": [
                {
                    "id": "cam_main_a",
                    "approach": "MAIN_A",
                    "source": "file",
                    "path": "missing.avi",
                }
            ],
            "decision": {
                "units": [{"id": "MAIN", "approaches": ["MAIN_A"]}]
            },
            "logging": {"decision_log": ""},
        }
    )
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    controller = AppController(config, dry_run=True)
    window = MainWindow(controller, settings=settings, theme_name="dark")
    window.show()
    qt_app.processEvents()
    try:
        assert window.wire_button.text() == "Wire traffic…"
        window.wire_button.click()
        qt_app.processEvents()
        assert window._wire_dialog is not None
        assert window._wire_dialog.isVisible()
        assert "No gateway traffic yet" in window._wire_dialog.status.text()
    finally:
        window.close()
        controller.shutdown()
