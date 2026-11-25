# Firmware three-phase proposal

> **PROPOSAL — NOT IMPLEMENTED.**
>
> Every name, field, message and timing in this document is a proposal for the
> ESP32-S3 gateway and Arduino Uno owners. **None of it exists in the current
> firmware.** Do not flash anything from this document. The current gateway
> accepts only `MAIN` and `SECONDARY`, and the current Uno drives two
> approaches. The AI host defaults to the existing two-phase compat mode
> precisely because of that.
>
> The host-side counterpart is disabled by default: `gateway.mode:
> three_phase` is refused unless `gateway.three_phase.enabled: true` **and** the
> proposed movement names are written explicitly in config.

## Target layout and invariant

A T junction with three signal heads, one camera per approach:

| Approach | Head | Camera |
| --- | --- | --- |
| `MAIN_A` | main road, arm A | one |
| `MAIN_B` | main road, arm B | one |
| `SEC` | side road | one |

**Invariant: exactly one approach is green at a time.** Yellow and all-red
clearance separate every ordered pair of greens. Nothing in this proposal
allows two approaches to be green together. If the real junction needs a
protected movement that runs with another approach, that is an open question
for the traffic engineer, not something to infer from this document.

The host remains advisory. It chooses **which approach should be served next
and for how long**; the Uno owns phase order, yellow, all-red and the actual
lamp states.

## 1. Gateway (ESP32-S3) — PROPOSAL

### 1.1 Movement validation

Current: `movement` must be `MAIN` or `SECONDARY`; anything else returns
`{"type":"ERROR","reason":"INVALID_FIELDS"}`.

Proposed accepted set for the new gateway:

| Approach | Proposed movement |
| --- | --- |
| `MAIN_A` | `MAIN_A` |
| `MAIN_B` | `MAIN_B` |
| `SEC` | `SECONDARY` |

These three names are **unconfirmed proposals**. They are not in the current
protocol, and the host will not send them unless three-phase mode is explicitly
enabled in config.

Validation rules to keep:

- `green_time` integer 5–30, `priority` exactly `NORMAL`, `sequence` positive
  and strictly increasing per TCP session.
- Unknown movement: keep `INVALID_FIELDS` (or add a distinct
  `UNSUPPORTED_MOVEMENT` reason; see 1.5).
- Reject a request when no heartbeat is younger than 3 s (`AI_LINK_OFFLINE`).

### 1.2 Mapping to Uno UART commands — EXAMPLES ONLY

The current gateway forwards an accepted request to the Uno and returns its
ACK. The exact UART text is the firmware owners' choice. The following is an
**illustrative proposal**, not the current format:

```text
Gateway -> Uno:  REQ MAIN_A 15
Gateway -> Uno:  REQ MAIN_B 18
Gateway -> Uno:  REQ SEC 12
Uno -> Gateway:  ACK:REQUEST_MAIN_A,15
Uno -> Gateway:  ACK:REQUEST_MAIN_B,18
Uno -> Gateway:  ACK:REQUEST_SEC,12
Uno -> Gateway:  NACK:OUT_OF_POLICY
```

The current host only ever sends `TRAFFIC_REQUEST` with `MAIN`/`SECONDARY`; it
never emits these example strings.

### 1.3 Pending request storage (gateway side)

- Keep at most one pending request per approach, newest wins.
- Store the requested green time, not a phase command.
- Do not queue an unbounded history; a duplicate request must not lengthen the
  next green beyond 30 s.
- Clear pending state for an approach once the Uno has applied it.
- A new TCP session invalidates all pending state and resets the sequence
  counter to 1 (current behaviour, keep it).

### 1.4 Applying requests

The gateway does not decide when a green starts. It hands the request to the
Uno, which applies it at that approach's next green entry (see 2.3). The
gateway's `CONTROLLER_ACCEPTED` means "the Uno accepted and stored the
request", never "the lamp changed".

### 1.5 ACK / NACK — PROPOSAL

Keep the current envelope for compatibility:

```json
{"type":"GATEWAY_ACK","status":"CONTROLLER_ACCEPTED","sequence":104,"uno_ack":"ACK:REQUEST_SEC,18"}
```

The host's **Controls → Wire traffic…** window (or `--show-wire`) shows this
envelope exactly as sent and received. Use it when freezing the new ACK text for
the three-phase firmware, and record the exchange in the bench notes.

Proposed additions:

- If the Uno cannot schedule a request it returns
  `{"type":"ERROR","reason":"UNO_NACK"}` plus, optionally, a `detail` string.
- If firmware does not know a movement, prefer
  `{"type":"ERROR","reason":"UNSUPPORTED_MOVEMENT"}` over the generic
  `INVALID_FIELDS`. This is a **new reason string**; current hosts treat
  unknown reasons as failures and back off, so it is safe to add, but it must
  be documented in the host's protocol list before use.
- Do not include lamp states in the ACK unless the Uno can guarantee them; the
  host must never log "green is on" from an ACK.

### 1.6 Protocol version — PROPOSAL, optional

Three approaches are a breaking change for movement validation. Two options:

1. **No version field:** the new gateway simply accepts the larger movement
   set and keeps old messages working. Simplest, but the host cannot detect
   which firmware it is talking to.
2. **Version in the handshake:** the gateway's reply to the first heartbeat
   includes `"protocol": 2` (PROPOSAL). The host logs the version and refuses
   to send three-phase movements to a version-1 gateway. This requires a new
   field in a reply the host already reads; it does not add a host-to-gateway
   field.

Recommendation: option 1 for the first firmware iteration, option 2 before
deployment if more than one firmware version will ever be in the field. The
current host ignores unknown reply fields, so a version field is backward
compatible.

