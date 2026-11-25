"""Configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import (
    CameraConfig,
    ConfigError,
    build_config,
    build_source,
    load_config,
)
from camera.file_source import FileSource
from camera.rtsp import RtspSource
from camera.webcam import WebcamSource
from decision.models import GREEN_TIME_MAX, GREEN_TIME_MIN, ServingUnit


ENV = {"CAM_USER": "camera", "CAM_PASS": "secret"}


def test_example_config_loads_with_env_credentials():
    config = load_config("config.example.yaml", env=ENV)

    assert config.camera_ids == ("cam_main_a", "cam_main_b", "cam_sec")
    assert config.units == (
        ServingUnit("MAIN", ("MAIN_A", "MAIN_B")),
        ServingUnit("SECONDARY", ("SEC",)),
    )
    assert config.required_camera_count == 3
    assert config.gateway.host == "192.168.4.1"
    assert config.gateway.port == 3333
    assert config.decision.aggregation == "sum"
    assert GREEN_TIME_MIN <= config.decision.min_green_s
    assert config.decision.max_green_s <= GREEN_TIME_MAX
    assert config.cameras[0].url.startswith("rtsp://camera:secret@192.168.50.12")
    assert config.cameras[2].url.startswith("rtsp://camera:secret@192.168.50.11")
    # The old ambiguous mode switch is gone; the layout lives in units.
    assert not hasattr(config, "phase_mode")


def test_retired_approaches_and_camera_ids_are_refused_with_migration_help():
    for camera in (
        _camera(id="legacy", approach="MAIN"),
        _camera(id="legacy", approach="SEC_A"),
        _camera(id="cam_main", approach="MAIN_A"),
        _camera(id="cam_sec_a", approach="MAIN_A"),
    ):
        with pytest.raises(ConfigError) as excinfo:
            build_config(
                {
                    "cameras": [camera],
                    "decision": {"units": _units(("MAIN", ("MAIN_A",)))},
                }
            )
        message = str(excinfo.value)
        assert "retired approach layout" in message
        assert "docs/MIGRATION_RENAME.md" in message
        assert "old MAIN is now SEC" in message


def test_retired_three_phase_names_are_refused():
    raw = _three_phase_raw()
    raw["gateway"]["three_phase"]["movements"] = {
        "MAIN": "MAIN",
        "SEC_A": "SECONDARY_A",
        "SEC_B": "SECONDARY_B",
    }
    with pytest.raises(ConfigError, match="retired names"):
        build_config(raw)


def test_compat_units_cannot_cross_wire_groups():
    raw = _raw(
        cameras=[_camera(id="cam_sec", approach="SEC")],
        decision={"units": _units(("MAIN", ("SEC",)))},
    )
    with pytest.raises(ConfigError, match="compat unit 'MAIN' cannot contain"):
        build_config(raw)


def test_missing_environment_variable_is_reported():
    with pytest.raises(ConfigError) as excinfo:
        load_config("config.example.yaml", env={})
    message = str(excinfo.value)
    assert "CAM_USER" in message and "CAM_PASS" in message


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _camera(**overrides) -> dict:
    camera = {
        "id": "cam_main_a",
        "approach": "MAIN_A",
        "source": "file",
        "path": "clip.avi",
    }
    camera.update(overrides)
    return camera


def _units(*entries: tuple[str, tuple[str, ...]]) -> list[dict]:
    return [
        {"id": unit_id, "approaches": list(approaches)}
        for unit_id, approaches in entries
    ]


def _raw(**overrides) -> dict:
    raw = {
        "cameras": [_camera()],
        "decision": {"units": _units(("MAIN", ("MAIN_A",)))},
    }
    raw.update(overrides)
    return raw


def test_relative_file_paths_resolve_against_the_config_file(tmp_path):
    path = _write(tmp_path, _raw())
    config = load_config(path, env={})
    assert Path(config.cameras[0].path) == tmp_path / "clip.avi"


def test_two_cameras_cannot_share_an_approach():
    raw = {
        "cameras": [_camera(), _camera(id="cam_other")],
        "decision": {"units": _units(("MAIN", ("MAIN_A",)))},
    }
    with pytest.raises(ConfigError, match="one camera per approach"):
        build_config(raw)


def test_compat_mode_requires_the_firmware_unit_names():
    raw = _raw(decision={"units": _units(("NORTH", ("MAIN_A",)))})
    with pytest.raises(ConfigError, match="compat_two_phase units"):
        build_config(raw)


def test_camera_approach_must_be_assigned_to_a_unit():
    raw = _raw(
        cameras=[
            _camera(),
            _camera(id="cam_sec", approach="SEC"),
        ],
        decision={"units": _units(("MAIN", ("MAIN_A",)))},
    )
    with pytest.raises(ConfigError, match="missing from decision.units"):
        build_config(raw)


def test_unit_may_not_reference_an_approach_without_a_camera():
    raw = _raw(
        decision={"units": _units(("MAIN", ("MAIN_A", "SEC")))},
    )
    with pytest.raises(ConfigError, match="no camera"):
        build_config(raw)


def test_an_approach_may_not_be_in_two_units():
    raw = _raw(
        decision={
            "units": _units(
                ("MAIN", ("MAIN_A",)),
                ("SECONDARY", ("MAIN_A",)),
            )
        }
    )
    with pytest.raises(ConfigError, match="assigned to both"):
        build_config(raw)


def test_duplicate_camera_ids_are_rejected():
    raw = _raw(
        cameras=[_camera(), _camera(approach="MAIN_B")],
        decision={
            "units": _units(
                ("MAIN", ("MAIN_A",)),
                ("SECONDARY", ("MAIN_B",)),
            )
        },
    )
    with pytest.raises(ConfigError, match="unique"):
        build_config(raw)


def test_unknown_keys_are_rejected():
    raw = _raw(gateway={"host": "127.0.0.1", "portt": 3333})
    with pytest.raises(ConfigError, match="unknown keys"):
        build_config(raw)


def test_legacy_layout_keys_are_rejected_with_a_migration_hint():
    with pytest.raises(ConfigError, match="decision.units"):
        build_config(
            {
                "cameras": [_camera()],
                "approaches": {"MAIN": "MAIN"},
                "decision": {"units": _units(("MAIN", ("MAIN_A",)))},
            }
        )
    with pytest.raises(ConfigError, match="gateway.mode"):
        build_config(
            {
                "cameras": [_camera()],
                "phase_mode": "two_phase_compat",
                "decision": {"units": _units(("MAIN", ("MAIN_A",)))},
            }
        )


def test_green_range_must_stay_inside_the_protocol():
    raw = _raw(decision={"units": _units(("MAIN", ("MAIN_A",))), "max_green_s": 40})
    with pytest.raises(ConfigError, match="green time"):
        build_config(raw)

    narrowed = build_config(
        _raw(decision={"units": _units(("MAIN", ("MAIN_A",))), "min_green_s": 8, "max_green_s": 25})
    )
    assert (narrowed.decision.min_green_s, narrowed.decision.max_green_s) == (8, 25)


def test_polygon_must_be_normalized():
    raw = _raw(
        cameras=[_camera(polygon=[[0.1, 0.3], [0.9, 0.3], [1.5, 0.95]])]
    )
    with pytest.raises(ConfigError, match="normalized"):
        build_config(raw)


def test_file_camera_needs_a_path():
    camera = _camera()
    camera.pop("path")
    with pytest.raises(ConfigError, match="without 'path'"):
        build_config(_raw(cameras=[camera]))


def test_decision_units_must_be_a_non_empty_list():
    with pytest.raises(ConfigError, match="non-empty list"):
        build_config(_raw(decision={"units": []}))
    with pytest.raises(ConfigError, match="non-empty list"):
        build_config({"cameras": [_camera()]})


THREE_PHASE_UNITS = _units(
    ("MAIN_A", ("MAIN_A",)),
    ("MAIN_B", ("MAIN_B",)),
    ("SEC", ("SEC",)),
)
THREE_PHASE_MOVEMENTS = {
    "MAIN_A": "MAIN_A",
    "MAIN_B": "MAIN_B",
    "SEC": "SECONDARY",
}


def _three_phase_cameras() -> list[dict]:
    return [
        _camera(id="cam_main_a", approach="MAIN_A"),
        _camera(id="cam_main_b", approach="MAIN_B"),
        _camera(id="cam_sec", approach="SEC"),
    ]


def _three_phase_raw(**gateway) -> dict:
    gateway.setdefault("mode", "three_phase")
    gateway.setdefault(
        "three_phase",
        {"enabled": True, "movements": dict(THREE_PHASE_MOVEMENTS)},
    )
    return {
        "cameras": _three_phase_cameras(),
        "decision": {"units": THREE_PHASE_UNITS},
        "gateway": gateway,
    }


def test_three_phase_layout_loads_when_explicitly_enabled():
    config = build_config(_three_phase_raw())

    assert config.gateway_mode == "three_phase"
    assert config.three_phase_enabled is True
    assert config.gateway_movements == THREE_PHASE_MOVEMENTS
    assert [unit.id for unit in config.units] == ["MAIN_A", "MAIN_B", "SEC"]


def test_three_phase_is_refused_while_disabled():
    raw = _three_phase_raw()
    raw["gateway"]["three_phase"]["enabled"] = False

    with pytest.raises(ConfigError, match="enabled is false"):
        build_config(raw)


def test_three_phase_needs_explicit_movement_names():
    raw = _three_phase_raw()
    del raw["gateway"]["three_phase"]["movements"]["SEC"]

    with pytest.raises(ConfigError, match="explicitly name every unit"):
        build_config(raw)


def test_three_units_are_rejected_in_compat_mode():
    raw = _three_phase_raw(mode="compat_two_phase")
    raw["gateway"].pop("three_phase")

    with pytest.raises(ConfigError, match="compat_two_phase units"):
        build_config(raw)


def test_enabling_three_phase_while_in_compat_mode_is_an_error():
    raw = _raw(
        gateway={
            "mode": "compat_two_phase",
            "three_phase": {"enabled": True},
        }
    )
    with pytest.raises(ConfigError, match="choose gateway.mode"):
        build_config(raw)


def test_unknown_gateway_mode_is_rejected():
    with pytest.raises(ConfigError, match="gateway.mode must be"):
        build_config(_raw(gateway={"mode": "split_phase"}))


def test_three_phase_movements_must_be_unique_and_complete():
    duplicate = _three_phase_raw()
    duplicate["gateway"]["three_phase"]["movements"] = {
        "MAIN_A": "MAIN_A",
        "MAIN_B": "SIDE",
        "SEC": "SIDE",
    }
    with pytest.raises(ConfigError, match="unique"):
        build_config(duplicate)

    extra = _three_phase_raw()
    extra["gateway"]["three_phase"]["movements"]["NORTH"] = "NORTH"
    with pytest.raises(ConfigError, match="unknown units"):
        build_config(extra)


def test_unknown_three_phase_keys_are_rejected():
    raw = _three_phase_raw()
    raw["gateway"]["three_phase"]["magic"] = True
    with pytest.raises(ConfigError, match="unknown keys"):
        build_config(raw)


def _two_unit_raw(**decision) -> dict:
    raw = {
        "cameras": [
            _camera(),
            _camera(id="cam_sec", approach="SEC"),
        ],
        "decision": {
            "units": _units(
                ("MAIN", ("MAIN_A",)),
                ("SECONDARY", ("SEC",)),
            )
        },
    }
    raw["decision"].update(decision)
    return raw


def test_example_red_model_is_configured():
    config = load_config("config.example.yaml", env=ENV)
    assert config.decision.yellow_s == 3.0
    assert config.decision.all_red_s == 1.0
    assert config.decision.max_red_s == 90.0
    assert config.decision.green_memory_s == 120.0


def test_max_red_accepts_a_scalar_for_every_unit():
    config = build_config(_two_unit_raw(max_red_s=45))
    assert config.decision.max_red_s == 45.0
    assert config.decision.max_red_s_by_unit == {}


def test_max_red_accepts_a_complete_per_unit_mapping():
    config = build_config(
        _two_unit_raw(max_red_s={"MAIN": 30, "SECONDARY": 60})
    )
    assert config.decision.max_red_s_by_unit == {
        "MAIN": 30.0,
        "SECONDARY": 60.0,
    }


def test_max_red_mapping_must_cover_known_units():
    with pytest.raises(ConfigError, match="cap every unit"):
        build_config(_two_unit_raw(max_red_s={"MAIN": 30}))
    with pytest.raises(ConfigError, match="unknown units"):
        build_config(
            _two_unit_raw(
                max_red_s={"MAIN": 30, "SECONDARY": 60, "NORTH": 90}
            )
        )


def test_max_red_and_clearances_are_validated():
    with pytest.raises(ConfigError, match="must be a number"):
        build_config(_two_unit_raw(max_red_s="ninety"))
    with pytest.raises(ConfigError, match="must be positive"):
        build_config(_two_unit_raw(max_red_s=-5))
    with pytest.raises(ConfigError, match="non-negative"):
        build_config(_two_unit_raw(yellow_s=-1))


def test_source_factory_builds_each_type():
    common = {"id": "cam", "approach": "MAIN_A"}
    webcam = build_source(CameraConfig(source="webcam", index=1, **common))
    rtsp = build_source(
        CameraConfig(source="rtsp", url="rtsp://user:pass@10.0.0.2/stream", **common)
    )
    file = build_source(CameraConfig(source="file", path="clip.avi", **common))

    assert isinstance(webcam, WebcamSource)
    assert webcam.index == 1
    assert isinstance(rtsp, RtspSource)
    # The password must not leak through repr/log helpers.
    assert rtsp.health_message() == "rtsp://user:***@10.0.0.2/stream"
    assert isinstance(file, FileSource)
    assert file.path.name == "clip.avi"
