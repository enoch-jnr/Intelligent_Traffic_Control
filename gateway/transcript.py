"""Exact, bounded transcript of the newline-delimited gateway link.

The client records the JSON text after a successful send and the line before
decoding on receive. This is deliberately separate from the decision log: it
shows the bytes the gateway actually saw, including heartbeats and handshakes,
without claiming that a phase changed.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_WIRE_CAPACITY = 1000


@dataclass(frozen=True)
class WireEntry:
    """One complete JSON line, exactly as transmitted."""

    sequence: int
    at: str
    direction: str
    json: str


class WireTranscript:
    """Thread-safe, bounded TX/RX history for one gateway client."""

    def __init__(self, capacity: int = DEFAULT_WIRE_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("wire transcript capacity must be at least 1")
        self._entries: deque[WireEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._next_sequence = 1

    def record(self, direction: str, json_line: str) -> WireEntry:
        """Append one line and return the stored entry."""
        if direction not in ("TX", "RX"):
            raise ValueError(f"direction must be TX or RX, got {direction!r}")
        if not json_line:
            raise ValueError("wire line must not be empty")
        at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        with self._lock:
            entry = WireEntry(
                sequence=self._next_sequence,
                at=at,
                direction=direction,
                json=json_line,
            )
            self._next_sequence += 1
            self._entries.append(entry)
        return entry

    def entries(self) -> tuple[WireEntry, ...]:
        """Return a stable copy, oldest first."""
        with self._lock:
            return tuple(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
