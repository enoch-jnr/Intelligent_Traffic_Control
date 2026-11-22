"""A faithful stand-in for the ESP32-S3 gateway.

Imitates the parts of the firmware that the AI host can observe:

* one TCP client at a time;
* newline-delimited JSON, 512-byte line limit, discard-to-newline;
* heartbeat freshness (default 3 s) with ``AI_LINK_OFFLINE`` otherwise;
* strict field validation, 5–30 s green range;
* strictly increasing sequence per session, updated only on acceptance;
* sequence reset and no valid heartbeat on a new session;
* optional random ``UNO_NACK`` / ``UNO_ACK_TIMEOUT`` injection.

Run it with ``python -m gateway.mock_gateway``.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from gateway.protocol import (
    MOVEMENTS,
    LineBuffer,
    ProtocolError,
    ack_accepted,
    ack_heartbeat,
    ack_status,
    decode_message,
    encode_message,
    error_message,
    traffic_request_error,
    uno_ack_name,
)

_log = logging.getLogger("gateway.mock")

STATUS_DETAIL = (
    "STATUS:STATE=GREEN,MODE=AUTO,REMAINING=12,"
    "PENDING_MAIN=0,PENDING_SIDE=0"
)


@dataclass
class _Session:
    ident: int
    last_heartbeat_at: Optional[float] = None
    last_accepted_sequence: int = 0


@dataclass
class _Stats:
    heartbeats: int = 0
    heartbeat_times: list[float] = field(default_factory=list)
    traffic_requests: list[dict] = field(default_factory=list)
    errors_sent: list[str] = field(default_factory=list)


class MockGateway:
    """Threaded mock of the ESP32 gateway protocol."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3333,
        *,
        heartbeat_freshness_s: float = 3.0,
        nack_rate: float = 0.0,
        timeout_rate: float = 0.0,
        seed: Optional[int] = None,
        movements: Optional[list[str]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.heartbeat_freshness_s = heartbeat_freshness_s
        self.nack_rate = nack_rate
        self.timeout_rate = timeout_rate
        self.movements: tuple[str, ...] = tuple(movements or MOVEMENTS)
        self._random = random.Random(seed)

        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._client: Optional[socket.socket] = None
        self._client_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stats = _Stats()
        self._session_counter = 0
        self._sessions: list[_Session] = []

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._server is not None:
            return
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(5)
        server.settimeout(0.2)
        self._server = server
        self.port = server.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._accept_loop, name="mock-gateway", daemon=True
        )
        self._thread.start()
        _log.info("mock gateway listening on %s:%s", self.host, self.port)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except OSError:
                pass
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        for thread in (self._thread, self._client_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)

    def serve_forever(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(0.25)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def __enter__(self) -> "MockGateway":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # -- introspection (tests) -------------------------------------------

    @property
    def traffic_requests(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._stats.traffic_requests]

    @property
    def heartbeats(self) -> int:
        with self._lock:
            return self._stats.heartbeats

    @property
    def heartbeat_times(self) -> list[float]:
        """Monotonic arrival times, for measuring heartbeat regularity."""
        with self._lock:
            return list(self._stats.heartbeat_times)

    @property
    def errors_sent(self) -> list[str]:
        with self._lock:
            return list(self._stats.errors_sent)

    @property
    def sessions(self) -> int:
        with self._lock:
            return len(self._sessions)

    # -- server internals -------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            server = self._server
            if server is None:
                break
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            with self._lock:
                busy = self._client is not None
                if not busy:
                    self._client = conn
                    self._session_counter += 1
                    session = _Session(ident=self._session_counter)
                    self._sessions.append(session)

            if busy:
                _log.warning("second client refused (single-client gateway)")
                try:
                    conn.close()
                except OSError:
                    pass
                continue

            self._client_thread = threading.Thread(
                target=self._handle_client,
                args=(conn, session),
                name=f"mock-client-{session.ident}",
                daemon=True,
            )
            self._client_thread.start()

    def _handle_client(self, conn: socket.socket, session: _Session) -> None:
        conn.settimeout(0.2)
        buffer = LineBuffer()
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                for line in buffer.feed(chunk):
                    self._handle_line(conn, session, line)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                if self._client is conn:
                    self._client = None

    def _handle_line(self, conn: socket.socket, session: _Session, line: str) -> None:
        try:
            message = decode_message(line)
        except ProtocolError as exc:
            _log.warning("invalid line: %s", exc)
            self._reply(conn, error_message("INVALID_FIELDS"))
            return

        message_type = message.get("type")
        if message_type == "HEARTBEAT":
            session.last_heartbeat_at = time.monotonic()
            with self._lock:
                self._stats.heartbeats += 1
                self._stats.heartbeat_times.append(session.last_heartbeat_at)
            self._reply(conn, ack_heartbeat())
            return

        if message_type == "STATUS_REQUEST":
            if not self._heartbeat_fresh(session):
                self._fail(conn, "AI_LINK_OFFLINE")
                return
            self._reply(conn, ack_status(STATUS_DETAIL))
            return

        if message_type == "TRAFFIC_REQUEST":
            self._handle_traffic_request(conn, session, message)
            return

        _log.warning("unknown message type %r", message_type)
        self._fail(conn, "INVALID_FIELDS")

    def _handle_traffic_request(
        self, conn: socket.socket, session: _Session, message: dict
    ) -> None:
        reason = traffic_request_error(message, self.movements)
        if reason is not None:
            self._fail(conn, reason)
            return

        if not self._heartbeat_fresh(session):
            self._fail(conn, "AI_LINK_OFFLINE")
            return

        sequence = message["sequence"]
        if sequence <= session.last_accepted_sequence:
            self._fail(conn, "STALE_SEQUENCE")
            return

        if self._random.random() < self.timeout_rate:
            self._fail(conn, "UNO_ACK_TIMEOUT")
            return
        if self._random.random() < self.nack_rate:
            self._fail(conn, "UNO_NACK")
            return

        session.last_accepted_sequence = sequence
        movement = message["movement"]
        green_time = message["green_time"]
        with self._lock:
            self._stats.traffic_requests.append(
                {
                    "session": session.ident,
                    "sequence": sequence,
                    "movement": movement,
                    "green_time": green_time,
                    "priority": message.get("priority"),
                }
            )
        _log.info(
            "accepted session=%s sequence=%s movement=%s green=%ss",
            session.ident,
            sequence,
            movement,
            green_time,
        )
        self._reply(
            conn,
            ack_accepted(
                sequence, f"ACK:REQUEST_{uno_ack_name(movement)},{green_time}"
            ),
        )

    def _heartbeat_fresh(self, session: _Session) -> bool:
        if session.last_heartbeat_at is None:
            return False
        return (
            time.monotonic() - session.last_heartbeat_at
            <= self.heartbeat_freshness_s
        )

    def _reply(self, conn: socket.socket, message: dict) -> None:
        try:
            conn.sendall(encode_message(message))
        except OSError:
            pass

    def _fail(self, conn: socket.socket, reason: str) -> None:
        with self._lock:
            self._stats.errors_sent.append(reason)
        _log.info("rejecting request: %s", reason)
        self._reply(conn, error_message(reason))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Mock Smarti ESP32 gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3333)
    parser.add_argument(
        "--heartbeat-freshness",
        type=float,
        default=3.0,
        help="seconds before a heartbeat is considered stale",
    )
    parser.add_argument("--nack-rate", type=float, default=0.0)
    parser.add_argument("--timeout-rate", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--movements",
        default=",".join(MOVEMENTS),
        help=(
            "comma-separated movements to accept. The default mimics the "
            "current firmware. Any other value is HYPOTHETICAL: it lets the "
            "host adapter be tested against a future gateway that does not "
            "exist yet, e.g. MAIN_A,MAIN_B,SECONDARY."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    movements = [item.strip() for item in args.movements.split(",") if item.strip()]
    if not movements:
        parser.error("--movements must name at least one movement")

    gateway = MockGateway(
        args.host,
        args.port,
        heartbeat_freshness_s=args.heartbeat_freshness,
        nack_rate=args.nack_rate,
        timeout_rate=args.timeout_rate,
        seed=args.seed,
        movements=movements,
    )
    gateway.start()
    print(
        json.dumps(
            {
                "mock_gateway": "listening",
                "host": args.host,
                "port": gateway.port,
                "movements": list(gateway.movements),
            }
        ),
        flush=True,
    )
    try:
        while True:
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("stopping", flush=True)
    finally:
        gateway.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
