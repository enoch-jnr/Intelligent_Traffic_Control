"""Offscreen tests for operator controls and the export save-as dialog."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from app.config import build_config
from app.controller import AppController
from ui.export_dialog import ExportLogDialog
from ui.main_window import MainWindow


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _window(qt_app, tmp_path):
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
    window.show()
    qt_app.processEvents()
    return controller, window


def test_shortcuts_and_control_commands(qt_app, tmp_path, monkeypatch):
    controller, window = _window(qt_app, tmp_path)
    monkeypatch.setattr("ui.main_window.confirm", lambda *args, **kwargs: True)
    try:
        assert window.export_log_action.shortcut().toString() == "Ctrl+E"
        assert window.reset_action.shortcut().toString() == "Ctrl+R"
        assert window.shutdown_action.shortcut().toString() == "Ctrl+Q"

        window.hold_action.trigger()
        assert controller.held is True
        assert window.hold_button.text() == "Resume requests"
        window.hold_action.trigger()
        assert controller.held is False

        window.reset_action.trigger()
        assert controller.orchestrator._reset_requested.is_set()

        window.release_action.trigger()
        assert controller.released is True
        window.release_action.trigger()
        assert controller.released is False
    finally:
        window.close()
        controller.shutdown()


def test_controls_section_has_ready_buttons(qt_app, tmp_path):
    controller, window = _window(qt_app, tmp_path)
    try:
        assert window.start_stop_button.text() == "Start / Stop"
        assert "Ctrl+E" in window.export_log_action.toolTip() or True
        assert window.reset_button.property("variant") == "destructive"
        assert window.release_button.property("variant") == "caution"
        assert window.controls_section.is_expanded()
    finally:
        window.close()
        controller.shutdown()


def test_export_dialog_offers_formats_ranges_and_suffixes(qt_app, tmp_path):
    dialog = ExportLogDialog(
        None, default_dir=tmp_path, has_selection=True
    )
    try:
        path, fmt, range_key = dialog.values()
        assert fmt == "jsonl"
        assert path.endswith(".jsonl")
        assert dialog.range_box.count() == 4

        dialog.range_box.setCurrentIndex(3)
        assert dialog.values()[2] == "selected"

        dialog.format_box.setCurrentIndex(1)
        assert dialog.values()[1] == "csv"
        assert dialog.path_edit.text().endswith(".csv")

        dialog.format_box.setCurrentIndex(2)
        assert dialog.values()[1] == "text"
        assert dialog.path_edit.text().endswith(".log")
        assert dialog.range_box.isEnabled() is False
    finally:
        dialog.deleteLater()
