# Follow-up audit: T-junction, units and the protocol edge

> **Historical audit — not current documentation.** The issues below were the
> starting point for the follow-up. Units, the wire adapter and the exact
> read-only wire transcript have since been implemented; use
> [`README.md`](../README.md) and [`DECISION.md`](DECISION.md) for current
> behaviour. The code references here intentionally describe the old tree.

This is a snapshot of the code **before** the follow-up changes, written by
reading it rather than from the earlier brief. Line references are to the
pre-change tree (commit `449262f`). No behaviour was changed for this phase.

## 1. Where `SEC_A` and `SEC_B` are merged today

The merge is not one function. It is spread across four layers:

| Layer | File / function | What it does |
| --- | --- | --- |
| Config | `app/config.py::build_config` | Reads top-level `approaches: {approach_id: movement}` and only accepts `MAIN`/`SECONDARY`. Both `SEC_A` and `SEC_B` map to `SECONDARY`. |
| Engine | `decision/engine.py::DecisionEngine.__init__` | Builds `self.groups` by inverting that mapping (`movement -> [approach ids]`) and rejects any movement outside `("MAIN", "SECONDARY")`. |
| Engine | `decision/engine.py::_score_group` | Aggregates member metrics into one score for the wire group, using `aggregation: sum|max`; waiting is always the group max. |
| Output | `decision/models.py::Recommendation` | Carries `movement` (a wire name), not an approach or unit, so the wire mapping is baked into the engine's output. |
| Client | `gateway/client.py::request_green` | Validates the movement against `gateway.protocol.MOVEMENTS`. |
| Mock | `gateway/mock_gateway.py::_handle_traffic_request` | Validates through `gateway.protocol.traffic_request_error`, same constant. |
| Logging | `app/orchestrator.py::_build_entry` | Logs `decision.group_scores` keyed by wire movement. |

`phase_mode: two_phase_compat` in `app/config.py` is a separate top-level
string gate that only checks its own value; it is not consulted by the engine,
client or adapter. The actual merging is the `approaches` mapping.

## 2. Current config keys

```yaml
approaches:            # top level: approach id -> wire movement (MAIN/SECONDARY)
  MAIN: MAIN
  SEC_A: SECONDARY
  SEC_B: SECONDARY
phase_mode: two_phase_compat   # only accepted value
decision:
  aggregation: sum|max         # only used when one wire movement has >1 approach
  weights: {...}
  base_green_s, k_green_s, score_saturation
  min_green_s, max_green_s     # inside protocol 5-30
  min_delta_s, min_interval_s
  max_wait_before_force_s
  smoothing_window_s, max_metrics_age_s, idle_score_threshold
  require_all_approaches_healthy
gateway: {host, port, connect_timeout_s, handshake_timeout_s, request_timeout_s,
          heartbeat_interval_s, reconnect_min_s, reconnect_max_s,
          request_backoff_s, recv_chunk}
```

There is no unit concept, no `gateway.mode`, no per-approach unit mapping, no
yellow/all-red/red model.

## 3. What `Recommendation` contains

`Recommendation(movement, green_time, priority="NORMAL", reasons, scores)`,
where `movement` is the **wire** movement `MAIN` or `SECONDARY` and `scores` is
a mapping of wire movement to score. It is per wire group, never per approach.

`Decision(recommendation, reasons, group_scores, healthy)` mirrors that shape;
`group_scores` is keyed by wire movement.

## 4. Tests that encode the old merged behaviour

- `tests/test_decision_engine.py`
  - `APPROACHES` maps `SEC_A`/`SEC_B` to `SECONDARY`; `engine()` builds the
    engine from it.
  - `test_busy_main_gets_the_green`, `test_starvation_guard_forces_waiting_group`,
    `test_no_starvation_without_demand` assert `recommendation.movement`.
  - `test_compat_mode_aggregates_secondary_demand_as_sum` asserts
    `group_scores["SECONDARY"]` for both `sum` and `max`.
  - `test_unknown_wire_movement_is_rejected` asserts the engine rejects
    `{"MAIN": "NORTH"}` — this test disappears with units, replaced by unit
    validation.
- `tests/test_config.py`
  - `test_example_config_loads_with_env_credentials` asserts
    `config.approaches` and `config.phase_mode`.
  - `test_two_cameras_cannot_share_an_approach`, `test_unknown_movement_is_rejected`,
    `test_camera_approach_must_be_mapped`, `test_three_phase_mode_is_not_supported`
    all speak the old vocabulary.
- `tests/test_orchestrator.py::test_three_cameras_to_mock_gateway` asserts every
  request movement is `MAIN`/`SECONDARY` and expects a single compat run.
