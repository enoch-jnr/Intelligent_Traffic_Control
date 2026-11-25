# Operator GUI design

Phase 0 deliverable for the optional PySide6 operator layer. Nothing here
changes decision behaviour, the wire protocol or the health policy. The GUI is
read-only with respect to the controller: it sends advisory requests through
the existing client and can close the heartbeat gate, never open it.

## 1. Reference image and licensing

The brief describes a licensed Shutterstock "surveillance system" thumbnail.
**No reference image is present in this repository.** `docs/ui/` did not exist,
so there was nothing to view, copy or compare. No Shutterstock asset was
downloaded, embedded or committed, and the app uses no stock media.

`.gitignore` ignores `docs/ui/reference.*` so a user-supplied reference cannot
be committed by accident. Screenshots produced by this project are our own and
may be committed under `docs/ui/`.

## 2. PySide6 check (Phase 0 gate)

| Check | Result |
| --- | --- |
| Python | 3.14.7 (`.venv/Scripts/python.exe`) |
| Wheel | `PySide6 6.12.0` (`cp310-abi3-win_amd64`) installed from PyPI |
| Import | OK |
| Offscreen window | `QT_QPA_PLATFORM=offscreen` created a `QApplication` and a screen |

On this Windows install Qt prints `QFontDatabase: Cannot find font directory`
when running offscreen because Qt no longer bundles fonts. It is a warning, not
a failure; system fonts are used. No web font, icon pack, CDN or network call is
used by the UI. PySide6 is the only added dependency.

## 3. Orchestrator and pipeline audit (what the GUI may read)

The GUI must not reach into pipeline internals. The audit found these usable
seams and the smallest additions needed:

| Need | Existing | Added hook |
| --- | --- | --- |
| Camera health | `CameraManager.health_all()` | read-only accessor on the controller |
| Latest metrics | `CameraPipeline.latest_metrics()` | controller snapshot |
| Annotated frame | `CameraPipeline.latest_annotated()` copies the full frame | `latest_annotated(max_size)` downscales before returning |
| Decision record | `tick()` builds the JSONL entry | optional `on_tick(entry)` callback, wrapped so a slow/broken GUI cannot break the loop |
| Gate state/reason | `health_gate_open()`, gate transition log | last reason retained for the status tooltip |
| Exact wire traffic | No existing seam | `GatewayClient` transcript + `AppController.wire_transcript()` returning immutable `WireLineView` copies; the UI never imports gateway internals |
| Requests held | none | `hold_requests()` / `resume_requests()`; decisions still logged with `request.reason = operator_hold` |
| Operator release | none | `release_to_autonomous()` forces the existing gate closed; `resume_control()` restores normal gating |
| Reset | `DecisionEngine.reset()` | `request_reset()` processed at the top of the next tick: engine + pipeline state reset and gateway session restart |
| File playback | none | `FileSource.pause()/resume()/restart()/set_speed()`; files only, testing aid |
| Pipeline overlay toggles | `annotate` fixed at construction | `CameraPipeline.set_overlays(polygon, count_line)` |
| Frame flip | `ThreadedFrameSource.flip` | setter used by the tile flip action |

Why a per-tick callback rather than polling the JSONL: the log may be disabled
or rotated, and the UI needs the exact decision the engine produced. The
callback runs on the decision thread, so it only copies the record into a
bounded queue; it never touches Qt.

Reset is deferred to the next tick on purpose. Calling `DecisionEngine.reset()`
from the GUI thread while `evaluate()` runs would race the smoothing deques.
The same rule applies to release/hold: they only flip thread-safe flags.

Annotation is enabled for the GUI even without `--display`, but no OpenCV window
is created. The existing `--display` path and headless `run()` are unchanged.

## 4. What was adopted, adapted or skipped

| Reference element | Decision |
| --- | --- |
| Title bar + menu bar | Adopted. File / View / Cameras / Log / Settings / Help; mode badge at the right. |
| Account / Login | Skipped; replaced by the mode / simulation badge. |
| 3 x 3 dense grid | Adapted to 2 x 2 with 2 px gaps: MAIN_A, MAIN_B, SEC and the T-junction schematic. Focus mode gives one large tile plus two small. |
| Tile name + LIVE label | Adopted; health badge is icon + word (LIVE / STALE / DISCONNECTED / CONNECTING). Red is reserved for faults, so LIVE is green, not red. |
| Corner telemetry | Adopted with the same four corners, monospace with a dark outline; View > Overlays hides them. |
| Right sidebar sections | Adopted: System, Live Events, Playback, Camera List, Controls. |
| Playback transport + speed | Adapted for file sources only, labelled testing aid. Hidden for RTSP and webcam. |
| PTZ, numbered 1-6 buttons | Skipped: cameras are fixed. |
| Bottom START + session timer | Adopted: Start/Stop left, timer centred, status chips right. |
| Hospital footage / camera names | Replaced by real approaches and live frames. |

