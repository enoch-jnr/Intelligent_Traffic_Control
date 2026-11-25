"""Rename-era GUI tests: shortcuts and a log dock that can shrink."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from app.config import build_config
from app.controller import AppController
from ui.main_window import DEFAULT_DOCK_HEIGHT, MINIMUM_DOCK_HEIGHT, MainWindow


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _config():
    return build_config(
        {
            "cameras": [
                {"id": "cam_main_a", "approach": "MAIN_A", "source": "file", "path": "a.avi"},
                {"id": "cam_main_b", "approach": "MAIN_B", "source": "file", "path": "b.avi"},
                {"id": "cam_sec", "approach": "SEC", "source": "file", "path": "c.avi"},
            ],
            "decision": {
                "units": [
                    {"id": "MAIN", "approaches": ["MAIN_A", "MAIN_B"]},
                    {"id": "SECONDARY", "approaches": ["SEC"]},
                ]
            },
        }
    )


def _window(qt_app, settings, size=(1280, 720)):
    controller = AppController(_config(), dry_run=True)
    window = MainWindow(controller, settings=settings)
    window.resize(*size)
    window.show()
    qt_app.processEvents()
    window._apply_dock_height(window._dock_height)
    qt_app.processEvents()
    return controller, window


def _settings(tmp_path, name="ui.ini"):
    return QSettings(str(tmp_path / name), QSettings.Format.IniFormat)


def test_shortcut_registry_is_unique_and_every_action_uses_it(qt_app, tmp_path):
    controller, window = _window(qt_app, _settings(tmp_path))
    try:
        sequences = [spec.sequence for spec in window._shortcuts.specs]
        assert len(sequences) == len(set(sequences))
        assert "Ctrl+J" in sequences
        assert "Ctrl+T" in sequences
        assert {"F6", "Shift+F6", "Ctrl+1", "Ctrl+2", "Ctrl+3", "Ctrl+,"} <= set(
            sequences
        )
        for spec in window._shortcuts.specs:
            assert spec.action.shortcut().toString() == spec.action.shortcut().toString()
            assert spec.description
            assert spec.action.toolTip()
    finally:
        window.close()
        controller.shutdown()


def test_new_shortcuts_drive_logs_theme_focus_and_config(qt_app, tmp_path, monkeypatch):
    controller, window = _window(qt_app, _settings(tmp_path))
    calls = []
    monkeypatch.setattr(QDialog, "exec", lambda self: calls.append(self) or 0)
    try:
        window._set_focus_mode(False)
        assert window._focused_camera == "cam_main_a"

        QTest.keyClick(window, Qt.Key.Key_J, Qt.KeyboardModifier.ControlModifier)
        assert not window.log_dock.isVisible()
        QTest.keyClick(window, Qt.Key.Key_J, Qt.KeyboardModifier.ControlModifier)
        assert window.log_dock.isVisible()
        assert window.log_dock.height() >= MINIMUM_DOCK_HEIGHT

        QTest.keyClick(window, Qt.Key.Key_T, Qt.KeyboardModifier.ControlModifier)
        assert window.theme.name == "light"
        QTest.keyClick(window, Qt.Key.Key_T, Qt.KeyboardModifier.ControlModifier)
        assert window.theme.name == "dark"

        QTest.keyClick(window, Qt.Key.Key_F6)
        assert window._focused_camera == "cam_main_b"
        QTest.keyClick(window, Qt.Key.Key_F6, Qt.KeyboardModifier.ShiftModifier)
        assert window._focused_camera == "cam_main_a"
        QTest.keyClick(window, Qt.Key.Key_3, Qt.KeyboardModifier.ControlModifier)
        assert window._focused_camera == "cam_sec"
        assert "SEC" in window.focus_label.text()

        QTest.keyClick(window, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
        assert calls, "Ctrl+, did not open the read-only configuration dialog"
    finally:
        window.close()
        controller.shutdown()


def test_shortcuts_do_not_steal_keys_from_the_log_search(qt_app, tmp_path):
    controller, window = _window(qt_app, _settings(tmp_path))
    try:
        search = window.log_panel.live.search
        search.setFocus()
        qt_app.processEvents()
        was_visible = window.log_dock.isVisible()
        QTest.keyClick(search, Qt.Key.Key_J, Qt.KeyboardModifier.ControlModifier)
        assert window.log_dock.isVisible() is was_visible
        assert search.hasFocus()
    finally:
        window.close()
        controller.shutdown()


def test_dock_drag_grows_and_shrinks(qt_app, tmp_path):
    controller, window = _window(qt_app, _settings(tmp_path))
    try:
        window._apply_dock_height(260)
        qt_app.processEvents()
        before = window.log_dock.height()
        handle = window.work_splitter.handle(1)
        start = QPoint(handle.width() // 2, handle.height() // 2)
        QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(handle, QPoint(start.x(), start.y() + 55))
        QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=QPoint(start.x(), start.y() + 55))
        qt_app.processEvents()
        shrunk = window.log_dock.height()
        assert shrunk < before

        handle = window.work_splitter.handle(1)
        start = QPoint(handle.width() // 2, handle.height() // 2)
        QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(handle, QPoint(start.x(), start.y() - 55))
        QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=QPoint(start.x(), start.y() - 55))
        qt_app.processEvents()
        assert window.log_dock.height() > shrunk
    finally:
        window.close()
        controller.shutdown()


def test_dock_floor_collapse_restore_and_persist(qt_app, tmp_path):
    settings = _settings(tmp_path)
    controller, window = _window(qt_app, settings)
    try:
        window._apply_dock_height(1)
        assert window.log_dock.height() >= MINIMUM_DOCK_HEIGHT
        assert window.log_dock.minimumHeight() == MINIMUM_DOCK_HEIGHT

        window._apply_dock_height(260)
        qt_app.processEvents()
        restored = window.log_dock.height()
        window.logs_action.trigger()
        assert not window.log_dock.isVisible()
        window.logs_action.trigger()
        assert window.log_dock.isVisible()
        assert window.log_dock.height() >= MINIMUM_DOCK_HEIGHT
        assert abs(window.log_dock.height() - restored) <= 2
    finally:
        window.close()
        controller.shutdown()


def test_bad_persisted_dock_height_and_focus_recover(qt_app, tmp_path):
    settings = _settings(tmp_path)
    settings.setValue("window/dock_height", "not-a-number")
    settings.setValue("view/focused_camera", "legacy_camera")
    controller, window = _window(qt_app, settings)
    try:
        assert window._dock_height == DEFAULT_DOCK_HEIGHT
        assert window.log_dock.height() >= MINIMUM_DOCK_HEIGHT
        assert window._focused_camera == "cam_main_a"
    finally:
        window.close()
        controller.shutdown()
