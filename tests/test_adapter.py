"""Unit -> wire adapter: modes, mapping and exact protocol output."""

from __future__ import annotations

import pytest

from decision.models import Recommendation
from gateway.adapter import (
    COMPAT_MODE,
    THREE_PHASE_MODE,
    ProtocolAdapter,
)
from gateway.client import GatewayClient, GatewayConfig
from gateway.mock_gateway import MockGateway
from gateway.protocol import MAX_LINE_BYTES, ProtocolError, encode_message
from tests.conftest import wait_until


def recommendation(unit_id: str = "MAIN", green: int = 12) -> Recommendation:
    return Recommendation(
        unit_id=unit_id,
        green_time=green,
        reasons=("test",),
        scores={unit_id: 4.0},
    )


def compat_adapter() -> ProtocolAdapter:
    return ProtocolAdapter(
        COMPAT_MODE,
        {"MAIN": "MAIN", "SECONDARY": "SECONDARY"},
        ["MAIN", "SECONDARY"],
    )


def three_phase_adapter() -> ProtocolAdapter:
    return ProtocolAdapter(
        THREE_PHASE_MODE,
        {"MAIN_A": "MAIN_A", "MAIN_B": "MAIN_B", "SEC": "SECONDARY"},
        ["MAIN_A", "MAIN_B", "SEC"],
        three_phase_enabled=True,
    )


def test_compat_adapter_maps_units_to_the_current_movements():
    adapter = compat_adapter()
    wire = adapter.to_wire(recommendation("SECONDARY", 15))

    assert (wire.unit_id, wire.movement, wire.green_time) == (
        "SECONDARY",
        "SECONDARY",
        15,
    )
    assert wire.priority == "NORMAL"
    assert adapter.wire_movements == ("MAIN", "SECONDARY")
    assert adapter.startup_warnings() == ()


def test_three_phase_adapter_uses_the_proposed_names():
    adapter = three_phase_adapter()

    assert adapter.to_wire(recommendation("MAIN_B", 20)).movement == "MAIN_B"
    assert adapter.to_wire(recommendation("SEC", 20)).movement == "SECONDARY"
    assert adapter.wire_movements == ("MAIN_A", "MAIN_B", "SECONDARY")
    assert any("NOT implemented" in warning for warning in adapter.startup_warnings())


def test_three_phase_requires_the_explicit_opt_in():
    with pytest.raises(ValueError, match="disabled"):
        ProtocolAdapter(
            THREE_PHASE_MODE,
            {"MAIN_A": "MAIN_A", "MAIN_B": "MAIN_B", "SEC": "SECONDARY"},
            ["MAIN_A", "MAIN_B", "SEC"],
            three_phase_enabled=False,
        )


def test_compat_adapter_refuses_non_firmware_movements():
    with pytest.raises(ValueError, match="cannot send"):
        ProtocolAdapter(
            COMPAT_MODE,
            {"MAIN": "MAIN", "SEC": "SIDE"},
            ["MAIN", "SEC"],
        )


def test_adapter_requires_a_movement_for_every_unit():
    with pytest.raises(ValueError, match="no wire movement"):
        ProtocolAdapter(COMPAT_MODE, {"MAIN": "MAIN"}, ["MAIN", "SECONDARY"])


def test_adapter_rejects_duplicate_wire_movements():
    with pytest.raises(ValueError, match="unique"):
        ProtocolAdapter(
            THREE_PHASE_MODE,
            {"MAIN_A": "MAIN_A", "MAIN_B": "SIDE", "SEC": "SIDE"},
            ["MAIN_A", "MAIN_B", "SEC"],
            three_phase_enabled=True,
        )


def test_unknown_unit_is_a_protocol_error():
    with pytest.raises(ProtocolError):
        compat_adapter().to_wire(recommendation("SEC"))


def test_build_message_is_compact_newline_terminated_and_within_the_limit():
    adapter = three_phase_adapter()
    message = adapter.build_message(recommendation("SEC", 15), sequence=104)
    payload = encode_message(message)

    assert message == {
        "type": "TRAFFIC_REQUEST",
        "movement": "SECONDARY",
        "green_time": 15,
        "priority": "NORMAL",
        "sequence": 104,
    }
    assert payload == (
        b'{"type":"TRAFFIC_REQUEST","movement":"SECONDARY","green_time":15,'
        b'"priority":"NORMAL","sequence":104}\n'
    )
    assert b" " not in payload
    assert len(payload) <= MAX_LINE_BYTES


def test_compat_wire_goldens_keep_sec_on_the_secondary_group():
    adapter = compat_adapter()

    secondary = adapter.build_message(recommendation("SECONDARY", 15), 104)
    main = adapter.build_message(recommendation("MAIN", 15), 105)
    assert encode_message(secondary) == (
        b'{"type":"TRAFFIC_REQUEST","movement":"SECONDARY","green_time":15,'
        b'"priority":"NORMAL","sequence":104}\n'
    )
    assert encode_message(main) == (
        b'{"type":"TRAFFIC_REQUEST","movement":"MAIN","green_time":15,'
        b'"priority":"NORMAL","sequence":105}\n'
    )


def test_adapter_refuses_green_time_outside_the_protocol():
    with pytest.raises(ProtocolError):
        compat_adapter().to_wire(recommendation("MAIN", 45))
    with pytest.raises(ProtocolError):
        compat_adapter().to_wire(recommendation("MAIN", 4))


def test_compat_mapping_reaches_the_mock_gateway_end_to_end():
    with MockGateway(port=0) as gateway:
        adapter = compat_adapter()
        client = GatewayClient(
            GatewayConfig(
                host="127.0.0.1",
                port=gateway.port,
                connect_timeout_s=0.5,
                handshake_timeout_s=1.0,
                request_timeout_s=1.0,
                heartbeat_interval_s=0.05,
                reconnect_min_s=0.05,
                reconnect_max_s=0.2,
                request_backoff_s=0.05,
            ),
            lambda: True,
            movements=adapter.wire_movements,
        )
        client.start()
        try:
            assert wait_until(lambda: client.connected, timeout_s=3.0)
            for unit_id, green in (("SECONDARY", 12), ("MAIN", 13)):
                wire = adapter.to_wire(recommendation(unit_id, green))
                result = client.request_green(wire.movement, wire.green_time)
                assert result.ok, result

            requests = gateway.traffic_requests
            assert [(item["movement"], item["green_time"]) for item in requests] == [
                ("SECONDARY", 12),
                ("MAIN", 13),
            ]
        finally:
            client.stop()
