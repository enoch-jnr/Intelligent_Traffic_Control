"""Live log and decision-record panels with bounded memory."""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from typing import Iterable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.events import LogLine
from ui.theme import Theme

LEVEL_TONE = {
    "DEBUG": "text-secondary",
    "INFO": "accent-text",
    "WARNING": "status-warn",
    "ERROR": "status-critical-text",
    "CRITICAL": "status-critical-text",
}


def format_log_line(line: LogLine) -> str:
    stamp = datetime.fromtimestamp(line.ts).strftime("%Y-%m-%d %H:%M:%S")
    return f"{stamp} {line.level:<7} {line.logger}: {line.message}"


class LiveLogTab(QWidget):
    """Colour-coded, searchable log view with a 5,000-line cap."""

    def __init__(self, theme: Theme, capacity: int = 5000) -> None:
        super().__init__()
        self.theme = theme
        self._all: deque[LogLine] = deque(maxlen=capacity)
        self._levels = {"INFO", "WARN", "ERROR"}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        controls = QHBoxLayout()
        controls.setSpacing(4)
        controls.setContentsMargins(0, 0, 0, 0)
        self.filter_boxes: dict[str, QCheckBox] = {}
        for key in ("INFO", "WARN", "ERROR"):
            box = QCheckBox(key)
            box.setChecked(True)
            box.toggled.connect(
                lambda checked, level=key: self.set_level(level, checked)
            )
            self.filter_boxes[key] = box
            controls.addWidget(box)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search message or logger")
        self.search.textChanged.connect(self._render)
        controls.addWidget(self.search, 1)
        self.search.setFixedHeight(26)
        self.pause_scroll = QPushButton("Pause scroll")
        self.pause_scroll.setCheckable(True)
        self.pause_scroll.setProperty("compact", True)
        controls.addWidget(self.pause_scroll)
        copy = QPushButton("Copy")
        copy.setProperty("compact", True)
        copy.clicked.connect(self._copy)
        controls.addWidget(copy)
        jump = QPushButton("Jump")
        jump.setProperty("compact", True)
        jump.clicked.connect(self._jump)
        controls.addWidget(jump)
        layout.addLayout(controls)

        self.view = QPlainTextEdit()
        self.view.setAccessibleName("Live log")
        self.view.setReadOnly(True)
        self.view.setMinimumHeight(0)
        self.view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored
        )
        self.view.setMaximumBlockCount(capacity)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setPlaceholderText("Log records appear here.")
        layout.addWidget(self.view, 1)

    # -- data --------------------------------------------------------------

    def append_lines(self, lines: Iterable[LogLine]) -> None:
        for line in lines:
            self._all.append(line)
            if self._matches(line):
                self._append(line)

    def set_level(self, level: str, enabled: bool) -> None:
        if enabled:
            self._levels.add(level)
        else:
            self._levels.discard(level)
        self._render()

    def set_search(self, text: str) -> None:
        self.search.setText(text)

    def visible_lines(self) -> list[str]:
        return [format_log_line(line) for line in self._all if self._matches(line)]

    def clear(self) -> None:
        self._all.clear()
        self.view.clear()

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self._render()

    # -- rendering ---------------------------------------------------------

    def _matches(self, line: LogLine) -> bool:
        if line.level.startswith("WARN"):
            level = "WARN"
        elif line.level in ("ERROR", "CRITICAL"):
            level = "ERROR"
        elif line.level in ("INFO", "DEBUG"):
            level = "INFO"
        else:
            level = line.level
        if level not in self._levels:
            return False
        query = self.search.text().strip().lower()
        if not query:
            return True
        return query in line.message.lower() or query in line.logger.lower()

    def _append(self, line: LogLine) -> None:
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self.theme.tokens[LEVEL_TONE.get(line.level, "text-primary")]))
        cursor.insertText(format_log_line(line) + "\n", fmt)
        if not self.pause_scroll.isChecked():
            self.view.verticalScrollBar().setValue(
                self.view.verticalScrollBar().maximum()
            )

    def _render(self) -> None:
        self.view.clear()
        for line in self._all:
            if not self._matches(line):
                continue
            cursor = self.view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            fmt = QTextCharFormat()
            fmt.setForeground(
                QColor(self.theme.tokens[LEVEL_TONE.get(line.level, "text-primary")])
            )
            cursor.insertText(format_log_line(line) + "\n", fmt)
        if not self.pause_scroll.isChecked():
            self._jump()

    def _copy(self) -> None:
        cursor = self.view.textCursor()
        if cursor.hasSelection():
            self.view.copy()
        else:
            self.view.selectAll()
            self.view.copy()
            cursor.clearSelection()
            self.view.setTextCursor(cursor)

    def _jump(self) -> None:
        self.view.verticalScrollBar().setValue(
            self.view.verticalScrollBar().maximum()
        )


