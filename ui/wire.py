"""Live view of the exact JSON lines exchanged with the gateway."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from app.snapshots import WireLineView


class WireTrafficDialog(QDialog):
    """Modeless transcript of the gateway's newline-delimited JSON."""

    def __init__(self, controller, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Gateway wire traffic — exact JSON")
        self.setModal(False)
        self.resize(900, 620)

        layout = QVBoxLayout(self)
        note = QLabel(
            "Exact JSON lines as sent (TX) and received (RX). "
            "TCP newline framing is not shown. Heartbeats and handshakes are included."
        )
        note.setWordWrap(True)
        note.setObjectName("helper")
        layout.addWidget(note)

        self.status = QLabel("No gateway traffic yet.")
        self.status.setObjectName("helper")
        layout.addWidget(self.status)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setObjectName("wireTranscript")
        self.view.setAccessibleName("Gateway wire transcript")
        layout.addWidget(self.view, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.copy_button = self.buttons.addButton(
            "Copy all", QDialogButtonBox.ButtonRole.ActionRole
        )
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy_all)
        self.buttons.rejected.connect(self.reject)
        self.buttons.accepted.connect(self.accept)
        layout.addWidget(self.buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        entries: tuple[WireLineView, ...] = self.controller.wire_transcript()
        text = "\n\n".join(
            f"{entry.direction}  {entry.at}\n{entry.json}" for entry in entries
        )
        if text == self.view.toPlainText():
            return

        scroll = self.view.verticalScrollBar()
        follow = scroll.value() >= scroll.maximum() - 2
        self.view.setPlainText(text)
        if follow or not entries:
            scroll.setValue(scroll.maximum())
        self.copy_button.setEnabled(bool(entries))
        self.status.setText(
            f"{len(entries)} line(s); newest {entries[-1].at}"
            if entries
            else "No gateway traffic yet. Start the host to see TX/RX lines."
        )

    def _copy_all(self) -> None:
        QApplication.clipboard().setText(self.view.toPlainText())

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._timer.stop()
        super().hideEvent(event)
