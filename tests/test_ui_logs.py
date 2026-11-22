"""Offscreen tests for the log and decisions panels."""

from __future__ import annotations

import json
import logging
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from app.config import build_config
from app.controller import AppController
from app.events import LogLine
from ui.logs import DecisionsTab, LiveLogTab
from ui.main_window import MainWindow
from ui.theme import DARK


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _line(level: str, message: str, seq: int = 1) -> LogLine:
    return LogLine(seq, time.time(), level, "tests.logger", message)


def test_live_log_filters_search_and_caps_memory(qt_app):
    tab = LiveLogTab(DARK, capacity=10)
    tab.append_lines(
        [
            _line("INFO", "camera started", 1),
            _line("WARNING", "gate closed", 2),
            _line("ERROR", "gateway timeout", 3),
            _line("INFO", "camera live", 4),
        ]
    )
    assert "camera started" in tab.view.toPlainText()
    assert "gate closed" in tab.view.toPlainText()

    tab.set_level("WARN", False)
    assert "gate closed" not in tab.view.toPlainText()
    assert "camera started" in tab.view.toPlainText()

    tab.set_search("timeout")
    assert "gateway timeout" in tab.view.toPlainText()
    assert "camera started" not in tab.view.toPlainText()

    tab.set_search("")
    tab.set_level("WARN", True)
    tab.append_lines([_line("INFO", f"line {index}", index + 10) for index in range(20)])
    assert len(tab._all) == 10
    assert tab.view.blockCount() <= 11
    assert len(tab.visible_lines()) == 10


def _entry() -> dict:
    return {
        "ts": "2026-11-20T10:00:00.000+00:00",
        "health_gate_open": True,
        "recommendation": {
            "unit_id": "MAIN",
            "wire_movement": "MAIN",
            "green_time": 14,
        },
        "request": {
            "status": "CONTROLLER_ACCEPTED",
            "latency_s": 0.018,
            "accepted": True,
        },
    }


def test_decisions_table_shows_full_json(qt_app):
    tab = DecisionsTab(DARK)
    tab.set_entries([_entry()])
    assert tab.table.rowCount() == 1
    assert tab.table.item(0, 1).text() == "MAIN"
    tab.table.selectRow(0)
    tab._show_selected(0, 0, -1, -1)
    payload = json.loads(tab.detail.toPlainText())
    assert payload["recommendation"]["green_time"] == 14
    assert len(tab.selected_entries()) == 1


def test_window_consumes_log_records(qt_app, tmp_path):
    from PySide6.QtCore import QSettings

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
            "decision": {"units": [{"id": "MAIN", "approaches": ["MAIN_A"]}]},
        }
    )
    controller = AppController(config, dry_run=True)
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    window = MainWindow(controller, settings=settings)
    try:
        logging.getLogger("tests.window").warning("visible operator warning")
        controller._events.publish("camera", "cam_main_a", "LIVE", "fps 8")
        window._refresh_panels()
        assert "visible operator warning" in window.log_panel.live.view.toPlainText()
        assert window.events_table.rowCount() == 1
        assert window.events_table.item(0, 1).text() == "cam_main_a"
    finally:
        window.close()
        controller.shutdown()
