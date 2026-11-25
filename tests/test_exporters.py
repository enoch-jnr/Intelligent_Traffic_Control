"""Exporter content, ranges, rotation and secret redaction."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone

from app.config import build_config
from app.exporters import (
    config_yaml,
    export_diagnostics,
    export_entries,
    export_text,
    filter_range,
    load_entries,
    redact_text,
)


def _entry(stamp: datetime, unit: str = "MAIN", movement: str = "MAIN") -> dict:
    return {
        "ts": stamp.isoformat(timespec="milliseconds"),
        "mode": "compat_two_phase",
        "health_gate_open": True,
        "approaches": {
            "MAIN": {
                "vehicle_count_in_region": 3,
                "stationary_count": 1,
                "max_wait_s": 4.0,
                "congestion_level": "Moderate",
                "healthy": True,
            }
        },
        "units": {"MAIN": {"score": 4.5, "expected_red_s": 12.0}},
        "decision": {"unit_scores": {"MAIN": 4.5}, "reasons": ["busy"]},
        "recommendation": {
            "unit_id": unit,
            "wire_movement": movement,
            "green_time": 14,
            "reasons": ["busy"],
            "expected_red_s": {"MAIN": 12.0},
        },
        "request": {
            "sent": True,
            "status": "CONTROLLER_ACCEPTED",
            "accepted": True,
            "latency_s": 0.02,
        },
    }


def test_load_entries_includes_rotated_files_and_sorts(tmp_path):
    now = datetime.now(timezone.utc)
    base = tmp_path / "decisions.jsonl"
    base.write_text(json.dumps(_entry(now)), encoding="utf-8")
    rotation = tmp_path / "decisions.jsonl.1"
    rotation.write_text(
        json.dumps(_entry(now - timedelta(minutes=10))) + "\nnot json\n",
        encoding="utf-8",
    )

    entries = load_entries(base)
    assert len(entries) == 2
    assert entries[0]["ts"] < entries[1]["ts"]


def test_range_filter_uses_record_timestamps(tmp_path):
    now = datetime.now(timezone.utc)
    entries = [
        _entry(now - timedelta(minutes=30)),
        _entry(now - timedelta(minutes=2)),
        _entry(now),
    ]
    assert len(filter_range(entries, "5m", now=now)) == 2
    assert len(filter_range(entries, "1h", now=now)) == 3
    assert len(filter_range(entries, "all", now=now)) == 3


def test_csv_flattens_approaches_and_units(tmp_path):
    now = datetime.now(timezone.utc)
    target = tmp_path / "out.csv"
    count = export_entries([_entry(now)], target, "csv")
    assert count == 1

    text = target.read_text(encoding="utf-8")
    header, row = text.splitlines()
    assert "wire_movement" in header
    assert "MAIN.vehicles" in header
    assert "unit.MAIN.expected_red_s" in header
    assert "CONTROLLER_ACCEPTED" in row
    assert "MAIN,14" in row or "MAIN" in row


def test_jsonl_round_trip(tmp_path):
    now = datetime.now(timezone.utc)
    target = tmp_path / "out.jsonl"
    export_entries([_entry(now)], target, "jsonl")
    loaded = load_entries(target, include_rotated=False)
    assert loaded[0]["recommendation"]["unit_id"] == "MAIN"


def test_text_export_writes_filtered_lines(tmp_path):
    target = tmp_path / "filtered.log"
    count = export_text(["2026-01-01 INFO one", "2026-01-01 ERROR two"], target)
    assert count == 2
    assert "ERROR two" in target.read_text(encoding="utf-8")


def test_credentials_are_redacted_in_text_and_config():
    assert redact_text("rtsp://admin:hunter2@10.0.0.2/stream") == (
        "rtsp://admin:***@10.0.0.2/stream"
    )
    config = build_config(
        {
            "cameras": [
                {
                    "id": "cam",
                    "approach": "MAIN_A",
                    "source": "rtsp",
                    "url": "rtsp://admin:hunter2@10.0.0.2/stream",
                }
            ],
            "decision": {"units": [{"id": "MAIN", "approaches": ["MAIN_A"]}]},
        }
    )
    rendered = config_yaml(config)
    assert "hunter2" not in rendered
    assert "admin:***@" in rendered


def test_diagnostics_bundle_has_no_images_and_no_secret(tmp_path):
    config = build_config(
        {
            "cameras": [
                {
                    "id": "cam",
                    "approach": "MAIN_A",
                    "source": "rtsp",
                    "url": "rtsp://admin:hunter2@10.0.0.2/stream",
                }
            ],
            "decision": {"units": [{"id": "MAIN", "approaches": ["MAIN_A"]}]},
        }
    )
    target = tmp_path / "diagnostics.zip"
    export_diagnostics(
        target,
        config=config,
        entries=[_entry(datetime.now(timezone.utc))],
        log_lines=["INFO started"],
        camera_health={"cam": {"state": "LIVE"}},
        versions={"pyside6": "6.12.0"},
        config_path="config.yaml",
    )

    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert names == {
            "config.redacted.yaml",
            "versions.json",
            "logs.txt",
            "decisions.jsonl",
            "camera_health.json",
        }
        assert not any(name.lower().endswith((".png", ".jpg", ".mp4")) for name in names)
        everything = b"".join(archive.read(name) for name in names)
        assert b"hunter2" not in everything
        assert b"6.12.0" in everything
