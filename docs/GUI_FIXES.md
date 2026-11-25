# GUI fix pass: diagnosis and changes

Targeted follow-up to the operator console, based on four reports from a
Windows machine with an AMD Ryzen 5, 8 GB DDR4 and Vega 8 graphics. The review
machine is the same class: 4 physical / 8 logical cores, 6.3 GB usable RAM,
no CUDA, CPU-only torch. Three HD/4K `.mp4` files are looped as cameras.

Nothing here changes decision semantics, the wire protocol, the 2 s freshness
threshold or the heartbeat gate. No hardware was used.

## Before screenshots

- `docs/ui/before/1280x720.png`
- `docs/ui/before/1920x1080.png`

They are offscreen captures of the real window with generated clips and the
fake detector, so they show layout and rendering only. They clearly show the
blue film, the in-window title, the amber banner, the pop-out affordance and
the short log dock.

## Issue 1: translucent blue film and unclear feeds

### Root cause

`CameraTile.paintEvent` drew the name strip and health badge with a translucent
brush (`overlay-panel`, `#D9162330`), then drew the one-pixel tile border with
`painter.drawRect(...)`. The brush was never cleared, so the border call also
**filled the entire tile** with an 85 % opaque blue-grey rectangle. It applied
to LIVE as well as stale feeds.

This was measured, not guessed. A 640x360 frame with solid red, green, blue and
white quadrants was fed to the tile in LIVE. Its rendered centres were:

| Patch | Source RGB | Rendered before | Rendered after |
| --- | --- | --- | --- |
| red | `(255,0,0)` | `(57,30,41)` | `(255,0,0)` |
| green | `(0,255,0)` | `(19,68,41)` | `(0,255,0)` |
| blue | `(0,0,255)` | `(19,30,79)` | `(0,0,255)` |
| white | `(255,255,255)` | `(57,68,79)` | `(255,255,255)` |

The before values are exactly `0.149 * source + 0.851 * (22,35,48)`, the
`overlay-panel` colour, confirming the persistent brush.

Candidate checklist:

| Candidate | Finding |
| --- | --- |
| Semi-transparent overlay widget / rgba fill | **Yes.** The border reused a translucent brush. No separate overlay widget. |
| `QGraphicsEffect` or widget opacity | No effect, no opacity setting in the tile path. |
| Stale/NO SIGNAL rectangle on LIVE | The dim layer was state-gated, but the border wash was not. |
| BGR/RGB confusion | No. Direct `QImage` and `QLabel` checks preserved all four patches exactly. |
| Downscale blur | Separate quality issue: frames were downscaled to 960 and then smoothly rescaled every 100 ms in paint. Fixed by one INTER_AREA resize to the displayed size. |

### Fix and proof

The border now clears the brush (`NoBrush`) and LIVE never draws the dim layer.
`tests/test_ui_fixes.py::test_live_video_pixels_have_no_blue_wash` samples the
rendered tile and asserts each patch within 2 RGB levels. The stale test checks
that a stale feed is still visibly dimmed. The only allowable dim is for
STALE, DISCONNECTED, CONNECTING or STOPPED.

## Issue 3: CPU and memory

### Code findings before changes

- Three `VehicleDetector` instances, each its own YOLO model, at the default
  640 px and 8 fps per camera.
- `FileSource` decoded full-resolution files (one is 3840x2160) and retained
  the full frame; the pipeline retained a second annotated full-size frame.
- The GUI polled every tile at 10 Hz and copied/downscaled an annotated frame
  each time, even when no new frame existed, then smooth-scaled again in paint.
- Separate panel (4 Hz) and video (10 Hz) timers; no hidden/minimised skip.
- Utils snapshot built at 4 Hz, but every poll also rebuilt camera/approach
  data and touched every chip.
- Logs were capped but `DecisionsTab` rebuilt all rows, and exports loaded the
  whole JSONL into memory.
- No thread cap: torch used 4 threads and OpenCV 8, for three pipelines.

The gateway was never implicated: the user saw saturation in dry-run too.

### Benchmark method

`python -m tools.benchmark` runs a real YOLO model for each camera (not a fake
detector), the configured looping HD files, and either the headless
orchestrator or the real offscreen Qt window. It samples the process tree every
5 s, including children, and normalises CPU to the whole machine. It records
source/processed fps, inference ms, frame age, drops, UI paint rate, RSS,
gateway heartbeat gaps and gate/camera transitions.