## 2. Uno — PROPOSAL

### 2.1 State machine

Proposed fixed round-robin order: `MAIN_A` → `MAIN_B` → `SEC` → `MAIN_A` …
The order is the Uno's choice; the host does not request an order.

```text
MAIN_A_GREEN --> MAIN_A_YELLOW --> ALL_RED --> MAIN_B_GREEN --> MAIN_B_YELLOW
     ^                                          |
     |                                          v
SEC_GREEN <-- ALL_RED <-- SEC_YELLOW <-- ALL_RED
```

States and timers:

- `*_GREEN`: the approach's current green. Duration = clamped pending request
  if one exists, otherwise the autonomous default. Clamp 5–30 s.
- `*_YELLOW`: `yellow_s` (placeholder 3 s), no new requests applied.
- `ALL_RED`: `all_red_s` (placeholder 1 s), no green.
- A pending request that arrives during its own approach's green waits for the
  next cycle; never extend a green already running beyond the 30 s cap.

### 2.2 Invariants the firmware must enforce

1. At most one `*_GREEN` state at any instant; every transition passes through
   yellow then all-red.
2. Yellow and all-red occur between **every ordered pair** of approaches, not
   only in one direction.
3. A requested green is clamped to 5–30 s and never starts without clearance.
4. If the request is malformed or stale, the Uno keeps its autonomous cycle and
   NACKs; it does not stop the junction.
5. The Uno remains the authority: it may shorten or skip a requested green to
   preserve the cycle, and must say so in the NACK/detail if it does.

### 2.3 Pending requests

- Store one pending green per approach, newest request wins.
- Apply it at that approach's next green entry, then clear it.
- Never apply a request to the approach currently green.
- `MAIN`/`SECONDARY` from a legacy host: keep supporting them. Proposed
  mapping: `SECONDARY` means "serve the side pair"; the Uno alternates
  `MAIN_A`/`MAIN_B` internally under its own cycle so the one-green invariant
  still holds. Confirm this with the traffic engineer: it changes which side
  road gets green, and the legacy host cannot express a preference.

### 2.4 Watchdog loss

Current behaviour: no fresh heartbeat for 3 s ⇒ autonomous cycle. Proposed
extension to three approaches:

- Run a fixed round-robin over `MAIN_A`, `MAIN_B`, `SEC` with default green
  (for example 15 s; placeholder), yellow 3 s, all-red 1 s.
- Clear all pending requests when the link is declared lost, so stale requests
  cannot fire after reconnection.
- On reconnection, resume advisory mode only after a fresh heartbeat; keep the
  autonomous cycle until then.
- Log the transition (ESP32 stack-watermark log is covered in the bench plan).

## 3. Compatibility plan

### 3.1 Coexistence today

| Host | Firmware | Behaviour |
| --- | --- | --- |
| compat_two_phase (default) | current two-approach | Supported today. `MAIN_A` and `MAIN_B` are merged into `MAIN`; `SEC` sends `SECONDARY`. The Uno cannot tell `MAIN_A` from `MAIN_B`. |
| three_phase | current two-approach | **Refused at startup.** Movement names are unknown to the gateway. |
| compat_two_phase | proposed three-approach firmware | Must keep working; `SECONDARY` is treated as the side pair (2.3). |
| three_phase | proposed three-approach firmware | Target state, after the checklist below. |

### 3.2 Checklist before setting `three_phase.enabled: true`

- [ ] New gateway firmware accepts exactly `MAIN_A`, `MAIN_B`,
      `SECONDARY` and still accepts legacy `MAIN`/`SECONDARY`.
- [ ] Uno implements the three-phase cycle with the one-green invariant and
      clearance between every ordered pair.
- [ ] ACK text for the new movements is frozen and documented.
- [ ] Heartbeat expiry (3 s) and autonomous cycle covering all three
      approaches are verified on the bench.
- [ ] Pending-request semantics are confirmed (newest wins, applied at next
      green, cleared on link loss).
- [ ] `docs/BENCH_TEST_PLAN.md` has been executed for compat mode first.
- [ ] Host config has three units and explicit movements, and starts without a
      warning about unsupported movements.
- [ ] A rollback path exists: set `gateway.mode: compat_two_phase`, restart the
      host, and confirm the old firmware still drives the junction.

## 4. Bench test plan for firmware owners (short form)

Full host-side plan: `docs/BENCH_TEST_PLAN.md`. For the firmware bench, with
LEDs and no cameras:

1. Power the Uno and gateway with the host disconnected. Confirm the
   autonomous round-robin covers all three approaches and never shows two
   greens.
2. Send `HEARTBEAT` over TCP and confirm `HEARTBEAT_OK`.
3. Send one request per movement at 15 s. Confirm the ACK, and that the green
   starts only at that approach's next slot with yellow/all-red before it.
4. Send a request during its own green; confirm it is stored for the next
   cycle, not applied immediately.
5. Send `green_time` 4 and 31; confirm `OUT_OF_POLICY` and no lamp change.
6. Send a stale sequence and an unknown movement; confirm rejection and that
   the autonomous cycle continues.
7. Stop heartbeats; confirm the Uno switches to autonomous within 3 s and
   clears pending requests.
8. Restore heartbeats; confirm advisory mode resumes and the sequence resets
   only on a new TCP session.
9. Watch the ESP32 stack watermark under sustained 1 Hz heartbeat + requests
   for at least 30 minutes.
10. Run the host with `--show-wire` (or open **Controls → Wire traffic…**) and
    save the exact TX/RX exchange for each new movement. Include any rejection
    reason in the firmware notes; do not infer lamp state from an ACK.
