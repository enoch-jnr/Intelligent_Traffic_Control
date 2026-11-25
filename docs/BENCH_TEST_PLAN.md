# Bench test plan — first hardware session

**Status: NOT YET PERFORMED.** Every step below is a plan. No camera, ESP32,
Uno or signal head has been touched by this repository's development, and
running this plan is a human decision with the hardware powered down until the
checks say otherwise.

Scope: **compat mode only** (`gateway.mode: compat_two_phase`). Three-phase
movements are proposals and cannot be exercised on current firmware; see
`docs/FIRMWARE_THREE_PHASE_PROPOSAL.md`.

Work on a bench with LEDs or a lamp simulator, not a live junction. Two people:
one at the laptop, one at the hardware with a multimeter. Stop at any step that
does not behave as described; do not "fix it live".

| # | Step | Expected observation | Pass criteria | Status |
| --- | --- | --- | --- | --- |
| 0 | Software pre-check, no hardware | `pytest` green; `python -m gateway.mock_gateway` accepts requests from a file-based config; `--show-wire` prints handshake and heartbeat TX/RX lines | Host, config, logs and the exact wire view work without hardware | NOT YET PERFORMED |
| 1 | Inventory and safety | Uno lamps replaced by LEDs or disconnected; ESP32 and Uno on a common ground; level shifter available | Nothing connected to mains switching | NOT YET PERFORMED |
| 2 | Network: camera Ethernet | Laptop `192.168.50.2/24`; cameras `.11/.12/.13`; ping each camera; open each RTSP URL in VLC | All three streams play for 60 s without dropping | NOT YET PERFORMED |
| 3 | Network: gateway Wi-Fi | Laptop joins `Smarti-Gateway`; `192.168.4.1` pings; camera subnet still reachable | Both interfaces work at once; Wi-Fi is not the default route for camera traffic | NOT YET PERFORMED |
| 4 | Logic-level interface, **powered down** | ESP32 TX/RX are 3.3 V; Uno RX is 5 V tolerant input; level shifter wired; common ground | No direct 5 V push into a 3.3 V ESP32 pin; continuity and polarity checked with the meter | NOT YET PERFORMED |
| 5 | Power-up, no host | Uno runs its autonomous two-approach cycle | LEDs show one approach green at a time; yellow/all-red visible between changes | NOT YET PERFORMED |
| 6 | Handshake and heartbeat | Start `python main.py --config config.yaml --show-wire`; log shows `session established; sequence reset to 1`; wire view shows `TX {"type":"HEARTBEAT"}` followed by `RX {"type":"GATEWAY_ACK","status":"HEARTBEAT_OK"}`; host logs `heartbeat gate OPEN` | Heartbeats continue at ~1 Hz for 2 minutes; no `AI_LINK_OFFLINE` errors while cameras are live; every heartbeat has a matching reply or a logged timeout | NOT YET PERFORMED |
| 7 | Confirm the Uno-to-ESP32 ACK path | With healthy cameras, force one advisory request (place a vehicle or play the test clip in the region; temporarily set `min_interval_s: 0`) and note the exact request/reply in the wire view | JSONL `request.accepted` is true, `uno_ack` text is recorded, and the Uno stores it as pending; the wire view shows the exact `TRAFFIC_REQUEST` and `GATEWAY_ACK`; **do not claim the lamp changed** unless it is observed at the next phase entry | NOT YET PERFORMED |
| 7b | **Label-to-lamp check** (bench only, after 7) | Send one advisory request for the `MAIN` unit and one for `SECONDARY` under supervision, and watch which physical heads change. Confirm the Uno's `MAIN` outputs drive **both** `MAIN_A` and `MAIN_B`, and its side output drives `SEC`. | Correct heads respond for their group; no request is sent to the other group. **Abort and power down if any other head changes, if both groups change together, or if the mapping is not the expected one.** | NOT YET PERFORMED |
| 7c | **Capture exact wire evidence** (after 7/7b) | In the GUI open **Controls → Wire traffic…**, or run with `--show-wire`; use **Copy all** before shutdown. Include the request, reply and any rejection reason in the session notes. | Exact TX and RX JSON lines are saved with the step result; no credentials appear; the transcript is not confused with lamp state | NOT YET PERFORMED |
| 8 | Unplug one camera | JSONL per-camera `state` changes; decision log reasons name the unhealthy approach; heartbeat gate closes; heartbeats stop; Uno goes autonomous after its 3 s freshness window | No recommendation is sent while unhealthy; Uno's LEDs continue cycling on their own | NOT YET PERFORMED |
| 9 | Reconnect the camera | Camera returns `LIVE`; metrics become healthy; heartbeat gate reopens; a new request is accepted | Sequence continues from the same session (no reset without a new TCP connection) | NOT YET PERFORMED |
| 10 | Disconnect Wi-Fi | Client logs `session lost`; reconnects with backoff 1→10 s; heartbeat gate and requests stop; Uno stays autonomous | After reconnecting, a new session starts with heartbeat and sequence reset to 1 | NOT YET PERFORMED |
| 11 | Sustained load and stack watermark | 30 minutes of 1 Hz heartbeats plus periodic requests; read the ESP32 stack-watermark log | Free stack stays above the firmware's safety margin and is recorded in the session notes; no resets or watchdog trips | NOT YET PERFORMED |
| 12 | Shutdown and evidence | Copy the wire transcript, then Ctrl-C the host; JSONL and console logs saved with the session notes | Clean shutdown; no requests after shutdown; Uno autonomous; wire excerpt and all step results recorded | NOT YET PERFORMED |

## Capturing wire evidence

Start the app with `--show-wire` when a terminal record is useful, or open
**Controls → Wire traffic…** in the console. The client records complete TX and
RX lines from the moment it starts, including handshakes, heartbeats, requests,
replies and malformed inbound lines. The GUI window is read-only and polls while
visible; it never injects a request.

Use **Copy all** before shutting down. The in-memory history is bounded to the
newest 1,000 lines, so a long soak test overwrites older traffic; for a full
30-minute record, redirect `--show-wire` to a terminal log file and keep the GUI
copy as the quick excerpt. Keep only what is needed for the session notes; the
traffic JSON contains no camera credentials, but it may contain operational
detail.

## What to record for each step

- Timestamp, step number, pass/fail, and the exact observation.
- The relevant JSONL lines (`mode`, `health_gate_open`, `approaches`,
  `decision`, `recommendation`, `request`).
- The exact `TX`/`RX` lines around each request, from the wire view, plus any
  malformed or rejected reply and its reason.
- Gateway serial log excerpt, including any `ERROR` reason.
- Uno LED behaviour (which head is green, yellow/all-red timing) **as
  observed**, not inferred from an ACK.
- For step 11: the stack watermark value and free heap at start and end.

## Abort criteria

Stop immediately and power down if:

- two signal heads are ever green together;
- the Uno stops its autonomous cycle when the host or Wi-Fi is lost;
- the ESP32 resets or the stack watermark approaches the firmware limit;
- any 5 V signal is measured on a 3.3 V ESP32 pin;
- a request is applied without yellow/all-red clearance.

## Explicit non-goals

- No three-phase movements are tested here; current firmware rejects them.
- No red-duration or phase-order request is sent; those fields do not exist.
- Nothing in this plan validates the decision weights on real traffic. That
  needs recorded footage and a separate tuning session.