The pre-fix matrix and post-fix matrix are recorded in
[`PERFORMANCE.md`](PERFORMANCE.md). The 5-minute rows are the acceptance runs;
short runs are labelled as such.

## Issue 4: separate feed window

Paths found and removed:

| Path | Action |
| --- | --- |
| `CameraTile.double_clicked` signal | Removed. |
| `CameraTile.mouseDoubleClickEvent` emit | Removed; inherited Qt behaviour only. |
| `CameraTile.fullscreen_requested` signal | Removed. |
| Tile context-menu `Fullscreen` action | Removed. |
| `MainWindow._build_tiles` connections to both signals | Removed. |
| `MainWindow._fullscreen_camera` (`QDialog`, `WindowFullScreen`, 10 Hz `QTimer`, second `controller.frame(max_size=1600)` render) | Removed entirely. |
| README / UI design references | Removed. |

The removed dialog did render the same feed again at up to 1600 px, so it
doubled per-camera conversion and paint cost while open. No shortcut was bound
to it. The in-window Focus layout (F11) is retained, as are polygon/line,
flip and snapshot. A tile now has no path to a top-level window.

Regression: `tests/test_ui_fixes.py::test_double_click_never_opens_a_window_or_offers_fullscreen`
checks `QApplication.topLevelWidgets()` is unchanged, the signals are absent,
and the context menu contains no Fullscreen item.

## Layout and mode label

- The in-window title row and amber banner are gone. The native window title
  remains "Smarti AI Host — Traffic Control Console".
- The menu-bar badge is the single mode source: `SIMULATION:DRY-RUN`,
  `SIMULATION:MOCK-GATEWAY`, `SIMULATION:THREE-PHASE`,
  `SIMULATION:FILE-SOURCES` or `LIVE:COMPAT-2PH`, with an explanatory tooltip.
- The log dock is a draggable vertical splitter, persisted, with a minimum of
  312 px at 1280x720. The log viewport measures 242 px (>= 240) and the
  decisions table viewport 177 px at 22 px rows (>= 8 rows).
- Double-clicking a dock tab maximises the dock; double-clicking again
  restores the previous splitter sizes.
- Menu bar, tab headers and log buttons are compact; tiles can shrink to
  108 px high and keep aspect ratio with letterboxing.

## Follow-up: dock could grow but not shrink

The second cause was not the 240 px acceptance figure alone. The dock frame had
`setMinimumHeight(312)`, and its tab page's child layout also reported a 291 px
minimum-size hint. QSplitter therefore treated 291 px as the floor even after
the explicit 312 px minimum was removed, so dragging up grew the dock but
moving the handle back down could not return the space.

The frame, tab widget, log view, decisions table and detail box now have zero
minimum height and an ignored vertical size policy. The frame's only minimum is
the 80 px floor (tab header plus about two log lines). Dragging below 80 px
collapses the dock; the handle double-click and `Ctrl+J` toggle collapse and
restore; the height and collapsed state persist. Corrupt or out-of-range saved
heights fall back to 312 px, and old camera ids in persisted focus settings are
ignored.

## Later addition: exact gateway wire traffic

A read-only diagnostic window was added after this fix pass. **Controls → Wire
traffic…** is modeless and polls the controller's bounded transcript every
400 ms while visible; it shows exact TX/RX JSON, a status count and **Copy all**.
The data is captured in the gateway client before parsing, kept in memory (newest
1,000 lines) and never added to `decisions.jsonl`. It does not touch the tile
paint path, decision logic or protocol. See
[`UI_DESIGN.md`](UI_DESIGN.md#12-exact-gateway-wire-traffic) and
[`NETWORKING.md`](NETWORKING.md#inspecting-the-gateway-link).

## Verification

- `python -m pytest -q` → 164 passed on the current tree (157 at this fix-pass
  checkpoint, before the wire-traffic tests).
- Pixel, stale, badge, layout, pop-out, preview, profile, load-shedding,
  dropped-frame and streaming-export tests were added.
- Exact wire capture, listener safety, bounded history and the GUI window are
  covered by `tests/test_wire_transcript.py` and `tests/test_ui_wire.py`.
- Core import guard still passes (`tests/test_gui_isolation.py`).
- No hardware, real RTSP, ESP32, Uno, high-DPI or multi-monitor testing.