Deliberate deviations from the reference: a fourth schematic tile instead of a
ninth camera, health words instead of a red LIVE dot, no account area, no
decorative motion, and a warning-coloured simulation mode badge in the menu bar.

## 5. Colour tokens

All colours live in `ui/theme.py`; no colour literal appears outside it. Values
are the brief's sampled chrome, with the following **AA adjustments** found by
computing the pairs:

| Token | Dark value | Adjustment |
| --- | --- | --- |
| `bg-chrome` | `#1C323C` | as supplied |
| `bg-base` | `#222B38` | as supplied |
| `bg-panel` | `#2A3846` | as supplied |
| `bg-panel-raised` | `#32424E` | as supplied |
| `bg-field` | `#1A202F` | as supplied |
| `border` | `#425560` | as supplied |
| `text-primary` | `#D3DDE6` | 7.5:1 or better everywhere |
| `text-secondary` | `#A6B2BF` | lightened from `#97A4B2`, which was 4.08:1 on raised |
| `text-muted` | `#6B7D8B` | decorative/disabled only |
| `accent` | `#3E6787` | white label is 6.0:1 |
| `accent-text` | `#7FB3D5` | 5.9:1 on chrome, 4.6:1 on raised |
| `selection` | `#1A2A47` | selection uses `accent-text` for the label |
| `status-ok` | `#5ACBA3` | lightened; 5.2:1 on raised, 6.0:1 on panel |
| `status-warn` | `#E5B84A` | 5.6:1 on raised |
| `status-critical` | `#F06A6E` | icons/large text; 3.45:1 on raised (3:1 UI) |
| `status-critical-text` | `#FF8A8D` | 4.6:1 on raised, 5.3:1 on panel |
| `status-unknown` | `#A6B2BF` | lightened; grey + word, 4.8:1 on raised |
| `approach-main-a` | `#5CC8D8` | 5.3:1 on raised |
| `approach-main-b` | `#7AA7F0` | route/identity; text token `approach-main-b-label` `#8FB4FF` is 5.0:1 on raised |
| `approach-sec` | `#A78BFA` | route/identity; text token `approach-sec-label` `#C4B5FD` is 5.6:1 on raised |
| `focus-ring` | `#93C5FD` | 5.8:1 on raised |

Measured body-text contrast (WCAG 2.1): primary 7.53-11.81, secondary
4.72-6.40, accent-text 4.60-7.21, status-ok 4.09→5.19 after adjustment,
status-warn 5.57-8.74, critical-text 4.58-7.18. `text-muted` remains below AA
and is documented as decorative only, never used for status or body copy.
`status-unknown` uses the lightened secondary value because the previous
`#6B7D8B` was 2.44:1 on raised.

Light theme: `bg-base #EEF2F5`, `bg-panel #FFFFFF`, `bg-field #F4F7FA`,
`border #CBD5DF`, `text-primary #14212E` (14.5:1), `text-secondary #46576A`
(6.6:1), status hues darkened to `#0F6B4F` (5.8:1), `#7A5A00` (5.7:1) and
`#B3261E` (5.8:1) on the base. Selection uses `#DCE7F2`.

Approach identity colours are cyan/violet/pink, never red, green or amber, so
they cannot be mistaken for a status or a physical signal aspect.

## 6. Typography, spacing, shape

- UI: Segoe UI, fallbacks Inter then generic sans. Logs and JSON: Cascadia Mono,
  Consolas, generic monospace. No downloaded fonts.
- 11 px helper, 13 px body, 15 px section titles, 20 px large readouts.
  Tabular numerals for metrics.
- 8-pt spacing grid; 10 px cards; 6 px buttons; 40 px minimum control height.
- Primary = accent fill with white text; secondary = outline; destructive =
  critical outline (fills only inside confirmation); caution = warn outline for
  Release.
- Every icon button carries text or a tooltip; focus ring is 2 px.

## 7. Operator-safety rules encoded in the UI

- Recommendations and expected red are always labelled advisory/derived.
- The UI never claims a lamp changed; it shows the last recommendation and the
  controller's reply.
- Missing data is "unknown" (grey + word), never an optimistic default.
- Destructive actions (Stop, Reset, Release, Shutdown) confirm, state the
  consequence in one sentence and default to Cancel.
- Simulation (mock gateway, dry run, three-phase, or file sources on a real
  gateway) shows one persistent `STATE:DETAIL` menu-bar badge.
