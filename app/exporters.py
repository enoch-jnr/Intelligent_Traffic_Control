"""Log, CSV and diagnostics exports.

Pure Python, no Qt. Exports never include video frames or credentials: RTSP
user-info is redacted and secret-looking keys are replaced before YAML is
written into a diagnostics bundle.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import platform
import re
import sys
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence

import yaml

Progress = Callable[[int, int], None]

_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)"
    r"(?P<user>[^:/@\s]+):(?P<secret>[^@\s]+)@"
)
_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|credential)"
)

RANGE_SECONDS = {
    "5m": 300,
    "1h": 3600,
    "all": None,
}


def redact_text(text: str) -> str:
    """Hide URL passwords and obvious secret assignments."""
    redacted = _URL_CREDENTIALS.sub(
        lambda match: f"{match.group('scheme')}{match.group('user')}:***@",
        text,
    )
    # YAML/JSON/query style assignments.
    redacted = re.sub(
        r"(?i)((?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*)"
        r"([^\s,;\"'}]+)",
        r"\1***",
        redacted,
    )
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY.search(key):
                result[key] = "***"
            else:
                result[key] = _redact_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def config_yaml(config: Any) -> str:
    """Serialise an AppConfig with credentials removed."""
    if dataclasses.is_dataclass(config):
        data = dataclasses.asdict(config)
    elif isinstance(config, Mapping):
        data = dict(config)
    else:  # pragma: no cover - defensive
        data = {"config": str(config)}
    return yaml.safe_dump(_redact_value(data), sort_keys=False, allow_unicode=True)


def log_paths(path: str | Path, *, include_rotated: bool = True) -> list[Path]:
    """Return the decision log and its numeric rotations."""
    base = Path(path)
    if not base.parent.is_dir():
        return [base]
    paths = [base]
    if include_rotated:
        for item in sorted(base.parent.glob(base.name + ".*")):
            suffix = item.name[len(base.name) + 1 :]
            if suffix.isdigit():
                paths.append(item)
    return paths


def iter_entries(
    path: str | Path, *, include_rotated: bool = True
) -> Iterator[dict[str, Any]]:
    """Stream parseable JSONL decision records without loading the file."""
    for item in log_paths(path, include_rotated=include_rotated):
        if not item.is_file():
            continue
        try:
            handle = item.open("r", encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value


def load_entries(
    path: str | Path, *, include_rotated: bool = True
) -> list[dict[str, Any]]:
    """Read every parseable JSONL decision record, oldest first."""
    entries = list(iter_entries(path, include_rotated=include_rotated))
    entries.sort(key=lambda entry: str(entry.get("ts", "")))
    return entries


def tail_entries(path: str | Path, *, limit: int = 500) -> list[dict[str, Any]]:
    """Keep only the newest ``limit`` records in memory (constant otherwise)."""
    kept: deque[dict[str, Any]] = deque(maxlen=max(1, int(limit)))
    for entry in iter_entries(path):
        kept.append(entry)
    entries = list(kept)
    entries.sort(key=lambda entry: str(entry.get("ts", "")))
    return entries


def _range_cutoff(range_key: str) -> Optional[datetime]:
    key = range_key.strip().lower()
    if key in ("5 min", "last 5 min", "last-5-min"):
        key = "5m"
    if key in ("1 hour", "last hour", "last-hour"):
        key = "1h"
    if key not in RANGE_SECONDS:
        raise ValueError(f"unknown export range {range_key!r}")
    seconds = RANGE_SECONDS[key]
    return None if seconds is None else datetime.now(timezone.utc) - timedelta(
        seconds=seconds
    )


def _in_range(entry: Mapping[str, Any], cutoff: Optional[datetime]) -> bool:
    if cutoff is None:
        return True
    stamp = entry.get("ts")
    if not isinstance(stamp, str):
        return False
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= cutoff


def _line_count(path: str | Path) -> int:
    total = 0
    for item in log_paths(path):
        if not item.is_file():
            continue
        try:
            with item.open("rb") as handle:
                total += sum(1 for line in handle if line.strip())
        except OSError:
            continue
    return total


def export_log_file(
    source: str | Path,
    target: str | Path,
    fmt: str,
    range_key: str = "all",
    *,
    progress: Optional[Progress] = None,
) -> int:
    """Stream a decision-log export, one record at a time.

    JSONL needs one pass. CSV makes a metadata pass for the dynamic approach
    and unit columns, then writes the data pass. Neither holds the log in RAM.
    """
    kind = fmt.lower()
    if kind not in ("jsonl", "json", "csv"):
        raise ValueError(f"unknown export format {fmt!r}")
    cutoff = _range_cutoff(range_key)
    total = _line_count(source)
    output = Path(target)
    output.parent.mkdir(parents=True, exist_ok=True)

    if kind in ("jsonl", "json"):
        count = 0
        with output.open("w", encoding="utf-8", newline="\n") as handle:
            for entry in iter_entries(source):
                if not _in_range(entry, cutoff):
                    continue
                handle.write(
                    json.dumps(entry, separators=(",", ":"), ensure_ascii=False)
                    + "\n"
                )
                count += 1
                if progress is not None and count % 200 == 0:
                    progress(count, total)
        if progress is not None:
            progress(total, total)
        return count

    base_fields = [
        "timestamp",
        "approach_layout",
        "mode",
        "gate_open",
        "unit",
        "wire_movement",
        "green_time",
        "request_sent",
        "request_status",
        "accepted",
        "latency_s",
    ]
    extras: set[str] = set()
    for entry in iter_entries(source):
        if not _in_range(entry, cutoff):
            continue
        extras.update(key for key in _flatten(entry) if key not in base_fields)
    fieldnames = base_fields + sorted(extras)
    count = 0
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for entry in iter_entries(source):
            if not _in_range(entry, cutoff):
                continue
            writer.writerow(_flatten(entry))
            count += 1
            if progress is not None and count % 200 == 0:
                progress(count, total)
    if progress is not None:
        progress(total, total)
    return count


def filter_range(
    entries: Sequence[dict[str, Any]],
    range_key: str,
    *,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Filter by ``last 5 min``, ``last hour`` or ``all``."""
    key = range_key.strip().lower()
    if key in ("5 min", "last 5 min", "last-5-min"):
        key = "5m"
    if key in ("1 hour", "last hour", "last-hour"):
        key = "1h"
    if key not in RANGE_SECONDS:
        raise ValueError(f"unknown export range {range_key!r}")
    seconds = RANGE_SECONDS[key]
    if seconds is None:
        return list(entries)

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=seconds)
    kept: list[dict[str, Any]] = []
    for entry in entries:
        stamp = entry.get("ts")
        if not isinstance(stamp, str):
            continue
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed >= cutoff:
            kept.append(entry)
    return kept


