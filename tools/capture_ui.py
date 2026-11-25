"""Capture the real console offscreen at fixed sizes with synthetic feeds.

    python -m tools.capture_ui --out docs/ui/after/1280x720.png --size 1280x720

Uses generated clips and the fake detector so captures are fast and repeatable.
They are layout evidence, never evidence of camera or hardware behaviour.
"""

from __future__ import annotations

import argparse
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import build_config  # noqa: E402
from app.controller import AppController  # noqa: E402
from gateway.mock_gateway import MockGateway  # noqa: E402
from tests.conftest import write_test_video  # noqa: E402
from tests.test_orchestrator import FixedCountDetector  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


def _font() -> None:
    for name in (
        "segoeui.ttf",
        "segoeuib.ttf",
        "seguisb.ttf",
        "consola.ttf",
        "consolab.ttf",
        "CascadiaMono.ttf",
        "CascadiaCode.ttf",
    ):
        path = Path("C:/Windows/Fonts") / name
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))


def capture(
    *,
    out: str,
    width: int,
    height: int,
    seconds: float,
    theme: str = "dark",
    preview: bool = True,
) -> None:
    app = QApplication.instance() or QApplication([])
    _font()
    tmp = Path(tempfile.mkdtemp(prefix="smarti_capture_"))
    clips = {
        name: write_test_video(
            tmp / f"{name}.avi",
            frames=180,
            start_x=40 + 20 * shift,
            start_y=20,
            step_y=5,
            moving=False,
        )
        for name, shift in (("MAIN_A", 2), ("MAIN_B", 5), ("SEC", 3))
    }

    def config(port: int):
        return build_config(
            {
                "cameras": [
                    {
                        "id": {"MAIN_A": "cam_main_a", "MAIN_B": "cam_main_b", "SEC": "cam_sec"}[approach],
                        "approach": approach,
                        "source": "file",
                        "path": str(clips[approach]),
                        "loop": True,
                        "realtime": True,
                        "target_fps": 15,
                    }
                    for approach in ("MAIN_A", "MAIN_B", "SEC")
                ],
                "decision": {
                    "units": [
                        {"id": "MAIN", "approaches": ["MAIN_A", "MAIN_B"]},
                        {"id": "SECONDARY", "approaches": ["SEC"]},
                    ],
                    "smoothing_window_s": 0,
                    "min_interval_s": 0,
                    "min_delta_s": 0,
                    "idle_score_threshold": 0.5,
                },
                "gateway": {
                    "host": "127.0.0.1",
                    "port": port,
                    "heartbeat_interval_s": 0.5,
                    "connect_timeout_s": 0.5,
                    "handshake_timeout_s": 1.0,
                    "request_timeout_s": 1.0,
                    "reconnect_min_s": 0.1,
                    "reconnect_max_s": 0.3,
                    "request_backoff_s": 0.05,
                },
                "health": {
                    "tick_interval_s": 0.25,
                    "max_frame_age_s": 2.0,
                    "tick_timeout_s": 2.0,
                },
                "logging": {"decision_log": str(tmp / "decisions.jsonl")},
            }
        )

    with MockGateway(port=0) as gateway:
        controller = AppController(
            config(gateway.port),
            mock_gateway=True,
            annotate_frames=True,
            detector_factory=lambda camera: FixedCountDetector(
                {"MAIN_A": 2, "MAIN_B": 5, "SEC": 3}[camera.approach]
            ),
        )
        settings = QSettings(str(tmp / "ui.ini"), QSettings.Format.IniFormat)
        window = MainWindow(controller, settings=settings, theme_name=theme)
        window.resize(width, height)
        window.show()
        if not preview and hasattr(window, "set_video_preview"):
            window.set_video_preview(False)
        controller.start()
        deadline = time.monotonic() + max(4.0, seconds)
        try:
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            for _ in range(8):
                app.processEvents()
                time.sleep(0.03)
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(out)
            print(f"wrote {out} ({Path(out).stat().st_size} bytes)")
        finally:
            window.hide()
            controller.shutdown()
            app.processEvents()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--size", default="1280x720")
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--theme", choices=("dark", "light"), default="dark")
    parser.add_argument("--no-preview", action="store_true")
    args = parser.parse_args(argv)
    width, height = (int(part) for part in args.size.lower().split("x"))
    capture(
        out=args.out,
        width=width,
        height=height,
        seconds=args.seconds,
        theme=args.theme,
        preview=not args.no_preview,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