- Snapshots are explicit-click only, warn once about people/plates, and are
  never written automatically. Exports contain no images.
- No sounds by default; reduced motion removes pulsing and any animation.
- About states: "Advisory research prototype. Not validated on hardware or
  approved for public roads."

## 8. Layout and interaction

Minimum 1280 x 720, resizable. The work area is a vertical splitter (video
above, Live log/Decisions below) next to a 280-430 px sidebar; its position
persists. The native title bar, menu bar and status bar never scroll away.
Sidebar sections are collapsible triangles whose state persists through
`QSettings`, as do window geometry, theme, overlay visibility, preview state
and reduced motion.

- One scheduler: video at 4/8/12 fps (or off), panels at 4 Hz, chips at 1 Hz.
- Log ring buffer 5,000 lines, inserted in batches; decisions are bounded and
  only refreshed while the dock is visible.
- UI actions run exports and shutdown in worker threads; the decision loop and
  gateway heartbeat never wait on the GUI.
- Keyboard: visible focus, logical tab order, and one registry used by menus,
  tooltips and Help > Keyboard shortcuts. Existing bindings stay
  (Ctrl+E/Ctrl+Shift+E/Ctrl+Shift+S exports, Ctrl+H hold, Ctrl+R reset,
  Ctrl+L release, F5 start/stop, F11 focus, Ctrl+Q shutdown). New bindings:
  Ctrl+J log panel, Ctrl+T theme, F6/Shift+F6 next/previous camera,
  Ctrl+1/2/3 direct camera focus, Ctrl+, configuration.
- The focused camera is named in the status bar, not shown by colour alone.
- The log dock has an 80 px floor, collapses when dragged below it, toggles
  with Ctrl+J or a splitter-handle double-click, and persists its height.

## 9. Phase plan

0. This document, dependency and orchestrator audit.
1. `app/controller.py`, `UiSnapshot`, event feed, `LogBroadcaster`, exporters,
   orchestrator/pipeline/file-source hooks; Qt-free tests.
2. `ui/theme.py`, shell, menu bar, status bar, collapsible sidebar, empty grid.
3. Camera tiles, overlays, health/NO SIGNAL, Grid/Focus; fps measurement.
4. Schematic, metric cards, last decision and reasons.
5. Live log and decisions table with filters, search and caps.
6. Controls, confirmations, worker exports, diagnostics, shortcuts.
7. Accessibility/polish, light theme, persisted layout, README and screenshots.
8. Exact wire-traffic view: `--show-wire`, **Controls → Wire traffic…**, live
   exact-JSON dialog and Copy all.

## 10. Fix pass: compact layout and calibrated rendering

The follow-up pass removed the in-window title and amber banner. Identity is
now only the native window title; simulation/live state is one menu-bar badge
with exact text `SIMULATION:DRY-RUN`, `SIMULATION:MOCK-GATEWAY`,
`SIMULATION:THREE-PHASE`, `SIMULATION:FILE-SOURCES` or `LIVE:COMPAT-2PH` and a
tooltip. It cannot be dismissed.

The video area and Live log/Decisions dock are a vertical `QSplitter` with
persisted sizes. At 1280x720 the dock is at least 312 px, the log viewport is
242 px and eight 22 px decision rows fit; the dock is ~47 % of the work area
because that is the smallest split that satisfies both acceptance measures.
The previous 60/40 target could not fit 240 px of log plus its tab and compact
filter row. Double-clicking a dock tab maximises it and double-clicking again
restores the split. Menu, tab and log-button padding is compact; tiles can
shrink to 108 px and letterbox rather than forcing a taller grid.

The video path now delivers one INTER_AREA-downscaled frame to the displayed
size. Tiles are opaque, use a dirty frame version, wrap the owned frame buffer
without a second copy, and scale only once per frame or resize. The blue film
was a translucent QPainter brush left over from the health badge and reused by
the border; the border now clears it. LIVE has no tint. Dimming is limited to
non-LIVE states.

View > Video preview turns off frame conversion and painting while panels keep
updating. Preview quality is 4/8/12 fps at 0.5/0.75/1.0 scale and persists.
One scheduler drives video (preview rate), panels (4 Hz) and status chips
(1 Hz); it does nothing when the window is hidden or minimised. No tile opens
a second window: the double-click handler, fullscreen context item, signal and
frameless dialog are removed, while Focus (F11) remains.

See [`GUI_FIXES.md`](GUI_FIXES.md) and [`PERFORMANCE.md`](PERFORMANCE.md).

## 11. Implementation status and measured impact