def _flatten(entry: Mapping[str, Any]) -> dict[str, Any]:
    recommendation = entry.get("recommendation") or {}
    request = entry.get("request") or {}
    row: dict[str, Any] = {
        "timestamp": entry.get("ts", ""),
        "approach_layout": entry.get("approach_layout", "v1"),
        "mode": entry.get("mode", ""),
        "gate_open": entry.get("health_gate_open", ""),
        "unit": recommendation.get("unit_id", ""),
        "wire_movement": recommendation.get("wire_movement", ""),
        "green_time": recommendation.get("green_time", ""),
        "request_sent": request.get("sent", ""),
        "request_status": request.get("status", request.get("reason", "")),
        "accepted": request.get("accepted", ""),
        "latency_s": request.get("latency_s", ""),
    }
    for approach_id, metrics in (entry.get("approaches") or {}).items():
        if not isinstance(metrics, Mapping):
            continue
        prefix = str(approach_id).replace(" ", "_")
        row[f"{prefix}.vehicles"] = metrics.get("vehicle_count_in_region", "")
        row[f"{prefix}.stationary"] = metrics.get("stationary_count", "")
        row[f"{prefix}.max_wait_s"] = metrics.get("max_wait_s", "")
        row[f"{prefix}.congestion"] = metrics.get("congestion_level", "")
        row[f"{prefix}.healthy"] = metrics.get("healthy", "")
    for unit_id, unit in (entry.get("units") or {}).items():
        if not isinstance(unit, Mapping):
            continue
        row[f"unit.{unit_id}.score"] = unit.get("score", "")
        row[f"unit.{unit_id}.expected_red_s"] = unit.get("expected_red_s", "")
    return row


def export_entries(
    entries: Sequence[Mapping[str, Any]],
    path: str | Path,
    fmt: str,
    *,
    progress: Optional[Progress] = None,
) -> int:
    """Write JSONL or flattened CSV and return the row count."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    kind = fmt.lower()
    total = len(entries)
    if kind in ("jsonl", "json"):
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            for index, entry in enumerate(entries, start=1):
                handle.write(
                    json.dumps(dict(entry), separators=(",", ":"), ensure_ascii=False)
                    + "\n"
                )
                if progress is not None and index % 200 == 0:
                    progress(index, total)
        if progress is not None:
            progress(total, total)
        return total

    if kind != "csv":
        raise ValueError(f"unknown export format {fmt!r}")

    rows = [_flatten(entry) for entry in entries]
    base_fields = [
        "timestamp",
        "approach_layout",
        "mode",
        "gate_open",
        "unit",
        "wire_movement",
        "green_time",
        "request_sent",
        "request_status",
        "accepted",
        "latency_s",
    ]
    extras: list[str] = []
    seen = set(base_fields)
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                extras.append(key)
    extras.sort()

    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=base_fields + extras)
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            writer.writerow(row)
            if progress is not None and index % 200 == 0:
                progress(index, total)
    if progress is not None:
        progress(total, total)
    return total


def export_text(lines: Iterable[str], path: str | Path) -> int:
    """Write already-filtered log text."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = [line.rstrip("\n") for line in lines]
    target.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    return len(rows)


def export_diagnostics(
    path: str | Path,
    *,
    config: Any,
    entries: Sequence[Mapping[str, Any]] = (),
    log_lines: Sequence[str] = (),
    camera_health: Optional[Mapping[str, Any]] = None,
    versions: Optional[Mapping[str, str]] = None,
    config_path: Optional[str] = None,
    max_decisions: int = 500,
    max_log_lines: int = 5000,
) -> Path:
    """Write a zip with redacted config, versions, logs, decisions and health.

    Deliberately contains no video frames or images.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    version_data: dict[str, str] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "application": "Smarti AI host",
    }
    if versions:
        version_data.update({str(key): str(value) for key, value in versions.items()})
    if config_path:
        version_data["config_path"] = str(config_path)

    clipped_entries = list(entries)[-max_decisions:]
    clipped_logs = list(log_lines)[-max_log_lines:]

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("config.redacted.yaml", config_yaml(config))
        archive.writestr(
            "versions.json", json.dumps(version_data, indent=2, sort_keys=True)
        )
        archive.writestr("logs.txt", "\n".join(clipped_logs) + "\n")
        archive.writestr(
            "decisions.jsonl",
            "".join(
                json.dumps(dict(entry), separators=(",", ":"), ensure_ascii=False)
                + "\n"
                for entry in clipped_entries
            ),
        )
        archive.writestr(
            "camera_health.json",
            json.dumps(dict(camera_health or {}), indent=2, sort_keys=True),
        )
    return target