class DecisionsTab(QWidget):
    """Decision records with the full JSON of the selected row."""

    HEADERS = ["Time", "Unit", "Wire", "Green", "Reply", "Latency", "Gate"]

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self.theme = theme
        self._entries: list[dict] = []
        self._signature: tuple | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setAccessibleName("Decision records")
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(0)
        self.table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored
        )
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.currentCellChanged.connect(self._show_selected)
        layout.addWidget(self.table, 1)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(0)
        self.detail.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored
        )
        self.detail.setMaximumHeight(72)
        self.detail.setPlaceholderText("Select a decision to see its full record.")
        layout.addWidget(self.detail)

    def set_entries(self, entries: Sequence[dict]) -> None:
        signature = (
            len(entries),
            entries[-1].get("ts") if entries else None,
        )
        if signature == self._signature:
            return
        self._signature = signature
        self._entries = [dict(entry) for entry in entries]
        self.table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            recommendation = entry.get("recommendation") or {}
            request = entry.get("request") or {}
            latency = request.get("latency_s")
            values = [
                _time(entry.get("ts")),
                recommendation.get("unit_id", "—"),
                recommendation.get("wire_movement", "—"),
                f"{recommendation.get('green_time')}s"
                if recommendation.get("green_time")
                else "—",
                request.get("status") or request.get("reason") or "—",
                f"{latency * 1000:.0f} ms" if isinstance(latency, (int, float)) else "—",
                "OPEN" if entry.get("health_gate_open") else "CLOSED",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                self.table.setItem(row, column, item)
        if self._entries and self.table.currentRow() < 0:
            self.table.selectRow(0)
            self._show_selected(0, 0, -1, -1)

    def selected_entries(self) -> list[dict]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return [self._entries[row] for row in rows if 0 <= row < len(self._entries)]

    def select_ts(self, timestamp: str) -> None:
        for row, entry in enumerate(self._entries):
            if entry.get("ts") == timestamp:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 0))
                self._show_selected(row, 0, -1, -1)
                return

    def _show_selected(self, row: int, _column: int, _prev: int, _prev_col: int) -> None:
        if 0 <= row < len(self._entries):
            self.detail.setPlainText(
                json.dumps(self._entries[row], indent=2, ensure_ascii=False)
            )

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme


class LogPanel(QTabWidget):
    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self.live = LiveLogTab(theme)
        self.decisions = DecisionsTab(theme)
        self.addTab(self.live, "Live log")
        self.addTab(self.decisions, "Decisions")
        self.setDocumentMode(True)
        self.setMinimumHeight(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored
        )

    def append_logs(self, lines: Iterable[LogLine]) -> None:
        self.live.append_lines(lines)

    def set_decisions(self, entries: Sequence[dict]) -> None:
        self.decisions.set_entries(entries)

    def set_theme(self, theme: Theme) -> None:
        self.live.set_theme(theme)
        self.decisions.set_theme(theme)


def _time(value: object) -> str:
    if not isinstance(value, str):
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%H:%M:%S")
    except ValueError:
        return value