Phases 0-8 are implemented. The console is `--gui` only; `python main.py`
without it and the existing `--display` window are unchanged. The suite has
grown from 99 to 164 tests. The core/UI guard test parses every core package and
asserts it imports neither `ui` nor PySide6, including in a fresh subprocess.

### Measured GUI impact (Phase 3 requirement)

Method: three synthetic 15 fps file sources, the same fake detector, a mock
gateway at a 0.1 s heartbeat interval, warm-up, then a 4 s window. The GUI run
builds and shows the real window offscreen, processes Qt events continuously
and repaints tiles at 10 fps. Both runs used the same CPU and configuration.

| Metric | Headless | GUI (offscreen) |
| --- | --- | --- |
| Source fps MAIN_A / MAIN_B / SEC | 14.89 / 14.97 / 14.85 | 14.94 / 14.94 / 14.94 |
| Processed fps MAIN_A / MAIN_B / SEC | 12.28 / 12.50 / 12.73 | 12.79 / 12.59 / 12.71 |
| Heartbeats in 4 s (0.1 s target) | 36 | 36 |
| Mean heartbeat gap | 0.110 s | 0.110 s |
| Maximum heartbeat gap | 0.127 s | 0.112 s |

Within this environment the console did not reduce pipeline throughput or
heartbeat regularity; the differences are inside run-to-run noise. This is a
short Windows/CPU measurement, not a soak test, and it says nothing about a
real camera, ESP32 or Uno.

### Screenshot

`docs/ui/console.png` is an offscreen capture of the real window driven by
synthetic clips and the mock gateway: three live tiles, the advisory schematic,
system state, controls, log and decision table, with the amber simulation
mode badge. It was rendered with system Segoe UI/Consolas loaded explicitly because
Qt's offscreen platform on this machine does not discover the Windows font
directory. No font, icon or stock asset is bundled by the app. The screenshot
predates the **Wire traffic…** button added in section 12; the README controls
table is authoritative. It contains no real people, plates or site imagery.

During the screenshot review two defects were found and fixed: the pipeline's
baked-in stats duplicated the tile telemetry (GUI frames now draw shapes/boxes
only, while `--display` keeps its stats), and long control labels clipped in the
sidebar (labels are now short, with the full action names and tooltips in the
menus).

### Not tested

- Real RTSP cameras, ESP32-S3, Arduino Uno, lamps or bench wiring.
- High-DPI scaling, multi-monitor placement, touch input and Windows text
  scaling above 100%.
- Long soak runs, log rotation under sustained load, and a real 30-minute
  heartbeat session.
- Modal export/diagnostics dialogs end-to-end on a desktop; their controller
  and exporter logic is covered without Qt, and the GUI builds offscreen.

**Nothing in the GUI, decisions or protocol was verified on hardware.**

### Open items and controls not added

- No manual "send test request" button was added, even for loopback; the
  gateway is only driven by the decision engine. This avoids any operator path
  that could send a request against real firmware. The wire-traffic window is
  read-only and cannot inject a message.
- PTZ and numbered preset controls were skipped: the cameras are fixed.
- Three-phase movements remain a disabled proposal; the mode badge marks them
  as simulation and the firmware does not exist.
- The optional light theme uses darkened status/identity hues; the dark theme's
  measured AA pairs are in section 5. `text-muted` is intentionally decorative
  only and is never used for status or body copy.

## 12. Exact gateway wire traffic

**Controls → Wire traffic…** opens a modeless, read-only window titled
“Gateway wire traffic — exact JSON”. It exists to answer one question without a
packet capture: what exact JSON left the host, and what exact JSON came back?

| Property | Behaviour |
| --- | --- |
| Data source | `GatewayClient` records each complete line after a successful send and each RX line before decoding. |
| Scope | Handshake, heartbeats, requests, replies, rejection reasons and malformed inbound lines. |
| Storage | Thread-safe in-memory transcript, newest 1,000 entries; no file and no decision-log row. |
| Presentation | Monospace read-only text, `TX`/`RX` plus UTC timestamp, blank line between entries. JSON is not reformatted. |
| Refresh | 400 ms timer, started only while shown and stopped when hidden. |
| Actions | **Copy all** and **Close**; the view cannot send or modify traffic. |
| Scrolling | Follows the newest entry only when already at the bottom; otherwise preserves the operator's position. |
| Isolation | The UI consumes immutable `WireLineView` copies from the controller; it imports no gateway module. |
| Empty state | “No gateway traffic yet” until the client has sent or received a line. |

The same history drives `--show-wire`, so CLI and GUI cannot disagree about the
message text. A malformed or rejected reply is shown as received even though it
produces no successful decision or request result. The window never claims a
lamp changed; `CONTROLLER_ACCEPTED` remains an acknowledgement only.
