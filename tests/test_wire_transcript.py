"""Exact wire-transcript tests against the mock gateway and CLI printer."""

from __future__ import annotations

from types import SimpleNamespace

from app.config import build_config
from app.controller import AppController
from gateway.client import GatewayClient, GatewayConfig
from gateway.mock_gateway import MockGateway
from gateway.transcript import WireEntry, WireTranscript
from main import build_parser, print_wire_entry
from tests.conftest import wait_until


def _config(mock: MockGateway) -> GatewayConfig:
    return GatewayConfig(
        host="127.0.0.1",
        port=mock.port,
        connect_timeout_s=0.5,
        handshake_timeout_s=1.0,
        request_timeout_s=1.0,
        heartbeat_interval_s=0.1,
        reconnect_min_s=0.05,
        reconnect_max_s=0.2,
        request_backoff_s=0.05,
    )


def test_transcript_is_bounded_and_keeps_order():
    transcript = WireTranscript(capacity=2)
    first = transcript.record("TX", '{"type":"HEARTBEAT"}')
    transcript.record("RX", '{"type":"GATEWAY_ACK","status":"HEARTBEAT_OK"}')
    transcript.record("TX", '{"type":"STATUS_REQUEST"}')

    entries = transcript.entries()
    assert len(entries) == 2
    assert entries[0].sequence == first.sequence + 1
    assert entries[-1].json == '{"type":"STATUS_REQUEST"}'
    assert len(transcript) == 2


def test_client_transcript_contains_exact_request_and_gateway_reply():
    with MockGateway(port=0) as gateway:
        client = GatewayClient(_config(gateway), lambda: True)
        client.start()
        try:
            assert wait_until(lambda: client.connected, timeout_s=3.0)
            result = client.request_green("SECONDARY", 12)
            assert result.ok, result

            entries = client.wire.entries()
            sent = [entry.json for entry in entries if entry.direction == "TX"]
            received = [entry.json for entry in entries if entry.direction == "RX"]

            assert (
                '{"type":"TRAFFIC_REQUEST","movement":"SECONDARY",'
                '"green_time":12,"priority":"NORMAL","sequence":1}'
            ) in sent
            assert (
                '{"type":"GATEWAY_ACK","status":"CONTROLLER_ACCEPTED",'
                '"sequence":1,"uno_ack":"ACK:REQUEST_SIDE,12"}'
            ) in received
            assert '{"type":"HEARTBEAT"}' in sent
        finally:
            client.stop()


def test_listener_sees_entries_but_cannot_break_the_link():
    seen: list[WireEntry] = []

    def broken_listener(entry: WireEntry) -> None:
        seen.append(entry)
        raise RuntimeError("display hook failed")

    with MockGateway(port=0) as gateway:
        client = GatewayClient(
            _config(gateway), lambda: True, wire_listener=broken_listener
        )
        client.start()
        try:
            assert wait_until(lambda: client.connected, timeout_s=3.0)
            result = client.request_green("MAIN", 10)
            assert result.ok, result
            assert seen
            assert seen[0].json == '{"type":"HEARTBEAT"}'
        finally:
            client.stop()


def test_controller_copies_wire_entries_for_the_ui():
    config = build_config(
        {
            "cameras": [
                {
                    "id": "cam_main_a",
                    "approach": "MAIN_A",
                    "source": "file",
                    "path": "missing.avi",
                }
            ],
            "decision": {
                "units": [{"id": "MAIN", "approaches": ["MAIN_A"]}]
            },
            "logging": {"decision_log": ""},
        }
    )
    controller = AppController(config, dry_run=True)
    transcript = WireTranscript()
    transcript.record("TX", '{"type":"HEARTBEAT"}')
    transcript.record("RX", '{"type":"GATEWAY_ACK","status":"HEARTBEAT_OK"}')
    controller.orchestrator.gateway = SimpleNamespace(wire=transcript)
    try:
        lines = controller.wire_transcript()
        assert [(line.direction, line.json) for line in lines] == [
            ("TX", '{"type":"HEARTBEAT"}'),
            ("RX", '{"type":"GATEWAY_ACK","status":"HEARTBEAT_OK"}'),
        ]
    finally:
        controller.shutdown()


def test_cli_has_show_wire_and_prints_the_json_unchanged(capsys):
    assert build_parser().parse_args(["--show-wire"]).show_wire is True

    print_wire_entry(
        WireEntry(
            sequence=1,
            at="2026-11-22T00:00:00.000Z",
            direction="TX",
            json='{"type":"TRAFFIC_REQUEST","movement":"MAIN"}',
        )
    )
    assert capsys.readouterr().out == (
        'TX {"type":"TRAFFIC_REQUEST","movement":"MAIN"}\n'
    )
