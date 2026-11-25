# Decision engine

Pure, deterministic logic in `decision/`. It reads per-approach metrics and
returns at most one **advisory** recommendation for a **serving unit**. It never
changes a phase, never sends emergency priority (`priority` is always `NORMAL`)
and never sends a red duration or a phase order.

All thresholds below are **starting values for tuning on real footage**, not
calibrated constants.

## Assumption: one approach green at a time

The model assumes exactly one approach is green at a time. The Uno owns phase
order, yellow and all-red; the host only recommends which unit to serve next
and for how long. If the real junction needs two simultaneous greens, this
engine and the proposed firmware design do not cover it.

## Serving units

A **unit** is what green is allocated to: one or more approaches. Units come
from `decision.units`; the engine never hard-codes an approach or a wire name.

```yaml
# Compat layout (current firmware): two units, sides merged.
decision:
  units:
    - {id: MAIN,      approaches: [MAIN_A, MAIN_B]}
    - {id: SECONDARY, approaches: [SEC]}
  aggregation: sum

# Three-phase layout (target; needs new firmware).
decision:
  units:
    - {id: MAIN_A, approaches: [MAIN_A]}
    - {id: MAIN_B, approaches: [MAIN_B]}
    - {id: SEC,    approaches: [SEC]}
  aggregation: sum
```

Rules enforced at startup:

- unit ids are unique, and an approach belongs to exactly one unit;
- every camera approach is in a unit, and every unit approach has a camera;
- compat mode accepts only the units `MAIN`/`SECONDARY`;
- three-phase mode requires exactly `MAIN_A`/`MAIN_B`/`SEC`, is disabled by
  default, and needs explicit movement names.

`aggregation: sum` or `max` applies only when a unit has more than one
approach; waiting is always the unit maximum.

## Inputs

`ApproachMetrics` at ~1 Hz per approach:

- `vehicle_count_in_region`, `stationary_count`
- `crossings_last_window`
- `congestion_level` (Low / Moderate / High)
- `mean_wait_s`, `max_wait_s` (how long tracked vehicles have been stationary)
- `frame_age_s`, `healthy`, `timestamp`

Per-approach metrics are logged whichever layout is active.

## Health policy (default)

An approach is unhealthy when its camera is not `LIVE`, its snapshot is older
than `max_metrics_age_s` (2 s), or its pipeline has stalled. **If any approach is
unhealthy the engine makes no recommendation at all**, and the heartbeat gate
closes too, so the Uno runs its autonomous cycle. Missing data is never treated
as zero traffic.

A unit is **never scored from a subset of its members**. Even with
`require_all_approaches_healthy: false` (an alternative, not the default), a
unit with one blind member is skipped entirely and named in the reasons.

## Scoring

```text
score = w_queue      * vehicles_in_region
      + w_stationary * stationary_count
      + w_wait       * max_wait_s
      + w_cong       * congestion_weight      # Low 0, Moderate 1, High 2
```

Defaults: `w_queue=1.0`, `w_stationary=1.5`, `w_wait=0.1`, `w_cong=1.0`.
Counts and congestion are combined across a unit's members with `aggregation`;
waiting uses the unit maximum.

Metrics are averaged over `smoothing_window_s` (default 5 s) before scoring; the
wait signal uses the maximum in the window so one long waiter is not averaged
away.

## Green time

```text
normalized = min(1, score / score_saturation)      # saturation default 10
green      = clamp(round(base + k * normalized), min_green_s, max_green_s)
```

Defaults: `base=8`, `k=10`. The clamp is hard-limited to the protocol's 5–30 s
range, and config may narrow it (e.g. min 8, max 25).

## Red is derived, never requested

The protocol has no red-duration or order field. The engine estimates red so it
can reason about fairness, and logs the estimate each cycle:

```text
expected_green(unit) = last recommended green for that unit, if younger than
                       green_memory_s
                     = round(base_green_s) otherwise
expected_red(unit)   = sum(expected_green(other) for other != unit)
                     + (number of other units) * (yellow_s + all_red_s)
```

Example with `base=8`, `yellow=3`, `all_red=1`, and no history: in the two-unit
layout both units expect 12 s of red. After recommending 18 s to `MAIN`,
`SECONDARY` expects `18 + 4 = 22 s` while `MAIN` still expects `8 + 4 = 12 s`.

`yellow_s` (3) and `all_red_s` (1) are **placeholders** matching the controller
README; they are likely to differ in final firmware. The Uno owns the real
clearance and phase order, so this model shapes red only indirectly, by
choosing which unit to favour and for how long.

## Fairness: starvation guard

Each unit remembers when it was last recommended. A unit with demand (score at
or above the idle threshold) is forced to the top when either:

- it has not been recommended for more than `max_wait_before_force_s`
  (default 60 s), or
- its estimated red exceeds its `max_red_s` cap.

`max_red_s` is a number for every unit or a per-unit mapping:

```yaml
decision:
  max_red_s: 90              # all units
  # max_red_s: {MAIN: 90, SECONDARY: 120}
```

Ties go to the unit that was not just served, so one busy approach cannot
monopolise the junction. It is still a recommendation: the Uno applies it at
its next phase change, subject to the 5–30 s limits.

## Hysteresis

A request is emitted only when **all** of these hold:

- at least `min_interval_s` (default 8 s) since the previous request;
- the unit changed, or green time differs by more than `min_delta_s`
  (default 3 s);
- the new recommendation is not idle.

This matters because the Uno stores a request as *pending* and applies it at the
next phase entry — flooding it with requests achieves nothing.

## Idle

If every unit's score is below `idle_score_threshold` (default 1.0), nothing is
emitted and the Uno keeps its default cycle.

## Output

`Recommendation(unit_id, green_time, priority="NORMAL", reasons, scores,
expected_red_s)`. `reasons` explains the scores, the rule that fired, clamping
and any suppression. The orchestrator translates `unit_id` to a wire movement
and writes both to the JSONL log alongside the gateway reply.

## Observing the wire without changing it

The exact TX/RX transcript is outside the decision path. `GatewayClient` records
complete lines in a bounded, thread-safe memory buffer (newest 1,000); the CLI
`--show-wire` flag prints them, and **Controls → Wire traffic…** displays the
same lines in the console. RX is captured before JSON parsing, so malformed or
rejected replies are visible as the gateway sent them.

The transcript is not written to `decisions.jsonl` and does not alter scoring,
hysteresis, fairness, health gating, priority or what is sent. It is not lamp
state: `CONTROLLER_ACCEPTED` still means only that the request was accepted, not
that a phase changed.

## Wire modes (the protocol edge)

The engine does not know `MAIN_A`, `MAIN_B`, `SEC`, `SECONDARY` or any other
wire name. `gateway/adapter.py` maps units to movements:

| `gateway.mode` | Units | Wire movement | Current firmware |
| --- | --- | --- | --- |
| `compat_two_phase` (default) | `MAIN`, `SECONDARY` | same names | yes |
| `three_phase` (opt-in) | `MAIN_A`, `MAIN_B`, `SEC` | `MAIN_A`, `MAIN_B`, `SECONDARY` (**proposed**) | **no** |

Three-phase needs `gateway.three_phase.enabled: true` and explicit movement
names, and logs a prominent warning at startup. The proposal for the firmware
owners is in `docs/FIRMWARE_THREE_PHASE_PROPOSAL.md`; it is not current
behaviour.
