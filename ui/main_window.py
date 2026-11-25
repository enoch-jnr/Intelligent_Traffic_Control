"""The operator console shell.

The window only reads `AppController.snapshot()` and calls controller commands.
It never imports the core packages directly.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QSettings, QThreadPool, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QColor,
    QDesktopServices,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.controller import AppController
from app.exporters import config_yaml
from app.snapshots import UiSnapshot
from ui.dialogs import about_text, confirm
from ui.export_dialog import ExportLogDialog
from ui.logs import LogPanel
from ui.schematic import SchematicTile
from ui.shortcuts import ShortcutRegistry
from ui.tasks import FunctionTask
from ui.theme import DARK, THEMES, stylesheet
from ui.tiles import CameraTile
from ui.widgets import (
    CollapsibleSection,
    StatusChip,
    format_uptime,
)
from ui.wire import WireTrafficDialog

APPROACH_ORDER = ("MAIN_A", "MAIN_B", "SEC")
DEFAULT_DOCK_HEIGHT = 312
MINIMUM_DOCK_HEIGHT = 80


class MainWindow(QMainWindow):
    def __init__(
        self,
        controller: AppController,
        *,
        settings: QSettings | None = None,
        theme_name: str | None = None,
    ) -> None:
        super().__init__()
        self.controller = controller
        self.settings = settings or QSettings("Smarti", "AIHost")
        stored_theme = theme_name or str(self.settings.value("theme", "dark"))
        self.theme = THEMES.get(stored_theme, DARK)
        self._snapshot: UiSnapshot | None = None
        self._log_seq = 0
        self._event_seq = 0
        self._events_by_seq: dict[int, object] = {}
        ordered = sorted(
            controller.config.cameras,
            key=lambda camera: (
                APPROACH_ORDER.index(camera.approach)
                if camera.approach in APPROACH_ORDER
                else len(APPROACH_ORDER)
            ),
        )
        self._camera_order = [camera.id for camera in ordered]
        self._default_focus = (
            next(
                (camera.id for camera in ordered if camera.approach == "MAIN_A"),
                self._camera_order[0] if self._camera_order else None,
            )
        )
        self._focused_camera: str | None = self._default_focus
        self._focus_mode = False
        self._shortcuts = ShortcutRegistry(self)
        self._dock_height = DEFAULT_DOCK_HEIGHT
        self._playback_paused = False
        self._snapshot_warned = False
        self._shutting_down = False
        self._tasks: set[FunctionTask] = set()
        self._tiles: dict[str, CameraTile] = {}
        self._wire_dialog: WireTrafficDialog | None = None
        self._flip_state = {
            camera.id: camera.flip for camera in controller.config.cameras
        }
        self._preview_enabled = True
        self._preview_fps = 5.0
        self._preview_scale = 0.5
        self._last_video_at = 0.0
        self._last_panel_at = 0.0
        self._last_status_at = 0.0
        self._frame_versions: dict[str, int] = {}
        self._frames_painted = 0
        self._paint_times: deque[float] = deque(maxlen=400)
        self._dock_maximized = False
        self._dock_saved_sizes: list[int] = []

        self.setWindowTitle("Smarti AI Host — Traffic Control Console")
        self.setMinimumSize(1280, 720)
        self.resize(1440, 900)

        self._build_actions()
        self._build_ui()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._apply_theme()
        self._restore_layout()

        # One scheduler for video, panels and status chips. Individual widgets
        # never own timers, so hidden/minimised windows do no work.
        self._scheduler_timer = QTimer(self)
        self._scheduler_timer.setInterval(50)
        self._scheduler_timer.timeout.connect(self._scheduler_tick)
        self._scheduler_timer.start()

        self._refresh_status()

    # -- construction ------------------------------------------------------

    def _build_actions(self) -> None:
        self.exit_action = QAction("Exit", self)
        self.exit_action.triggered.connect(self.close)

        self.shutdown_action = QAction("Shutdown", self)
        self.shutdown_action.triggered.connect(self._shutdown)
        self._shortcuts.register(
            self.shutdown_action,
            "Ctrl+Q",
            "Stop requests and heartbeats, then close",
        )

        self.hold_action = QAction("Hold requests", self)
        self.hold_action.triggered.connect(self._toggle_hold)
        self._shortcuts.register(
            self.hold_action, "Ctrl+H", "Hold or resume advisory requests"
        )

        self.reset_action = QAction("Reset analysis", self)
        self.reset_action.triggered.connect(self._reset)
        self._shortcuts.register(
            self.reset_action, "Ctrl+R", "Reset analysis state and gateway session"
        )

        self.release_action = QAction("Release to autonomous", self)
        self.release_action.triggered.connect(self._toggle_release)
        self._shortcuts.register(
            self.release_action,
            "Ctrl+L",
            "Release control to the autonomous controller",
        )

        self.export_log_action = QAction("Export log…", self)
        self.export_log_action.triggered.connect(self._export_log)
        self._shortcuts.register(
            self.export_log_action, "Ctrl+E", "Export the decision log"
        )

        self.export_diagnostics_action = QAction("Export diagnostics…", self)
        self.export_diagnostics_action.triggered.connect(self._export_diagnostics)
        self._shortcuts.register(
            self.export_diagnostics_action,
            "Ctrl+Shift+E",
            "Export redacted diagnostics",
        )

        self.dashboard_snapshot_action = QAction("Save dashboard snapshot…", self)
        self.dashboard_snapshot_action.triggered.connect(self._snapshot_dashboard)
        self._shortcuts.register(
            self.dashboard_snapshot_action,
            "Ctrl+Shift+S",
            "Save a dashboard image after the privacy warning",
        )

        self.start_action = QAction("Start / Stop", self)
        self.start_action.triggered.connect(self._toggle_started)
        self._shortcuts.register(
            self.start_action, "F5", "Start or stop the analysis and advisory link"
        )

        self.focus_action = QAction("Focus mode", self)
        self.focus_action.setCheckable(True)
        self.focus_action.toggled.connect(self._set_focus_mode)
        self._shortcuts.register(
            self.focus_action, "F11", "Toggle the large focused camera"
        )

        self.focus_next_action = QAction("Focus next camera", self)
        self.focus_next_action.triggered.connect(lambda: self._cycle_focus(1))
        self._shortcuts.register(
            self.focus_next_action, "F6", "Focus the next camera"
        )

        self.focus_previous_action = QAction("Focus previous camera", self)
        self.focus_previous_action.triggered.connect(lambda: self._cycle_focus(-1))
        self._shortcuts.register(
            self.focus_previous_action, "Shift+F6", "Focus the previous camera"
        )

        self.overlays_action = QAction("Show tile overlays", self)
        self.overlays_action.setCheckable(True)
        self.overlays_action.setChecked(True)
        self.overlays_action.toggled.connect(self._toggle_overlays)

        self.logs_action = QAction("Show log panel", self)
        self.logs_action.setCheckable(True)
        self.logs_action.setChecked(True)
        self.logs_action.toggled.connect(self._set_logs_visible)
        self._shortcuts.register(
            self.logs_action, "Ctrl+J", "Hide or restore the log panel"
        )

        self.wire_action = QAction("Wire traffic…", self)
        self.wire_action.triggered.connect(self._show_wire_window)
        self._shortcuts.register(
            self.wire_action,
            "Ctrl+Shift+W",
            "Show exact gateway wire traffic",
        )

        self.preview_action = QAction("Video preview", self)
        self.preview_action.setCheckable(True)
        self.preview_action.setChecked(True)
        self.preview_action.toggled.connect(self.set_video_preview)

        self.preview_quality_group = QActionGroup(self)
        self.preview_quality_actions: dict[str, QAction] = {}
        for key, label in (("low", "Low (4 fps)"), ("medium", "Medium (8 fps)"), ("high", "High (12 fps)")):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setData(key)
            action.triggered.connect(
                lambda _checked=False, quality=key: self.set_preview_quality(quality)
            )
            self.preview_quality_group.addAction(action)
            self.preview_quality_actions[key] = action

        self.theme_action = QAction("Light theme", self)
        self.theme_action.setCheckable(True)
        self.theme_action.setChecked(self.theme.name == "light")
        self.theme_action.toggled.connect(self._theme_toggled)
        self._shortcuts.register(
            self.theme_action, "Ctrl+T", "Switch between dark and light themes"
        )

        self.reduced_motion_action = QAction("Reduce motion", self)
        self.reduced_motion_action.setCheckable(True)
        self.reduced_motion_action.toggled.connect(self._set_reduced_motion)

        self.open_logs_action = QAction("Open logs folder", self)
        self.open_logs_action.triggered.connect(self._open_logs_folder)

        self.live_log_action = QAction("Live log", self)
        self.live_log_action.triggered.connect(self._show_log_tab)

        self.open_config_action = QAction("View configuration…", self)
        self.open_config_action.triggered.connect(self._open_config)
        self._shortcuts.register(
            self.open_config_action,
            "Ctrl+,",
            "Open the redacted, read-only configuration",
        )

        self.shortcuts_help_action = QAction("Keyboard shortcuts", self)
        self.shortcuts_help_action.triggered.connect(self._show_shortcuts)

        self.about_action = QAction("About and limits", self)
        self.about_action.triggered.connect(self._show_about)

        self.camera_actions: dict[str, QAction] = {}
        ordered = [
            camera
            for camera_id in self._camera_order
            for camera in self.controller.config.cameras
            if camera.id == camera_id
        ]
        for index, camera in enumerate(ordered, start=1):
            action = QAction(f"Focus {camera.approach} ({camera.id})", self)
            action.triggered.connect(
                lambda _checked=False, camera_id=camera.id: self._focus_camera(
                    camera_id
                )
            )
            if index <= 3:
                self._shortcuts.register(
                    action,
                    f"Ctrl+{index}",
                    f"Focus {camera.approach}",
                )
            self.camera_actions[camera.id] = action

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._build_menus()

        self.work_splitter = QSplitter(Qt.Orientation.Vertical)
        self.work_splitter.setChildrenCollapsible(True)
        self.work_splitter.addWidget(self._build_video_area())
        self.log_dock = self._build_log_dock()
        self.work_splitter.addWidget(self.log_dock)
        self.work_splitter.setCollapsible(1, True)
        self.work_splitter.setStretchFactor(0, 3)
        self.work_splitter.setStretchFactor(1, 2)
        self.work_splitter.setSizes([348, DEFAULT_DOCK_HEIGHT])
        self.work_splitter.splitterMoved.connect(self._splitter_moved)
        self.work_splitter.handle(1).installEventFilter(self)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.work_splitter)
        self.splitter.addWidget(self._build_sidebar())
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([1050, 300])
        outer.addWidget(self.splitter, 1)

        self.setCentralWidget(central)
        self._build_status_bar()

    def _build_menus(self) -> None:
        menu = self.menuBar()

        file_menu = menu.addMenu("File")
        file_menu.addAction(self.start_action)
        file_menu.addAction(self.hold_action)
        file_menu.addAction(self.reset_action)
        file_menu.addAction(self.release_action)
        file_menu.addSeparator()
        file_menu.addAction(self.export_log_action)
        file_menu.addAction(self.export_diagnostics_action)
        file_menu.addAction(self.dashboard_snapshot_action)
        file_menu.addSeparator()
        file_menu.addAction(self.shutdown_action)
        file_menu.addAction(self.exit_action)

        view_menu = menu.addMenu("View")
        view_menu.addAction(self.focus_action)
        view_menu.addAction(self.overlays_action)
        view_menu.addAction(self.preview_action)
        quality_menu = view_menu.addMenu("Preview quality")
        for action in self.preview_quality_actions.values():
            quality_menu.addAction(action)
        view_menu.addAction(self.theme_action)
        view_menu.addSeparator()
        view_menu.addAction(self.logs_action)
        view_menu.addAction(self.wire_action)

        cameras_menu = menu.addMenu("Cameras")
        cameras_menu.addAction(self.focus_next_action)
        cameras_menu.addAction(self.focus_previous_action)
        cameras_menu.addSeparator()
        for action in self.camera_actions.values():
            cameras_menu.addAction(action)

        log_menu = menu.addMenu("Log")
        log_menu.addAction(self.logs_action)
        log_menu.addAction(self.live_log_action)
        log_menu.addAction(self.open_logs_action)

        settings_menu = menu.addMenu("Settings")
        settings_menu.addAction(self.open_config_action)
        settings_menu.addAction(self.theme_action)
        settings_menu.addAction(self.reduced_motion_action)

        help_menu = menu.addMenu("Help")
        help_menu.addAction(self.shortcuts_help_action)
        help_menu.addAction(self.about_action)

        self.mode_badge = QLabel("LIVE:COMPAT-2PH")
        self.mode_badge.setObjectName("modeBadge")
        self.mode_badge.setAccessibleName("Operating mode")
        self.mode_badge.setToolTip(
            "Shows whether this host is live or simulated. Simulation is never hidden."
        )
        menu.setCornerWidget(self.mode_badge, Qt.Corner.TopRightCorner)

    def _build_video_area(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.grid_host = QWidget()
        self.grid_layout = QGridLayout(self.grid_host)
        self.grid_layout.setContentsMargins(2, 2, 2, 2)
        self.grid_layout.setSpacing(2)
        for column in range(2):
            self.grid_layout.setColumnStretch(column, 1)
        for row in range(2):
            self.grid_layout.setRowStretch(row, 1)

        self._build_tiles()
        self.grid_host.setMinimumHeight(0)
        layout.addWidget(self.grid_host, 1)
        return holder

    def _build_log_dock(self) -> QWidget:
        dock = QFrame()
        dock.setObjectName("card")
        dock.setMinimumHeight(MINIMUM_DOCK_HEIGHT)
        dock.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored
        )
        dock_layout = QVBoxLayout(dock)
        dock_layout.setContentsMargins(2, 0, 2, 2)
        dock_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.log_panel = LogPanel(self.theme)
        dock_layout.addWidget(self.log_panel)
        self.log_panel.tabBar().tabBarDoubleClicked.connect(
            self._toggle_dock_maximized
        )
        return dock

    def _build_tiles(self) -> None:
        ordered = [
            camera
            for camera_id in self._camera_order
            for camera in self.controller.config.cameras
            if camera.id == camera_id
        ]
        self.schematic_tile = SchematicTile(
            theme=self.theme,
            approaches=tuple(camera.approach for camera in ordered),
        )
        self.schematic_tile.setAccessibleName(
            "T-junction recommendation, advisory only"
        )
        for camera in ordered:
            tile = CameraTile(
                camera.id,
                camera.approach,
                camera.source,
                theme=self.theme,
            )
            tile.setAccessibleName(f"{camera.approach} camera {camera.id}")
            tile.clicked.connect(self._tile_clicked)
            tile.snapshot_requested.connect(self._snapshot_camera)
            tile.flip_requested.connect(self._toggle_flip)
            tile.overlays_requested.connect(self._set_tile_overlays)
            self._tiles[camera.id] = tile
        self._relayout_tiles()

    def _relayout_tiles(self) -> None:
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(self.grid_host)
        self.schematic_tile.setVisible(not self._focus_mode)
        if self._focus_mode and self._focused_camera in self._tiles:
            big = self._tiles[self._focused_camera]
            self.grid_layout.addWidget(big, 0, 0, 2, 2)
            others = [
                tile
                for camera_id, tile in self._tiles.items()
                if camera_id != self._focused_camera
            ]
            for index, tile in enumerate(others[:2]):
                self.grid_layout.addWidget(tile, index, 2)
        else:
            items: list[QWidget] = [
                self._tiles[camera_id]
                for camera_id in self._camera_order
                if camera_id in self._tiles
            ] + [self.schematic_tile]
            for index, tile in enumerate(items[:4]):
                self.grid_layout.addWidget(tile, index // 2, index % 2)

    def _build_sidebar(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(280)
        scroll.setMaximumWidth(430)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar = QWidget()
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        system = QLabel("Waiting for the first tick.")
        system.setObjectName("mono")
        system.setWordWrap(True)
        system.setContentsMargins(10, 8, 10, 8)
        system.setMinimumWidth(0)
        system.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum
        )
        self.system_label = system
        layout.addWidget(
            CollapsibleSection(
                "System", system, settings=self.settings, key="system"
            )
        )

        self.events_table = QTableWidget(0, 4)
        self.events_table.setHorizontalHeaderLabels(
            ["Time", "Unit / camera", "Event", "Result"]
        )
        self.events_table.verticalHeader().setVisible(False)
        self.events_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.events_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.events_table.setMinimumHeight(140)
        self.events_table.cellClicked.connect(self._event_clicked)
        events_box = QWidget()
        events_layout = QVBoxLayout(events_box)
        events_layout.setContentsMargins(0, 0, 0, 4)
        events_layout.setSpacing(2)
        events_layout.addWidget(self.events_table)
        view_log = QPushButton("View full log")
        view_log.clicked.connect(self._show_log_tab)
        events_layout.addWidget(view_log)
        layout.addWidget(
            CollapsibleSection(
                "Live Events", events_box, settings=self.settings, key="events"
            )
        )

        playback = QWidget()
        playback_layout = QVBoxLayout(playback)
        playback_layout.setContentsMargins(10, 8, 10, 8)
        playback_layout.setSpacing(6)
        note = QLabel("Testing aid: file sources only. Speed changes the feed, not decision timing.")
        note.setObjectName("helper")
        note.setWordWrap(True)
        playback_layout.addWidget(note)
        row = QHBoxLayout()
        self.play_button = QPushButton("Pause")
        self.play_button.clicked.connect(self._toggle_playback)
        restart = QPushButton("Restart")
        restart.clicked.connect(self.controller.restart_playback)
        self.speed_box = QComboBox()
        for label, value in (("0.5x", 0.5), ("1x", 1.0), ("2x", 2.0)):
            self.speed_box.addItem(label, value)
        self.speed_box.setCurrentIndex(1)
        self.speed_box.currentIndexChanged.connect(
            lambda _index: self.controller.set_playback_speed(
                float(self.speed_box.currentData())
            )
        )
        row.addWidget(self.play_button)
        row.addWidget(restart)
        row.addWidget(self.speed_box)
        playback_layout.addLayout(row)
        self.playback_section = CollapsibleSection(
            "Playback", playback, settings=self.settings, key="playback"
        )
        self.playback_section.setVisible(bool(self.controller.file_camera_ids()))
        layout.addWidget(self.playback_section)

        self.camera_tree = QTreeWidget()
        self.camera_tree.setHeaderLabels(["Approach / camera", "Source"])
        self.camera_tree.setMinimumHeight(150)
        self.camera_tree.setAccessibleName("Camera list")
        self.camera_items: dict[str, QTreeWidgetItem] = {}
        root = QTreeWidgetItem(["Junction (T)", ""])
        self.camera_tree.addTopLevelItem(root)
        for camera_id in self._camera_order:
            camera = next(
                item for item in self.controller.config.cameras if item.id == camera_id
            )
            group = QTreeWidgetItem([camera.approach, ""])
            group.setForeground(
                0,
                QBrush(
                    QColor(
                        self.theme.tokens[
                            {
                                "MAIN_A": "approach-main-a",
                                "MAIN_B": "approach-main-b-label",
                                "SEC": "approach-sec-label",
                            }.get(camera.approach, "text-secondary")
                        ]
                    )
                ),
            )
            root.addChild(group)
            item = QTreeWidgetItem([camera.id, camera.source])
            item.setData(0, Qt.ItemDataRole.UserRole, camera.id)
            group.addChild(item)
            self.camera_items[camera.id] = item
            group.setExpanded(True)
        root.setExpanded(True)
        self.camera_tree.itemClicked.connect(self._tree_item_clicked)
        layout.addWidget(
            CollapsibleSection(
                "Camera List", self.camera_tree, settings=self.settings, key="cameras"
            )
        )

        self.controls_section = CollapsibleSection(
            "Controls",
            self._build_controls(),
            settings=self.settings,
            key="controls",
            expanded=True,
        )
        layout.addWidget(self.controls_section)
        layout.addStretch(1)

        scroll.setWidget(sidebar)
        return scroll

    def _build_controls(self) -> QWidget:
        box = QWidget()
        grid = QGridLayout(box)
        grid.setContentsMargins(8, 6, 8, 8)
        grid.setSpacing(6)

        def button(text: str, slot, variant: str, tooltip: str) -> QPushButton:
            btn = QPushButton(text)
            if variant:
                btn.setProperty("variant", variant)
            btn.setToolTip(tooltip)
            btn.clicked.connect(slot)
            return btn

        self.start_stop_button = button(
            "Start / Stop",
            self._toggle_started,
            "primary",
            "Start or stop the analysis and advisory link (F5)",
        )
        self.hold_button = button(
            "Hold / Resume",
            self._toggle_hold,
            "",
            "Keep analysing and heartbeating without sending requests (Ctrl+H)",
        )
        self.reset_button = button(
            "Reset analysis",
            self._reset,
            "destructive",
            "Clear smoothing, counters and tracks, then restart the gateway session (Ctrl+R)",
        )
        self.release_button = button(
            "Release / Resume",
            self._toggle_release,
            "caution",
            "Close the heartbeat gate so the controller runs autonomously (Ctrl+L)",
        )
        export_button = button(
            "Export log…",
            self._export_log,
            "",
            "Export JSONL, CSV or filtered log text (Ctrl+E)",
        )
        diagnostics_button = button(
            "Diagnostics…",
            self._export_diagnostics,
            "",
            "Zip redacted config, versions, logs and decisions; no images (Ctrl+Shift+E)",
        )
        self.wire_button = button(
            "Wire traffic…",
            self._show_wire_window,
            "",
            "Show the exact JSON sent to the gateway and every reply received (Ctrl+Shift+W)",
        )
        snapshot_button = button(
            "Dashboard image…",
            self._snapshot_dashboard,
            "",
            "Save the dashboard image after one privacy warning (Ctrl+Shift+S)",
        )
        shutdown_button = button(
            "Shutdown",
            self._shutdown,
            "destructive",
            "Stop requests and heartbeats, flush logs and close (Ctrl+Q)",
        )
        logs_button = button(
            "Open logs", self._open_logs_folder, "", "Open the decision-log folder"
        )
        config_button = button(
            "View config", self._open_config, "", "Read-only, secrets redacted"
        )
        about_button = button("About", self._show_about, "", "Scope and safety limits")

        buttons = [
            self.start_stop_button,
            self.hold_button,
            self.reset_button,
            self.release_button,
            export_button,
            diagnostics_button,
            self.wire_button,
            snapshot_button,
            shutdown_button,
            logs_button,
            config_button,
            about_button,
        ]
        for index, widget in enumerate(buttons):
            grid.addWidget(widget, index // 2, index % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        return box

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        bar.setSizeGripEnabled(False)
        self.start_button = QPushButton("START")
        self.start_button.setProperty("variant", "primary")
        self.start_button.setMinimumWidth(112)
        self.start_button.clicked.connect(self._toggle_started)
        bar.addWidget(self.start_button)

        self.session_label = QLabel("Session 00:00:00")
        self.session_label.setObjectName("session")
        bar.addWidget(self.session_label, 1)

        self.camera_chip = StatusChip("0/0 cameras", "unknown")
        self.gateway_chip = StatusChip("Gateway unknown", "unknown")
        self.gate_chip = StatusChip("Gate CLOSED", "critical")
        self.clock_chip = StatusChip("--:--:--", "info")
        self.focus_label = QLabel("Focus: —")
        self.focus_label.setObjectName("mono")
        self.focus_label.setAccessibleName("Focused camera")
        bar.addPermanentWidget(self.focus_label)
        for chip in (
            self.camera_chip,
            self.gateway_chip,
            self.gate_chip,
            self.clock_chip,
        ):
            bar.addPermanentWidget(chip)
        self.setStatusBar(bar)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if hasattr(self, "work_splitter"):
            self._apply_dock_height(self._dock_height)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if (
            hasattr(self, "log_dock")
            and not self._dock_maximized
            and self.log_dock.isVisible()
        ):
            self._apply_dock_height(self._dock_height)

    # -- theme and layout --------------------------------------------------

    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(stylesheet(self.theme))
        if hasattr(self, "mode_badge"):
            self.mode_badge.setStyleSheet("")

    def switch_theme(self, name: str) -> None:
        theme = THEMES.get(name)
        if theme is None:
            return
        self.theme = theme
        self.settings.setValue("theme", name)
        self.theme_action.blockSignals(True)
        self.theme_action.setChecked(name == "light")
        self.theme_action.blockSignals(False)
        self._apply_theme()
        for tile in self._tiles.values():
            tile.theme = self.theme
            tile.update()
        self.schematic_tile.set_theme(self.theme)
        self.log_panel.set_theme(self.theme)
        self._refresh_status()

    def _theme_toggled(self, light: bool) -> None:
        self.switch_theme("light" if light else "dark")

    def _set_reduced_motion(self, enabled: bool) -> None:
        self._reduced_motion = bool(enabled)
        self.settings.setValue("view/reduced_motion", self._reduced_motion)
        # The console has no decorative animation; this preference records the
        # operator choice and is honoured by any future animation.

    def _set_focus_mode(self, enabled: bool) -> None:
        self._focus_mode = bool(enabled)
        self.settings.setValue("view/focus", self._focus_mode)
        if self._focus_mode and self._focused_camera not in self._tiles:
            self._focused_camera = self._default_focus
        self._relayout_tiles()
        self._update_tile_focus()

    def _tile_clicked(self, camera_id: str) -> None:
        self._focus_camera(camera_id)
        if not self._focus_mode:
            self.focus_action.setChecked(True)

    def _focus_camera(self, camera_id: str) -> None:
        if camera_id not in self._tiles:
            return
        self._focused_camera = camera_id
        self.settings.setValue("view/focused_camera", camera_id)
        if self._focus_mode:
            self._relayout_tiles()
        self._update_tile_focus()
        self._refresh_focus_label()

    def _cycle_focus(self, step: int) -> None:
        if not self._camera_order:
            return
        current = self._focused_camera or self._camera_order[0]
        try:
            index = self._camera_order.index(current)
        except ValueError:
            index = 0
        self._focus_camera(self._camera_order[(index + step) % len(self._camera_order)])

    def _update_tile_focus(self) -> None:
        for camera_id, tile in self._tiles.items():
            tile.set_focused(camera_id == self._focused_camera)

    def _refresh_focus_label(self) -> None:
        camera_id = self._focused_camera
        approach = ""
        if camera_id:
            for camera in self.controller.config.cameras:
                if camera.id == camera_id:
                    approach = camera.approach
                    break
        self.focus_label.setText(
            f"Focus: {approach} {camera_id}" if camera_id else "Focus: —"
        )

    def _tree_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        camera_id = item.data(0, Qt.ItemDataRole.UserRole)
        if camera_id:
            self._focus_camera(str(camera_id))

    def _toggle_overlays(self, enabled: bool) -> None:
        self.controller.set_overlays(polygon=enabled, count_line=enabled)
        for tile in self._tiles.values():
            tile.set_overlays_visible(enabled)
            tile.set_overlay_state(enabled, enabled)
        self.settings.setValue("view/overlays", enabled)

    def _set_tile_overlays(
        self, camera_id: str, polygon: bool, count_line: bool
    ) -> None:
        self.controller.set_camera_overlays(
            camera_id, polygon=polygon, count_line=count_line
        )
        tile = self._tiles.get(camera_id)
        if tile is not None:
            tile.set_overlay_state(polygon, count_line)

    def _set_logs_visible(self, visible: bool) -> None:
        visible = bool(visible)
        if visible:
            self.log_dock.setVisible(True)
            self._apply_dock_height(self._dock_height)
        else:
            if self.log_dock.isVisible():
                sizes = self.work_splitter.sizes()
                if len(sizes) > 1 and sizes[1] >= MINIMUM_DOCK_HEIGHT:
                    self._dock_height = sizes[1]
            self.log_dock.setVisible(False)
        self.settings.setValue("view/logs", visible)
        self.settings.setValue("window/dock_height", self._dock_height)

    def _apply_dock_height(self, height: int) -> None:
        total = self.work_splitter.height() - self.work_splitter.handleWidth()
        if total <= 0:
            total = sum(self.work_splitter.sizes()) or 720
        if total <= MINIMUM_DOCK_HEIGHT:
            return
        minimum_video = min(160, total // 2)
        height = max(MINIMUM_DOCK_HEIGHT, min(int(height), total - minimum_video))
        self._dock_height = height
        self.work_splitter.setSizes([max(1, total - height), height])

    def _splitter_moved(self, _position: int, _index: int) -> None:
        if not self.log_dock.isVisible():
            return
        sizes = self.work_splitter.sizes()
        if len(sizes) < 2:
            return
        if sizes[1] < MINIMUM_DOCK_HEIGHT:
            self.logs_action.blockSignals(True)
            self.logs_action.setChecked(False)
            self.logs_action.blockSignals(False)
            self.log_dock.setVisible(False)
            self.settings.setValue("view/logs", False)
            return
        self._dock_height = sizes[1]

    def _toggle_logs(self) -> None:
        self.logs_action.setChecked(not self.logs_action.isChecked())

    def _show_log_tab(self) -> None:
        self.logs_action.setChecked(True)
        self._set_logs_visible(True)
        self.log_panel.setCurrentWidget(self.log_panel.live)

    def _restore_layout(self) -> None:
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        splitter = self.settings.value("window/splitter")
        if splitter:
            self.splitter.restoreState(splitter)
        self._dock_height = DEFAULT_DOCK_HEIGHT
        try:
            stored_height = int(self.settings.value("window/dock_height", 0))
        except (TypeError, ValueError):
            stored_height = 0
        if MINIMUM_DOCK_HEIGHT <= stored_height <= 1200:
            self._dock_height = stored_height
        self.work_splitter.setSizes(
            [348, min(DEFAULT_DOCK_HEIGHT, max(MINIMUM_DOCK_HEIGHT, self._dock_height))]
        )
        overlays = self.settings.value("view/overlays", True)
        enabled = overlays in (True, "true", "True", "1", 1)
        self.overlays_action.blockSignals(True)
        self.overlays_action.setChecked(enabled)
        self.overlays_action.blockSignals(False)
        self.controller.set_overlays(polygon=enabled, count_line=enabled)
        for tile in self._tiles.values():
            tile.set_overlays_visible(enabled)
            tile.set_overlay_state(enabled, enabled)
        focused = self.settings.value("view/focus", False) in (
            True,
            "true",
            "True",
            "1",
            1,
        )
        if focused:
            self.focus_action.setChecked(True)
        stored_focus = str(
            self.settings.value("view/focused_camera", self._default_focus or "")
        )
        if stored_focus in self._tiles:
            self._focused_camera = stored_focus
        else:
            # Old or corrupted camera ids are ignored, never a crash.
            self._focused_camera = self._default_focus
        self._update_tile_focus()
        self._refresh_focus_label()
        logs = self.settings.value("view/logs", True) in (
            True,
            "true",
            "True",
            "1",
            1,
        )
        self.logs_action.blockSignals(True)
        self.logs_action.setChecked(logs)
        self.logs_action.blockSignals(False)
        self.log_dock.setVisible(logs)
        QTimer.singleShot(0, lambda: self._apply_dock_height(self._dock_height))
        reduced = self.settings.value("view/reduced_motion", False) in (
            True,
            "true",
            "True",
            "1",
            1,
        )
        self.reduced_motion_action.blockSignals(True)
        self.reduced_motion_action.setChecked(reduced)
        self.reduced_motion_action.blockSignals(False)
        self._reduced_motion = reduced

        preview = self.settings.value("view/preview", True) in (
            True,
            "true",
            "True",
            "1",
            1,
        )
        default_quality = {
            "low": "low",
            "balanced": "medium",
            "high": "high",
        }.get(self.controller.config.performance.profile, "low")
        quality = str(
            self.settings.value("view/preview_quality", default_quality)
        ).lower()
        if quality not in ("low", "medium", "high"):
            quality = default_quality
        self.preview_action.blockSignals(True)
        self.preview_action.setChecked(preview)
        self.preview_action.blockSignals(False)
        self.set_preview_quality(quality, persist=False)
        self.set_video_preview(preview, persist=False)

    def _save_layout(self) -> None:
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/splitter", self.splitter.saveState())
        if not self._dock_maximized and self.log_dock.isVisible():
            sizes = self.work_splitter.sizes()
            if len(sizes) > 1 and sizes[1] >= MINIMUM_DOCK_HEIGHT:
                self._dock_height = sizes[1]
        self.settings.setValue("window/dock_height", self._dock_height)
        self.settings.setValue("theme", self.theme.name)
        self.settings.setValue("view/focus", self._focus_mode)
        self.settings.setValue("view/focused_camera", self._focused_camera)
        self.settings.setValue("view/overlays", self.overlays_action.isChecked())
        self.settings.setValue("view/logs", self.logs_action.isChecked())
        self.settings.setValue("view/preview", self._preview_enabled)
        self.settings.setValue("view/preview_quality", self._preview_quality)
        self.settings.setValue(
            "view/reduced_motion", self.reduced_motion_action.isChecked()
        )

    def set_video_preview(self, enabled: bool, *, persist: bool = True) -> None:
        self._preview_enabled = bool(enabled)
        if persist:
            self.settings.setValue("view/preview", self._preview_enabled)
        for tile in self._tiles.values():
            tile.set_preview_enabled(self._preview_enabled)
        if self._preview_enabled:
            self._last_video_at = 0.0

    def set_preview_quality(self, quality: str, *, persist: bool = True) -> None:
        settings = {
            "low": (4.0, 0.5),
            "medium": (8.0, 0.75),
            "high": (12.0, 1.0),
        }
        key = quality if quality in settings else "low"
        self._preview_quality = key
        self._preview_fps, self._preview_scale = settings[key]
        action = self.preview_quality_actions.get(key)
        if action is not None:
            action.setChecked(True)
        if persist:
            self.settings.setValue("view/preview_quality", key)

    def _toggle_dock_maximized(self, _index: int = -1) -> None:
        if self._dock_maximized:
            self.grid_host.setVisible(True)
            if self._dock_saved_sizes:
                self.work_splitter.setSizes(self._dock_saved_sizes)
        else:
            self._dock_saved_sizes = self.work_splitter.sizes()
            self.grid_host.setVisible(False)
        self._dock_maximized = not self._dock_maximized

    # -- live updates ------------------------------------------------------

    def _scheduler_tick(self) -> None:
        if self.isMinimized() or not self.isVisible():
            return
        now = time.monotonic()
        if self._preview_enabled and now - self._last_video_at >= 1.0 / self._preview_fps:
            self._last_video_at = now
            self._refresh_video()
        if now - self._last_panel_at >= 0.25:
            self._last_panel_at = now
            self._refresh_panels()
        if now - self._last_status_at >= 1.0:
            self._last_status_at = now
            self._refresh_status()

    def _refresh_video(self) -> None:
        snapshot = self._snapshot
        if not self._preview_enabled or snapshot is None or not snapshot.started:
            return
        for camera in snapshot.cameras:
            tile = self._tiles.get(camera.camera_id)
            if tile is None or not tile.isVisible():
                continue
            max_size = max(
                160, int(tile.video_max_size() * self._preview_scale)
            )
            version = self._frame_versions.get(camera.camera_id, 0)
            frame = self.controller.frame(
                camera.camera_id, max_size=max_size, if_version=version
            )
            if frame is None:
                continue
            new_version = self.controller.frame_version(camera.camera_id)
            if tile.set_frame(frame, frame_index=new_version):
                self._frame_versions[camera.camera_id] = new_version
                self._frames_painted += 1
                self._paint_times.append(time.monotonic())

    def _ui_render_fps(self) -> float:
        now = time.monotonic()
        while self._paint_times and now - self._paint_times[0] > 2.0:
            self._paint_times.popleft()
        if len(self._paint_times) < 2:
            return 0.0
        span = max(0.001, self._paint_times[-1] - self._paint_times[0])
        return round((len(self._paint_times) - 1) / span, 1)

    def _refresh_panels(self) -> None:
        lines, self._log_seq = self.controller.log_lines(self._log_seq)
        if lines:
            self.log_panel.append_logs(lines)
        if self.log_panel.isVisible():
            self.log_panel.set_decisions(self.controller.decisions())
        self._refresh_events()

    def _refresh_events(self) -> None:
        events, self._event_seq = self.controller.events(self._event_seq)
        for event in events:
            self._events_by_seq[event.seq] = event
            self.events_table.insertRow(0)
            values = [
                datetime.fromtimestamp(event.ts).strftime("%H:%M:%S"),
                event.subject,
                event.action,
                event.result,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, event.seq)
                self.events_table.setItem(0, column, item)
        while self.events_table.rowCount() > 200:
            self.events_table.removeRow(self.events_table.rowCount() - 1)
        while len(self._events_by_seq) > 500:
            self._events_by_seq.pop(min(self._events_by_seq), None)

    def _event_clicked(self, row: int, _column: int) -> None:
        item = self.events_table.item(row, 0)
        if item is None:
            return
        seq = item.data(Qt.ItemDataRole.UserRole)
        event = self._events_by_seq.get(seq)
        if event is None or event.kind != "decision":
            return
        timestamp = event.detail.get("ts")
        if not timestamp:
            return
        self._show_log_tab()
        self.log_panel.setCurrentWidget(self.log_panel.decisions)
        self.log_panel.decisions.select_ts(str(timestamp))

    def _refresh_status(self) -> None:
        snapshot = self.controller.snapshot()
        self._snapshot = snapshot

        self.start_button.setText("STOP" if snapshot.started else "START")
        self.hold_button.setText(
            "Resume requests" if snapshot.held else "Hold / Resume"
        )
        self.release_button.setText(
            "Resume control" if snapshot.released else "Release / Resume"
        )
        self.hold_action.setText(
            "Resume requests" if snapshot.held else "Hold requests"
        )
        self.release_action.setText(
            "Resume control" if snapshot.released else "Release to autonomous"
        )
        self.session_label.setText(format_uptime(snapshot.uptime_s))
        self._refresh_focus_label()

        if snapshot.fresh_cameras == snapshot.required_cameras and snapshot.required_cameras:
            tone = "ok"
        elif snapshot.fresh_cameras == 0:
            tone = "critical"
        else:
            tone = "warn"
        self.camera_chip.set_state(
            f"{snapshot.fresh_cameras}/{snapshot.required_cameras} cameras", tone
        )

        badge_text, badge_tone, badge_tip = mode_badge_state(snapshot)
        if self.mode_badge.text() != badge_text:
            self.mode_badge.setText(badge_text)
        if self.mode_badge.property("tone") != badge_tone:
            self.mode_badge.setProperty("tone", badge_tone)
            self.mode_badge.style().unpolish(self.mode_badge)
            self.mode_badge.style().polish(self.mode_badge)
        self.mode_badge.setToolTip(badge_tip)

        gateway_tone = {
            "CONNECTED": "ok",
            "CONNECTING": "unknown",
            "OFFLINE": "critical",
            "DRY RUN": "info",
            "OFF": "unknown",
        }.get(snapshot.gateway_state, "unknown")
        self.gateway_chip.set_state(
            f"Gateway {snapshot.gateway_state.title()}",
            gateway_tone,
            f"{snapshot.gateway_host}:{snapshot.gateway_port}",
        )

        if snapshot.gate_open:
            gate_tone = "ok"
        elif snapshot.released:
            gate_tone = "warn"
        else:
            gate_tone = "critical"
        self.gate_chip.set_state(
            "Gate OPEN" if snapshot.gate_open else "Gate CLOSED",
            gate_tone,
            snapshot.gate_reason,
        )
        self.clock_chip.set_state(datetime.now().strftime("%H:%M:%S"), "info")

        fps = " / ".join(f"{camera.fps:.1f}" for camera in snapshot.cameras)
        processing = []
        for camera in snapshot.cameras:
            line = (
                f"{camera.approach}: {camera.processed_fps:.1f} fps "
                f"{camera.inference_ms:.0f} ms"
            )
            if camera.dropped_frames:
                line += f" · {camera.dropped_frames} dropped"
            if camera.overloaded:
                line += f" · overloaded: reduced to {camera.effective_fps:.0f} fps"
            processing.append(line)
        self.system_label.setText(
            "\n".join(
                [
                    f"Mode: {snapshot.mode}",
                    f"Profile: {snapshot.performance_profile}",
                    f"Gateway: {snapshot.gateway_host}:{snapshot.gateway_port} "
                    f"({snapshot.gateway_state})",
                    f"Heartbeat gate: {'OPEN' if snapshot.gate_open else 'CLOSED'}",
                    f"Reason: {snapshot.gate_reason}",
                    f"Device: {self.controller.config.detection.device} · "
                    f"imgsz {self.controller.config.performance.imgsz}",
                    f"Source fps: {fps or 'no data'}",
                    *processing,
                    f"App CPU: {_percent(snapshot.app_cpu_percent)}   "
                    f"RSS: {'unknown' if snapshot.app_memory_mb is None else f'{snapshot.app_memory_mb:.0f} MB'}",
                    f"UI render: {self._ui_render_fps():.1f} fps   "
                    f"System memory: {_percent(snapshot.memory_percent)}",
                    f"Uptime: {format_uptime(snapshot.uptime_s)}",
                ]
            )
        )

        state_tokens = {
            "LIVE": "status-ok",
            "STALE": "status-warn",
            "CONNECTING": "status-unknown",
            "DISCONNECTED": "status-critical-text",
        }
        icons = {
            "LIVE": "✓",
            "STALE": "!",
            "CONNECTING": "•",
            "DISCONNECTED": "✕",
        }
        for camera in snapshot.cameras:
            tile = self._tiles.get(camera.camera_id)
            if tile is not None:
                tile.set_health(
                    camera.state,
                    camera.fps,
                    camera.frame_age_s,
                    camera.reconnects,
                    snapshot.approach(camera.approach),
                    started=snapshot.started,
                )
            item = self.camera_items.get(camera.camera_id)
            if item is not None:
                item.setText(
                    0, f"{icons.get(camera.state, '•')} {camera.camera_id}"
                )
                item.setText(
                    1, f"{camera.source} · {camera.fps:.1f} fps"
                )
                item.setForeground(
                    0,
                    QBrush(
                        QColor(
                            self.theme.tokens[
                                state_tokens.get(camera.state, "status-unknown")
                            ]
                        )
                    ),
                )
        self.schematic_tile.set_snapshot(snapshot)

    # -- commands ----------------------------------------------------------

    def _toggle_started(self) -> None:
        if self.controller.running:
            if not confirm(
                self,
                "Stop the host?",
                "Stop the pipelines and the advisory link now?",
                confirm_label="Stop",
                informative=(
                    "The controller will run autonomously; heartbeats stop and "
                    "logs are flushed."
                ),
            ):
                return
            self.controller.stop()
        else:
            try:
                self.controller.start()
            except Exception as exc:  # noqa: BLE001 - shown to the operator
                QMessageBox.critical(self, "Could not start", str(exc))
        self._refresh_status()

    def _warn_snapshot_privacy(self) -> None:
        if self._snapshot_warned:
            return
        QMessageBox.information(
            self,
            "Snapshot privacy",
            "Camera frames may show people and number plates. "
            "A snapshot is saved only when you choose to, and exports "
            "never contain images.",
        )
        self._snapshot_warned = True

    def _snapshot_camera(self, camera_id: str) -> None:
        tile = self._tiles.get(camera_id)
        if tile is None:
            return
        self._warn_snapshot_privacy()
        default = f"snapshot_{tile.approach}_{datetime.now():%Y%m%d_%H%M%S}.png"
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save tile snapshot", default, "PNG image (*.png)"
        )
        if not path:
            return
        if not tile.grab().save(path):
            QMessageBox.warning(self, "Snapshot failed", f"Could not write {path}")

    def _toggle_flip(self, camera_id: str) -> None:
        new_state = not self._flip_state.get(camera_id, False)
        self._flip_state[camera_id] = new_state
        self.controller.set_flip(camera_id, new_state)

    def _toggle_hold(self) -> None:
        if self.controller.held:
            self.controller.resume_requests()
        else:
            self.controller.hold_requests()
        self._refresh_status()

    def _reset(self) -> None:
        if not confirm(
            self,
            "Reset analysis state?",
            "Clear smoothing windows, counters, track state and restart the gateway session?",
            confirm_label="Reset",
            informative=(
                "Configuration and model weights are kept. The sequence restarts "
                "at 1 on the new session."
            ),
        ):
            return
        self.controller.reset()
        self.statusBar().showMessage("Reset requested", 4000)

    def _toggle_release(self) -> None:
        if self.controller.released:
            self.controller.resume_control()
            self._refresh_status()
            return
        if not confirm(
            self,
            "Release to the controller?",
            "Close the heartbeat gate so the controller runs autonomously?",
            confirm_label="Release",
            informative=("The controller keeps its own cycle until you click Resume control."),
        ):
            return
        self.controller.release_to_autonomous()
        self._refresh_status()

    def _snapshot_dashboard(self) -> None:
        self._warn_snapshot_privacy()
        default = f"dashboard_{datetime.now():%Y%m%d_%H%M%S}.png"
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save dashboard snapshot", default, "PNG image (*.png)"
        )
        if not path:
            return
        if not self.grab().save(path):
            QMessageBox.warning(self, "Snapshot failed", f"Could not write {path}")

    def _run_task(
        self,
        function,
        on_done,
        *,
        title: str = "Working…",
        progress: bool = False,
    ) -> None:
        task = FunctionTask(function)
        self._tasks.add(task)
        dialog = None
        if progress:
            dialog = QProgressDialog(title, "", 0, 100, self)
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            dialog.setMinimumDuration(0)
            dialog.setCancelButton(None)
            dialog.setValue(0)
            dialog.show()
            task.signals.progress.connect(
                lambda done, total: dialog.setValue(
                    0 if total <= 0 else int(100 * done / total)
                )
            )

        def cleanup(*_args) -> None:
            self._tasks.discard(task)
            if dialog is not None:
                dialog.close()

        def failed(message: str) -> None:
            QMessageBox.warning(self, "Operation failed", message)

        task.signals.finished.connect(on_done)
        task.signals.finished.connect(cleanup)
        task.signals.failed.connect(failed)
        task.signals.failed.connect(cleanup)
        QThreadPool.globalInstance().start(task)

    def _export_log(self) -> None:
        selected = self.log_panel.decisions.selected_entries()
        dialog = ExportLogDialog(
            self,
            default_dir=Path(self.controller.config.logging.decision_log).resolve().parent,
            has_selection=bool(selected),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        path, fmt, range_key = dialog.values()
        if not path:
            QMessageBox.warning(self, "Export log", "Choose a destination file.")
            return
        selected_for_export = selected if range_key == "selected" else None
        text_lines = (
            self.log_panel.live.visible_lines() if fmt == "text" else None
        )

        def work(emit):
            return self.controller.export_log(
                path,
                fmt,
                range_key,
                selected=selected_for_export,
                text_lines=text_lines,
                progress=emit,
            )

        self._run_task(
            work,
            lambda result: self._export_finished(path, result),
            title="Exporting log…",
            progress=True,
        )

    def _export_finished(self, path: str, result) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Export complete")
        box.setText("Export complete")
        rows = result if isinstance(result, int) else ""
        box.setInformativeText(f"{rows} rows\n{path}" if rows != "" else str(path))
        open_button = box.addButton("Show in folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is open_button:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve().parent)))

    def _export_diagnostics(self) -> None:
        default = f"diagnostics_{datetime.now():%Y%m%d_%H%M%S}.zip"
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export diagnostics", default, "Zip archive (*.zip)"
        )
        if not path:
            return

        def work(emit):
            return self.controller.export_diagnostics(path, progress=emit)

        self._run_task(
            work,
            lambda result: self._export_finished(str(result), None),
            title="Building diagnostics…",
            progress=True,
        )

    def _shutdown(self) -> None:
        if self._shutting_down:
            return
        if not confirm(
            self,
            "Shutdown the host?",
            "Stop requests and heartbeats, flush logs and close the console?",
            confirm_label="Shutdown",
            informative=(
                "The controller runs autonomously after the heartbeat gate "
                "closes. Logs are flushed before the window exits."
            ),
        ):
            return
        self._shutting_down = True
        self.centralWidget().setEnabled(False)
        progress = QProgressDialog("Shutting down…", "", 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setCancelButton(None)
        progress.show()

        def work(_emit):
            self.controller.shutdown()
            return True

        def done(_result) -> None:
            progress.close()
            self.centralWidget().setEnabled(True)
            self.close()

        self._run_task(work, done)

    def _toggle_playback(self) -> None:
        self._playback_paused = not self._playback_paused
        if self._playback_paused:
            self.controller.pause_playback()
            self.play_button.setText("Play")
        else:
            self.controller.resume_playback()
            self.play_button.setText("Pause")

    def _open_logs_folder(self) -> None:
        path = Path(self.controller.config.logging.decision_log).resolve().parent
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_config(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Configuration (read-only, secrets redacted)")
        dialog.resize(760, 640)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(config_yaml(self.controller.config))
        layout.addWidget(view)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def _show_wire_window(self) -> None:
        if self._wire_dialog is None:
            self._wire_dialog = WireTrafficDialog(self.controller, self)
        self._wire_dialog.show()
        self._wire_dialog.raise_()
        self._wire_dialog.activateWindow()

    def _show_shortcuts(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Keyboard shortcuts")
        dialog.resize(560, 460)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["Action", "Shortcut"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        table.setObjectName("shortcutHelpTable")
        specs = sorted(
            self._shortcuts.specs, key=lambda spec: spec.description.lower()
        )
        table.setRowCount(len(specs))
        for row, spec in enumerate(specs):
            table.setItem(row, 0, QTableWidgetItem(spec.description))
            table.setItem(row, 1, QTableWidgetItem(spec.keys))
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def _show_about(self) -> None:
        QMessageBox.about(self, "About Smarti AI Host", about_text())

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        if watched is self.work_splitter.handle(1):
            if event.type() == QEvent.Type.MouseButtonDblClick:
                self._toggle_logs()
                return True
        if event.type() in (
            QEvent.Type.KeyPress,
            QEvent.Type.ShortcutOverride,
        ) and self._shortcuts.blocks_key(event):
            # A shortcut must never steal a keystroke from the log search box.
            if event.type() == QEvent.Type.ShortcutOverride:
                event.accept()
            return True
        return super().eventFilter(watched, event)

    # -- shutdown ----------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.controller.running and not self._shutting_down:
            if not confirm(
                self,
                "Shutdown the host?",
                "Shut down the host and close the console?",
                confirm_label="Shutdown",
                informative=(
                    "Requests and heartbeats stop, the controller runs "
                    "autonomously, logs are flushed and the window closes."
                ),
            ):
                event.ignore()
                return
        try:
            self.controller.shutdown()
        except Exception:  # noqa: BLE001 - closing must still proceed
            pass
        self._save_layout()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().closeEvent(event)


def mode_badge_state(snapshot: UiSnapshot) -> tuple[str, str, str]:
    """Return ``(STATE:DETAIL, tone, tooltip)`` for the single mode badge."""
    if snapshot.dry_run:
        return (
            "SIMULATION:DRY-RUN",
            "simulation",
            "Dry run: decisions and logs only. No gateway or hardware is connected.",
        )
    if snapshot.mock_gateway:
        return (
            "SIMULATION:MOCK-GATEWAY",
            "simulation",
            "Loopback mock gateway. Not connected to real firmware or lamps.",
        )
    if snapshot.three_phase:
        return (
            "SIMULATION:THREE-PHASE",
            "simulation",
            "Three-phase proposal only: the required firmware does not exist.",
        )
    if snapshot.file_sources:
        return (
            "SIMULATION:FILE-SOURCES",
            "simulation",
            "A real gateway is configured, but at least one camera is a video file, not a live camera.",
        )
    detail = "COMPAT-2PH" if snapshot.mode == "compat_two_phase" else snapshot.mode.upper().replace("_", "-")
    return (
        f"LIVE:{detail}",
        "live",
        "Live camera sources with a real gateway connection. Advisory only.",
    )


def _percent(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1f}%"
