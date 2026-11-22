"""Entry point for the Smarti AI host.

Normal use::

    python main.py --config config.yaml --mock-gateway
    python main.py --config config.yaml                 # real ESP32
    python main.py --config config.yaml --dry-run       # decide + log only

Legacy single-camera demo (no config file, no gateway)::

    python main.py --source webcam:0
    python main.py --source file:samples/clip.avi
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from typing import Optional, Sequence

from app.config import (
    AppConfig,
    CameraConfig,
    ConfigError,
    DetectionConfig,
    HealthConfig,
    LoggingConfig,
    load_config,
)
from app.orchestrator import Orchestrator
from decision.models import ServingUnit
from decision.settings import DecisionSettings
from gateway.adapter import COMPAT_MODE, THREE_PHASE_MODE
from gateway.client import GatewayConfig

_log = logging.getLogger("main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smarti AI traffic host: cameras -> decision -> ESP32 gateway"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="path to the YAML config (default: config.yaml)",
    )
    parser.add_argument(
        "--display",
        action="store_true",
        help="show the annotated camera tiles",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="never open a window (overrides --display and config display)",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="open the optional operator console (PySide6)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="decide and log but do not connect to a gateway",
    )
    parser.add_argument(
        "--mock-gateway",
        action="store_true",
        help="connect to the mock gateway on 127.0.0.1",
    )
    parser.add_argument(
        "--source",
        help="legacy single-camera demo: webcam:0, file:clip.avi or rtsp://...",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="stop after this many seconds (smoke runs)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="console log level (default: logging.level from config)",
    )
    return parser


def config_from_source(spec: str) -> AppConfig:
    """Build a one-camera demo config from ``--source``."""
    raw = spec.strip()
    if raw.startswith(("rtsp://", "rtsps://")):
        kind, value = "rtsp", raw
    else:
        kind, separator, value = raw.partition(":")
        if not separator:
            value = ""
        kind = kind.strip().lower()

    if kind == "webcam":
        camera = CameraConfig(
            id="demo_webcam",
            approach="MAIN_A",
            source="webcam",
            index=int(value or 0),
            flip=True,
            target_fps=12.0,
        )
    elif kind == "file":
        if not value:
            raise ConfigError("--source file: needs a video path")
        camera = CameraConfig(
            id="demo_file",
            approach="MAIN_A",
            source="file",
            path=value,
            loop=True,
            realtime=True,
            target_fps=12.0,
        )
    elif kind == "rtsp":
        if not value:
            raise ConfigError("--source rtsp: needs an rtsp:// URL")
        camera = CameraConfig(
            id="demo_rtsp",
            approach="MAIN_A",
            source="rtsp",
            url=value,
            target_fps=12.0,
        )
    else:
        raise ConfigError(
            f"unknown --source {spec!r}; use webcam:0, file:PATH or rtsp://URL"
        )

    return AppConfig(
        cameras=(camera,),
        units=(ServingUnit("MAIN", ("MAIN_A",)),),
        decision=DecisionSettings(),
        gateway=GatewayConfig(),
        health=HealthConfig(),
        logging=LoggingConfig(decision_log=""),
        detection=DetectionConfig(),
        display=True,
        gateway_mode=COMPAT_MODE,
        gateway_movements={"MAIN": "MAIN"},
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.source:
            config = config_from_source(args.source)
        else:
            config = load_config(args.config)
    except ConfigError as exc:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        _log.error("%s", exc)
        if not args.source:
            _log.error(
                "copy config.example.yaml to config.yaml and edit it, "
                "or run a demo with --source webcam:0"
            )
        return 2

    logging.basicConfig(
        level=getattr(logging, (args.log_level or config.logging.level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.mock_gateway:
        config = replace(
            config, gateway=replace(config.gateway, host="127.0.0.1")
        )
        if args.dry_run:
            _log.warning("--dry-run wins over --mock-gateway; no gateway is used")
        elif config.gateway_mode == THREE_PHASE_MODE:
            _log.warning(
                "three_phase mode with --mock-gateway: start the mock with "
                "--movements MAIN_A,MAIN_B,SECONDARY or it will reject "
                "the proposed names"
            )

    if args.gui:
        if args.headless:
            _log.error("--gui and --headless are mutually exclusive")
            return 2
        if args.display:
            _log.warning("--display is ignored with --gui; the console shows frames")
        from app.controller import AppController
        from ui.app import run_gui

        controller = AppController(
            config,
            config_path=None if args.source else args.config,
            dry_run=args.dry_run,
            mock_gateway=args.mock_gateway,
            display=False,
            annotate_frames=True,
        )
        return run_gui(controller, argv=sys.argv[:1])

    display = (args.display or config.display) and not args.headless
    orchestrator = Orchestrator(config, display=display, dry_run=args.dry_run)

    try:
        orchestrator.run(duration_s=args.duration)
    except KeyboardInterrupt:
        _log.info("interrupted; shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
