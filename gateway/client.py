"""TCP client for the ESP32-S3 Smarti gateway.

Protocol rules implemented here (see the prompt / README):

* newline-delimited compact JSON, 512 bytes maximum per line;
* one reader thread parses every inbound line and routes it by ``type``;
* heartbeat every second, gated on an external health callback;
* handshake on every (re)connect, then the sequence counter restarts at 1;
* at most one traffic request in flight, 2 s reply timeout, no tight retries;
* a disconnect invalidates the session immediately and triggers backoff.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from gateway.protocol import (
    GREEN_MAX,
    GREEN_MIN,
    MOVEMENTS,
    LineBuffer,
    ProtocolError,
    ack_heartbeat,
    decode_message,
    encode_message,
    heartbeat_message,
    is_accepted_ack,
    is_heartbeat_ack,
    status_request_message,
    traffic_request_message,
)
from gateway.transcript import WireEntry, WireTranscript

_log = logging.getLogger("gateway.client")


@dataclass(frozen=True)
class GatewayConfig:
    host: str = "192.168.4.1"
    port: int = 3333
    connect_timeout_s: float = 3.0
    handshake_timeout_s: float = 2.0
    request_timeout_s: float = 2.0
    heartbeat_interval_s: float = 1.0
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 10.0
    request_backoff_s: float = 2.0
    recv_chunk: int = 4096


@dataclass(frozen=True)
class RequestResult:
    """Outcome of one request; never claims a phase change happened."""

    ok: bool
    status: str
    sequence: Optional[int] = None
    reason: Optional[str] = None
    uno_ack: Optional[str] = None
    latency_s: float = 0.0
    raw: Optional[dict] = None


@dataclass
class _Pending:
    kind: str
    sequence: Optional[int] = None
    event: threading.Event = field(default_factory=threading.Event)
    message: Optional[dict] = None


class GatewayClient:
    """Threaded, reconnecting client with a gated heartbeat."""

    def __init__(
        self,
        config: GatewayConfig,
        health_check: Callable[[], bool],
        *,
        name: str = "gateway",
        movements: Optional[Iterable[str]] = None,
        wire_listener: Optional[Callable[[WireEntry], None]] = None,
    ) -> None:
        self.config = config
        self._health_check = health_check
        self._wire_listener = wire_listener
        # The transcript is always available to the GUI and CLI; it is bounded
        # so a long-running link cannot grow without limit.
        self.wire = WireTranscript()
        # Current firmware names by default; the adapter may pass the proposed
        # three-phase names after an explicit opt-in.
        self._movements: tuple[str, ...] = tuple(movements or MOVEMENTS)
        self._log = logging.getLogger(f"gateway.client.{name}")

        self._stop = threading.Event()
        self._session_ready = threading.Event()
        self._heartbeat_ack = threading.Event()

        self._socket: Optional[socket.socket] = None
        self._session_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._request_lock = threading.Lock()

        self._pending_lock = threading.Lock()
        self._pending: Optional[_Pending] = None

        self._next_sequence = 1
        self._backoff_until = 0.0
        self._gate_open: Optional[bool] = None

        self._supervisor: Optional[threading.Thread] = None
        self._reader: Optional[threading.Thread] = None
        self._heartbeat: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._supervisor is not None and self._supervisor.is_alive():
            return
        self._stop.clear()
        self._supervisor = threading.Thread(
            target=self._supervise, name="gateway-supervisor", daemon=True
        )
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop, name="gateway-heartbeat", daemon=True
        )
        self._supervisor.start()
        self._heartbeat.start()
        self._log.info(
            "connecting to %s:%s", self.config.host, self.config.port
        )

    def stop(self) -> None:
        self._stop.set()
        self._close_socket()
        for thread in (self._supervisor, self._reader, self._heartbeat):
            if thread is not None:
                thread.join(timeout=5.0)
        self._session_ready.clear()
        self._fail_pending("NOT_CONNECTED", "AI_LINK_OFFLINE")

    close = stop

    @property
    def connected(self) -> bool:
        return self._session_ready.is_set()

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    def restart_session(self) -> None:
        """Drop the current session; the supervisor reconnects and sequence resets."""
        if self._session_ready.is_set():
            self._on_session_lost("operator reset")
        else:
            self._next_sequence = 1

    # -- requests ---------------------------------------------------------

    def request_green(
        self, movement: str, green_time: int, timeout: Optional[float] = None
    ) -> RequestResult:
        """Send one TRAFFIC_REQUEST and wait for its reply."""
        if movement not in self._movements:
            return RequestResult(
                False,
                "INVALID_FIELDS",
                reason=f"movement {movement!r} not in {self._movements}",
            )
        if (
            not isinstance(green_time, int)
            or isinstance(green_time, bool)
            or not GREEN_MIN <= green_time <= GREEN_MAX
        ):
            return RequestResult(
                False,
                "INVALID_FIELDS",
                reason=f"green_time {green_time!r} outside {GREEN_MIN}-{GREEN_MAX}",
            )
        return self._request(
            "traffic", {"green_time": green_time}, movement=movement, timeout=timeout
        )

    def request_status(self, timeout: Optional[float] = None) -> RequestResult:
        return self._request("status", status_request_message(), timeout=timeout)

    def _request(
        self,
        kind: str,
        message: dict,
        *,
        movement: str = "",
        timeout: Optional[float] = None,
    ) -> RequestResult:
        if not self._session_ready.is_set():
            return RequestResult(False, "NOT_CONNECTED", reason="AI_LINK_OFFLINE")

        now = time.monotonic()
        if now < self._backoff_until:
            return RequestResult(
                False,
                "BACKOFF",
                reason=f"backing off for {self._backoff_until - now:.1f}s",
            )

        if not self._request_lock.acquire(blocking=False):
            return RequestResult(False, "BUSY", reason="request already in flight")

        try:
            if not self._session_ready.is_set():
                return RequestResult(
                    False, "NOT_CONNECTED", reason="AI_LINK_OFFLINE"
                )

            pending = _Pending(kind=kind)
            if kind == "traffic":
                sequence = self._next_sequence
                self._next_sequence += 1
                message = traffic_request_message(
                    movement,
                    message["green_time"],
                    sequence,
                    movements=self._movements,
                )
                pending.sequence = sequence

            with self._pending_lock:
                self._pending = pending

            started = time.monotonic()
            try:
                self._send(message)
            except OSError as exc:
                self._log.warning("send failed: %s", exc)
                self._on_session_lost("send failed")
                return RequestResult(
                    False, "NOT_CONNECTED", reason="AI_LINK_OFFLINE",
                    sequence=pending.sequence,
                )

            wait_for = timeout if timeout is not None else self.config.request_timeout_s
            if not pending.event.wait(wait_for):
                self._backoff_until = time.monotonic() + self.config.request_backoff_s
                return RequestResult(
                    False,
                    "TIMEOUT",
                    sequence=pending.sequence,
                    reason="RESPONSE_TIMEOUT",
                    latency_s=round(time.monotonic() - started, 3),
                )

            latency = round(time.monotonic() - started, 3)
            reply = pending.message or {}
            return self._parse_reply(pending, reply, latency)
        finally:
            with self._pending_lock:
                self._pending = None
            self._request_lock.release()

    def _parse_reply(
        self, pending: _Pending, reply: dict, latency: float
    ) -> RequestResult:
        if is_accepted_ack(reply):
            self._log.info(
                "controller accepted %s (sequence %s, uno_ack %s)",
                reply.get("sequence"),
                reply.get("sequence"),
                reply.get("uno_ack"),
            )
            return RequestResult(
                True,
                "CONTROLLER_ACCEPTED",
                sequence=reply.get("sequence", pending.sequence),
                uno_ack=reply.get("uno_ack"),
                latency_s=latency,
                raw=reply,
            )

        if reply.get("type") == "ERROR":
            reason = str(reply.get("reason", "UNKNOWN"))
            self._log.warning("gateway rejected request: %s", reason)
            if reason in (
                "UNO_ACK_TIMEOUT",
                "UNO_NACK",
                "OUT_OF_POLICY",
                "AI_LINK_OFFLINE",
            ):
                self._backoff_until = (
                    time.monotonic() + self.config.request_backoff_s
                )
            return RequestResult(
                False,
                reason,
                sequence=pending.sequence,
                reason=reason,
                latency_s=latency,
                raw=reply,
            )

        if reply.get("type") == "UNO_STATUS":
            return RequestResult(
                True,
                "UNO_STATUS",
                latency_s=latency,
                raw=reply,
            )

        return RequestResult(
            False,
            "UNEXPECTED_REPLY",
            sequence=pending.sequence,
            reason=f"unexpected reply type {reply.get('type')!r}",
            latency_s=latency,
            raw=reply,
        )

    # -- connection management -------------------------------------------

    def _supervise(self) -> None:
        delay = self.config.reconnect_min_s
        while not self._stop.is_set():
            try:
                sock = socket.create_connection(
                    (self.config.host, self.config.port),
                    timeout=self.config.connect_timeout_s,
                )
            except OSError as exc:
                self._log.warning("connect failed: %s", exc)
                if self._stop.wait(delay):
                    break
                delay = min(delay * 2, self.config.reconnect_max_s)
                continue

            sock.settimeout(0.5)
            self._socket = sock
            self._reader = threading.Thread(
                target=self._read_loop, args=(sock,), name="gateway-reader", daemon=True
            )
            self._reader.start()

            if not self._handshake():
                self._log.warning("handshake failed; retrying")
                self._teardown_session()
                if self._stop.wait(delay):
                    break
                delay = min(delay * 2, self.config.reconnect_max_s)
                continue

            delay = self.config.reconnect_min_s
            self._next_sequence = 1
            self._session_ready.set()
            self._log.info(
                "session established; sequence reset to 1 (heartbeat acked)"
            )

            while not self._stop.is_set() and self._session_ready.is_set():
                self._stop.wait(0.1)

            self._teardown_session()
            if self._stop.is_set():
                break
            self._log.warning("session lost; reconnecting in %.1fs", delay)
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self.config.reconnect_max_s)

    def _handshake(self) -> bool:
        self._heartbeat_ack.clear()
        try:
            self._send(heartbeat_message())
        except OSError as exc:
            self._log.warning("handshake send failed: %s", exc)
            return False
        return self._heartbeat_ack.wait(self.config.handshake_timeout_s)

    def _read_loop(self, sock: socket.socket) -> None:
        buffer = LineBuffer()
        while not self._stop.is_set():
            try:
                chunk = sock.recv(self.config.recv_chunk)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            for line in buffer.feed(chunk):
                # Record the line before parsing so malformed replies remain
                # visible instead of disappearing into a debug log.
                self._record("RX", line)
                try:
                    message = decode_message(line)
                except ProtocolError as exc:
                    self._log.warning("dropping malformed line: %s", exc)
                    continue
                self._dispatch(message)

        if not self._stop.is_set():
            self._on_session_lost("socket closed")
        if buffer.dropped:
            self._log.warning("dropped %d oversized line(s)", buffer.dropped)

    def _dispatch(self, message: dict) -> None:
        if is_heartbeat_ack(message):
            self._heartbeat_ack.set()
            return

        if is_accepted_ack(message):
            self._resolve_pending(message, expected_kind="traffic")
            return

        if message.get("type") == "UNO_STATUS":
            self._resolve_pending(message, expected_kind="status")
            return

        if message.get("type") == "ERROR":
            with self._pending_lock:
                pending = self._pending
                if pending is not None:
                    pending.message = message
                    pending.event.set()
            return

        self._log.debug("ignoring reply: %s", message)

    def _resolve_pending(self, message: dict, *, expected_kind: str) -> None:
        with self._pending_lock:
            pending = self._pending
            if pending is None or pending.kind != expected_kind:
                self._log.warning(
                    "unexpected %s while %s in flight",
                    message.get("type"),
                    pending.kind if pending else "nothing",
                )
                return
            if pending.sequence is not None:
                sequence = message.get("sequence")
                if sequence is not None and sequence != pending.sequence:
                    self._log.warning(
                        "reply sequence %s does not match pending %s",
                        sequence,
                        pending.sequence,
                    )
                    return
            pending.message = message
            pending.event.set()

    def _on_session_lost(self, reason: str) -> None:
        if self._session_ready.is_set():
            self._log.warning("session lost: %s", reason)
        self._session_ready.clear()
        self._close_socket()
        self._fail_pending("NOT_CONNECTED", "AI_LINK_OFFLINE")

    def _teardown_session(self) -> None:
        self._session_ready.clear()
        self._close_socket()

    def _close_socket(self) -> None:
        with self._session_lock:
            sock = self._socket
            self._socket = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _fail_pending(self, status: str, reason: str) -> None:
        with self._pending_lock:
            pending = self._pending
            if pending is None or pending.event.is_set():
                return
            pending.message = {"type": "ERROR", "reason": reason, "_local": status}
            pending.event.set()

    def _send(self, message: dict) -> None:
        payload = encode_message(message)
        with self._send_lock:
            sock = self._socket
            if sock is None:
                raise OSError("not connected")
            sock.sendall(payload)
        self._record("TX", payload.decode("utf-8").rstrip("\r\n"))

    def _record(self, direction: str, line: str) -> None:
        entry = self.wire.record(direction, line)
        listener = self._wire_listener
        if listener is None:
            return
        try:
            listener(entry)
        except Exception:  # noqa: BLE001 - a display hook must not kill the link
            self._log.exception("wire transcript listener failed")

    # -- heartbeat --------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.config.heartbeat_interval_s):
            if not self._session_ready.is_set():
                continue
            try:
                gate_open = bool(self._health_check())
            except Exception:  # noqa: BLE001 - health probe must not kill the loop
                self._log.exception("health check raised; closing heartbeat gate")
                gate_open = False

            if gate_open != self._gate_open:
                self._gate_open = gate_open
                self._log.info(
                    "heartbeat gate %s", "OPEN" if gate_open else "CLOSED"
                )
            if not gate_open:
                continue

            try:
                self._send(heartbeat_message())
            except OSError as exc:
                self._log.warning("heartbeat send failed: %s", exc)
                self._on_session_lost("heartbeat send failed")


# Re-exported so callers do not need to import protocol separately.
__all__ = [
    "GatewayClient",
    "GatewayConfig",
    "RequestResult",
    "ack_heartbeat",
]
