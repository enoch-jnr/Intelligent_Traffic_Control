"""One registry for every operator shortcut.

Menus own the QActions; this module attaches the key sequence, keeps the
tooltip and Help table in sync, and rejects duplicate bindings at construction.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QLineEdit, QWidget


@dataclass(frozen=True)
class ShortcutSpec:
    sequence: str
    description: str
    action: QAction

    @property
    def keys(self) -> str:
        return QKeySequence(self.sequence).toString(QKeySequence.SequenceFormat.NativeText)


class ShortcutRegistry:
    """Attach and describe the window-wide shortcuts."""

    def __init__(self, window: QWidget) -> None:
        self.window = window
        self._specs: list[ShortcutSpec] = []
        self._seen: set[str] = set()

    def register(self, action: QAction, sequence: str, description: str) -> QAction:
        canonical = QKeySequence(sequence).toString(
            QKeySequence.SequenceFormat.PortableText
        )
        if not canonical:
            raise ValueError(f"empty shortcut for {description!r}")
        if canonical in self._seen:
            raise ValueError(f"duplicate shortcut {canonical!r}")
        self._seen.add(canonical)
        action.setShortcut(QKeySequence(sequence))
        action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        action.setToolTip(f"{description} ({canonical})")
        action.setStatusTip(description)
        self._specs.append(ShortcutSpec(sequence, description, action))
        return action

    @property
    def specs(self) -> tuple[ShortcutSpec, ...]:
        return tuple(self._specs)

    def sequence_for(self, action: QAction) -> QKeySequence:
        for spec in self._specs:
            if spec.action is action:
                return QKeySequence(spec.sequence)
        return QKeySequence()

    def blocks_key(self, event) -> bool:
        """True when a registered key is pressed while typing in a text field."""
        if not isinstance(self.window.focusWidget(), QLineEdit):
            return False
        pressed = QKeySequence(event.keyCombination())
        return any(
            QKeySequence(spec.sequence) == pressed for spec in self._specs
        )