- `tests/test_gateway_protocol.py` / `test_gateway_client.py` use the global
  `MOVEMENTS` constant implicitly. They remain valid for the current firmware
  and need an extra parameter, not a rewrite.
- `docs/DECISION.md` and `README.md` describe the merged mapping as permanent.

## 5. What was actually tested

**Passing in tests (65 total, `pytest` on Python 3.14.7 / Windows, no GPU):**

- camera layer: three simultaneous file sources, staleness, reconnect/backoff,
  RTSP URL redaction and URL rejection (5)
- per-camera pipeline isolation (1)
- decision engine: scoring, clamping, idle, health suppression, stale metrics,
  degraded policy, hysteresis, starvation, aggregation, smoothing (14)
- gateway protocol: framing (split/merged/oversized/blank), value validation,
  compact JSON, 512-byte limit (17)
- gateway client against the mock: handshake, per-request sequences, sequence
  reset after reconnect, heartbeat gating, NACK/timeout handling, local
  validation, raw-socket framing and single-client refusal (12)
- config: example load with env expansion, validation errors, source factory,
  redaction (13)
- decision log rotation (2)
- end-to-end: three looping synthetic videos -> pipelines -> decisions -> mock
  gateway -> JSONL, with accepted requests and increasing sequences (1)

**Only compiled / never run:**

- Real RTSP cameras, the ESP32-S3 gateway and the Arduino Uno. No hardware was
  touched at any point.
- Real YOLO inference through the full application. The e2e test injects
  `AlternatingDetector`; a CLI smoke run loaded three real YOLO models but the
  first CPU inference per model took ~8.5 s, so a 3 s smoke window ended before
  any pipeline published metrics. That is an environment observation, not a
  passing test.
- The `--display` window, and Windows/Linux dual-homed networking.
- Anything about the actual Uno phase cycle, ACK text for new movements, or
  yellow/all-red timings.

## 6. Things in the earlier work that are now wrong or risky

1. **The engine knows wire names.** `DecisionEngine` validates
   `("MAIN", "SECONDARY")` and returns `Recommendation.movement`, so a third
   approach cannot be scored without editing pure decision logic. This is the
   main thing the follow-up fixes.
2. **The compat merge is hard-wired.** `{SEC_A, SEC_B} -> SECONDARY` exists in
   config, engine, docs and tests as the only layout; `aggregation` is a
   tie-breaker inside that fixed group, not a layout choice.
3. **A merged unit can be scored from partial data.** With
   `require_all_approaches_healthy: false`, `_score_group` silently drops an
   unhealthy member of `SECONDARY` and scores the rest. That understates demand
   and is worse than declaring the unit unusable. The follow-up keeps the
   default suppression and excludes any unit with an unhealthy member even in
   the degraded policy.
4. **Orphan approaches pass validation.** `approaches` is validated against
   cameras, but a mapping entry with no camera is accepted. The engine then
   marks it unhealthy forever, so the host never recommends. Units will be
   validated against the camera list instead.
5. **`phase_mode` is a dead gate.** It is checked for its value but unused,
   while the actual mode is implied by the `approaches` mapping. Replaced by
   `gateway.mode`.
6. **`Recommendation` has no red model.** Starvation is wall-clock only
   (`max_wait_before_force_s`), with no notion of how long a unit will wait
   because of the other units' greens.
7. **Wire validation is a global constant.** `gateway.protocol.MOVEMENTS`,
   `GatewayClient.request_green` and the mock all hard-code the two names, so a
   labelled proposal cannot be exercised end-to-end against a hypothetical
   gateway without parameterising them.
8. **The decision log cannot reconstruct the layout.** It logs per-approach
   metrics and wire-group scores, but not the mode, unit ids, approach->unit
   membership or wire mapping, so a three-phase run could not be audited from
   the log.
9. **Documentation drift.** `docs/DECISION.md` proposed future movements
   `SIDE_A`/`SIDE_B`; the follow-up brief proposes `SECONDARY_A`/`SECONDARY_B`.
   Nothing in the code ever sent either.
10. **No explicit assumption about phases running together.** The existing code
    never sends two movements at once, but no document states the invariant.

## 7. Assumptions carried into the follow-up

- One approach is green at a time (three-phase cycle). The controller owns phase
  order, yellow and all-red.
- `MAIN`, `SEC_A`, `SEC_B` are approaches in the target layout, one camera each.
- The current firmware accepts only `MAIN`/`SECONDARY`; `SECONDARY_A` and
  `SECONDARY_B` are **proposed names**, disabled by default.
- No firmware is edited by this work, and no new wire field or request type is
  added to default behaviour.
