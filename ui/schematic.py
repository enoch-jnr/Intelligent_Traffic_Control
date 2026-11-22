"""Advisory T-junction schematic and the last decision, in plain language."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.snapshots import UiSnapshot
from ui.theme import Theme

UNIT_COLOURS = {
    "MAIN_A": "approach-main-a",
    "MAIN_B": "approach-main-b-label",
    "SEC": "approach-sec-label",
    "MAIN": "approach-main-a",
    "SECONDARY": "approach-sec-label",
}
CONGESTION_TONE = {"Low": "ok", "Moderate": "warn", "High": "critical"}


class ApproachCard(QFrame):
    """Compact per-approach metrics under the schematic."""

    def __init__(self, approach: str, theme: Theme) -> None:
        super().__init__()
        self.setObjectName("card")
        self.approach = approach
        self.theme = theme
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 4, 7, 4)
        layout.setSpacing(1)
        self.title = QLabel(approach)
        self.title.setObjectName("helper")
        self.title.setStyleSheet(
            f"color: {theme.tokens.get(UNIT_COLOURS.get(approach, 'text-secondary'), theme.tokens['text-secondary'])};"
            " font-weight: 600;"
        )
        self.value = QLabel("—")
        self.value.setObjectName("largeReadout")
        self.value.setStyleSheet("font-size: 15px;")
        self.hint = QLabel("waiting for metrics")
        self.hint.setObjectName("helper")
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.hint)

    def update_metrics(self, snapshot: UiSnapshot) -> None:
        metrics = snapshot.approach(self.approach)
        if metrics is None:
            self.value.setText("—")
            self.hint.setText("no data")
            return
        self.value.setText(
            f"{metrics.vehicle_count_in_region} veh · {metrics.stationary_count} stop"
        )
        self.hint.setText(
            f"wait {metrics.max_wait_s:.1f}s · {metrics.congestion_level}"
        )
        tone = CONGESTION_TONE.get(metrics.congestion_level, "unknown")
        token = {
            "ok": "status-ok",
            "warn": "status-warn",
            "critical": "status-critical-text",
        }.get(tone, "text-secondary")
        self.hint.setStyleSheet(f"color: {self.theme.tokens[token]};")

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        token = UNIT_COLOURS.get(self.approach, "text-secondary")
        self.title.setStyleSheet(
            f"color: {theme.tokens[token]}; font-weight: 600;"
        )


class SchematicCanvas(QWidget):
    """A vector T junction. It shows a recommendation, never a lit lamp."""

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self.theme = theme
        self._snapshot: Optional[UiSnapshot] = None
        self.setMinimumHeight(100)

    def set_snapshot(self, snapshot: UiSnapshot) -> None:
        self._snapshot = snapshot
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor(self.theme.tokens["bg-field"]))

        width = max(1, rect.width())
        height = max(1, rect.height())
        cx = width / 2
        junction_y = height * 0.60
        road_width = max(26, min(52, int(width * 0.12)))
        road_color = QColor(self.theme.tokens["border"])

        # A T: the top bar carries MAIN_A and MAIN_B; the stem carries SEC.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(road_color)
        painter.drawRect(
            QRectF(0, int(height * 0.22), width, road_width)
        )
        painter.drawRect(
            QRectF(
                cx - road_width / 2,
                int(height * 0.22),
                road_width,
                height - int(height * 0.22),
            )
        )

        snapshot = self._snapshot
        recommended = snapshot.recommendation if snapshot else None
        unit_id = recommended.unit_id if recommended else None
        green = recommended.green_time if recommended else None

        def highlighted(approach: str) -> bool:
            if snapshot is None or unit_id is None:
                return False
            for unit in snapshot.units:
                if unit.unit_id == unit_id:
                    return approach in unit.approaches
            return approach == unit_id

        accent = QColor(self.theme.tokens["accent-text"])
        top_y = int(height * 0.22)
        if highlighted("MAIN_A") or highlighted("MAIN_B"):
            painter.setPen(QPen(accent, 3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(2, top_y + 2, width - 4, road_width - 4))
            if snapshot is not None and not snapshot.three_phase:
                # Both main approaches are one unit on current firmware.
                painter.drawRect(QRectF(4, top_y - 7, width - 8, road_width + 14))
        if highlighted("SEC"):
            painter.setPen(QPen(accent, 3))
            painter.drawRect(
                QRectF(
                    cx - road_width / 2 + 2,
                    top_y + 2,
                    road_width - 4,
                    height - top_y - 4,
                )
            )

        self._label(painter, 8, top_y - 10, "MAIN_A", "approach-main-a")
        self._label(
            painter,
            width - 8,
            top_y - 10,
            "MAIN_B",
            "approach-main-b-label",
            right=True,
        )
        self._label(
            painter,
            int(cx),
            int(height * 0.72),
            "SEC",
            "approach-sec-label",
            center=True,
        )

        if recommended is not None and green is not None:
            text = f"RECOMMENDED {unit_id} · {green}s (advisory)"
            tone = UNIT_COLOURS.get(unit_id, "accent-text")
            self._label(
                painter,
                int(cx),
                int(junction_y) + road_width + 18,
                text,
                tone,
                center=True,
            )
        else:
            self._label(
                painter,
                int(cx),
                int(junction_y) + road_width + 18,
                "No recommendation",
                "text-secondary",
                center=True,
            )

        if snapshot is not None and not snapshot.three_phase:
            self._label(
                painter,
                int(cx),
                height - 8,
                "MAIN_A + MAIN_B → MAIN · SEC → SECONDARY (compat)",
                "status-warn",
                center=True,
                size=9,
            )

    def _label(
        self,
        painter: QPainter,
        x: int,
        y: int,
        text: str,
        token: str,
        *,
        center: bool = False,
        right: bool = False,
        size: int = 10,
    ) -> None:
        font = QFont("Cascadia Mono")
        font.setPixelSize(size)
        font.setBold(True)
        painter.setFont(font)
        if center:
            x -= painter.fontMetrics().horizontalAdvance(text) // 2
        elif right:
            x -= painter.fontMetrics().horizontalAdvance(text)
        painter.setPen(QColor(self.theme.tokens["text-outline"]))
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            painter.drawText(x + dx, y + dy, text)
        painter.setPen(QColor(self.theme.tokens[token]))
        painter.drawText(x, y, text)


class SchematicTile(QFrame):
    """Fourth grid tile: schematic, metrics, expected red and reasons."""

    def __init__(self, *, theme: Theme, approaches: tuple[str, ...]) -> None:
        super().__init__()
        self.setObjectName("tile")
        self.theme = theme
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 4, 8, 4)
        title = QLabel("T-JUNCTION   schematic")
        title.setObjectName("tileName")
        self.advisory = QLabel("ADVISORY · no recommendation")
        self.advisory.setObjectName("helper")
        self.advisory.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        header_layout.addWidget(self.advisory)
        layout.addWidget(header)

        self.canvas = SchematicCanvas(theme)
        layout.addWidget(self.canvas, 3)

        self.red_label = QLabel("Expected red (derived): —")
        self.red_label.setObjectName("helper")
        self.red_label.setContentsMargins(8, 0, 8, 4)
        self.red_label.setWordWrap(True)
        layout.addWidget(self.red_label)

        cards = QWidget()
        cards_layout = QGridLayout(cards)
        cards_layout.setContentsMargins(6, 2, 6, 2)
        cards_layout.setSpacing(4)
        self.cards: dict[str, ApproachCard] = {}
        for index, approach in enumerate(approaches):
            card = ApproachCard(approach, theme)
            self.cards[approach] = card
            cards_layout.addWidget(card, 0, index)
        layout.addWidget(cards, 1)

        self.reasons = QPlainTextEdit()
        self.reasons.setReadOnly(True)
        self.reasons.setMaximumHeight(56)
        self.reasons.setMinimumHeight(36)
        self.reasons.setPlaceholderText("Decision reasons appear here.")
        layout.addWidget(self.reasons, 0)

    def set_snapshot(self, snapshot: UiSnapshot) -> None:
        self.canvas.set_snapshot(snapshot)
        for card in self.cards.values():
            card.update_metrics(snapshot)

        if snapshot.recommendation is not None:
            recommendation = snapshot.recommendation
            self.advisory.setText(
                f"ADVISORY · {recommendation.unit_id} {recommendation.green_time}s"
            )
        else:
            self.advisory.setText("ADVISORY · no recommendation")

        reds = sorted(snapshot.expected_red_s.items())
        self.red_label.setText(
            "Expected red (derived): "
            + ("  ·  ".join(f"{unit} ~{value:.0f}s" for unit, value in reds) or "—")
        )

        reasons = (
            snapshot.recommendation.reasons
            if snapshot.recommendation is not None
            else snapshot.decision_reasons
        )
        text = "\n".join(f"• {reason}" for reason in reasons)
        if not self.reasons.hasFocus() and self.reasons.toPlainText() != text:
            self.reasons.setPlainText(text)

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.canvas.theme = theme
        for card in self.cards.values():
            card.set_theme(theme)
        self.canvas.update()
        self.update()
