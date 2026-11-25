# Historical state (pre-multi-camera)

> **Historical snapshot — not current documentation.** This records the tree at
> commit `0bb9835`, before the multi-camera, decision-engine, gateway and GUI
> work. In particular, the exact wire-traffic view described as missing below
> was added later; see [`README.md`](../README.md) and
> [`NETWORKING.md`](NETWORKING.md) for current behaviour.

Snapshot of `Intelligent_Traffic_Control` as of commit `0bb9835`. This records
what the code did at that commit; where it disagrees with `README.md`, the
current code and README win.

## Runtime shape

A single top-level script, `main.py`. Everything runs in module scope at import
time: components are constructed, the `while True` loop runs, then resources are
released. There is no `main()` function, no `if __name__ == "__main__":` guard,
and no argument parsing. `main.py` calls `exit()` when the camera cannot be
opened, which raises `SystemExit` during import.

One webcam → one YOLO model → one tracker → one counter → one congestion
analyzer. All of these are **module-level globals**, so the process supports
exactly one camera and one intersection, forever.

## Public classes and functions

| Module | Symbol | Notes |
| --- | --- | --- |
| `camera/webcam.py` | `Webcam.__init__(camera_index=0)` | Opens `cv2.VideoCapture(index)` |
| | `Webcam.is_opened()` | `VideoCapture.isOpened()` |
| | `Webcam.read()` | Returns `(ok, frame)`, horizontally flips (`cv2.flip(frame, 1)`) on success |
| | `Webcam.release()` | Releases the capture |
| `detection/vehicle_detector.py` | `VehicleDetector.__init__(model_path='yolo11n.pt')` | Loads the YOLO model eagerly |
| | `VehicleDetector.detect(frame)` | `model.track(frame, conf=0.3, verbose=False, persist=True, tracker="bytetrack.yaml")` |
| `detection/object_classifier.py` | `classify_object(class_id)` | Maps a COCO class id to a category string |
| | `OBJECT_CATEGORIES` | Module-level dict (see below) |
| `tracking/vehicle_tracker.py` | `VehicleTracker.__init__(movement_threshold=5)` | Holds `previous_centers: dict[int, tuple[int,int]]` |
| | `VehicleTracker.update(results, class_names)` | Returns a list of per-object dicts |
| `analytics/vehicle_counter.py` | `Vehicle_counter.__init__(line_y=300)` | Misspelled class name (`snake_case` mixing) |
| | `Vehicle_counter.update(frame_objects)` | Counts line crossings |
| | `Vehicle_counter.get_statistics()` | `{"vehicles", "pedestrians", "others", "total"}` |
| `analytics/road_region.py` | `RoadRegion.__init__(points)` | `points` are normalized `(x, y)` pairs |
| | `RoadRegion.get_polygon(frame_shape)` | Scales to pixels, returns `np.int32` array |
| | `RoadRegion.contains(center, frame_shape)` | `cv2.pointPolygonTest(...) >= 0` |
| | `RoadRegion.draw(frame)` | Draws the polygon in place, also returns the frame |
| `analytics/congestion.py` | `CongestionAnalyzer.__init__(window_seconds=10, moderate_threshold=3, high_threshold=8, stationary_threshold=0.5)` | |
| | `CongestionAnalyzer.update(frame_objects)` | Returns count/ratio/congestion dict |

## How the YOLO tracker is invoked

`VehicleDetector.detect()` calls `model.track(..., persist=True,
tracker="bytetrack.yaml")`. `persist=True` keeps **ByteTrack state inside the
`YOLO` model object**, keyed by nothing in particular. Two cameras sharing one
`VehicleDetector` would therefore share one tracker and receive interleaved,
mismatched track IDs. Any multi-camera design must either create one model per
camera or run one tracker instance per camera.

Track data is read from `results[0].boxes`:

- `boxes.id` — track IDs, `None` when nothing is tracked (the tracker returns an
  empty list in that case)
- `boxes.cls`, `boxes.conf`, `boxes.xyxy`

## Object categories

`OBJECT_CATEGORIES` maps raw COCO ids:

- `pedestrian`: `0` (person)
- `non_motorized_transport`: `1` (bicycle)
- `vehicle`: `2` (car), `3` (motorcycle), `5` (bus), `7` (truck)
- `traffic_infrastructure`: `9` (traffic light), `11` (stop sign)
- `animals`: `16` (dog), `17` (cat)
- everything else → `"other"`

Consequence: a bicycle crossing the counting line is counted as **"other"**, not
as a vehicle.

## Hard-coded values

| Value | Where | Problem for multi-camera |
| --- | --- | --- |
| Counting line at `line_y = 300` px | `Vehicle_counter.__init__` default, used by `main.py` | Absolute pixels, not resolution-independent; a different camera resolution moves the line. |
| Road polygon `[(0.1,0.3),(0.9,0.3),(1.0,0.95),(0.0,0.95)]` | `main.py` | One polygon for one scene; not per camera. |
| `movement_threshold = 5` px/frame | `VehicleTracker.__init__` default | Per-camera sensitivity is not configurable. |
| `conf = 0.3` | `VehicleDetector.detect` | Not configurable. |
| Congestion thresholds `3` / `8` / `0.5` | `CongestionAnalyzer.__init__` defaults | Not configurable from outside. |
| Moderate stationary ratio `0.3` | `CongestionAnalyzer.update` | **Hard-coded in the branch**, so `stationary_threshold` does not control it. |
| Camera index `0` | `Webcam.__init__` default | Only local webcams, no RTSP/IP sources. |
| Window `10` s | `CongestionAnalyzer.__init__` default | Fine, but not wired to config. |

## Data flow per frame (`main.py`)

1. `camera.read()` — flipped frame.
2. `detector.detect(frame)` — YOLO tracking results.
3. `tracker.update(results, detector.model.names)` — list of object dicts with
   `track_id`, `class_id`, `object_name`, `category`, `confidence`,
   `coordinates`, `previous_center`, `center`, `displacement`, `movement`.
4. `road_objects` = objects whose center is inside the configured polygon.
5. `counter.update(frame_objects)` — **all** objects, not road-filtered.
   `congestion_analyzer.update(road_objects)` — **road-filtered only**.
6. Draw `results[0].plot()`, polygon, center dots, `ID n: <movement>` labels,
   the counting line, and the statistics text.
7. `cv2.waitKey(1) & 0xFF == ord("q")` to exit.

## Bugs and smells relevant to multi-camera work

1. **`persist=True` tracker state lives in the shared model** — see above; the
   single largest blocker for running several streams.
2. **Unbounded per-process state.** `VehicleTracker.previous_centers` and
   `Vehicle_counter.counted_ids` are never pruned. Memory grows with every track
   ID ever seen, and a recycled ByteTrack ID is treated as "already counted" and
   resumes movement from a stale center.
3. **Counting and congestion use different populations.** Accumulated counts
   cover the whole frame; congestion covers only the polygon. The two metrics in
   the same overlay are not comparable.
4. **Direction-agnostic counting.** Every crossing increments once per track,
   in either direction, with no per-direction split.
5. **`CongestionAnalyzer` averages over frames, not over observed vehicles.**
   Frames with zero vehicles are included, diluting `average_vehicle_count`; the
   level therefore lags real density.
6. **`stationary_threshold` is ignored for the Moderate branch** (hard-coded
   `0.3`), so the documented knob only affects High.
7. **Movement is single-frame.** A 5 px threshold with no smoothing marks slow
   traffic as stationary and is sensitive to camera shake.
8. **`main.py` has import-time side effects** (camera open, infinite loop,
   `exit()`), so nothing can import the application for testing.
9. **No health/staleness concept.** A frozen or disconnected camera is
   indistinguishable from "no traffic".
10. **`pandas` is a declared requirement but never imported.** `opencv-python`,
    `numpy`, `ultralytics` are the real dependencies.
11. **No tests, no CI, no license**, and `requirements.txt` is unpinned.
12. **`.gitignore` contains a stray `try` entry** (appears accidental) which
    ignores any file or directory named `try`.
13. **Two processes cannot share the webcam**, and there is no reconnect logic:
    a dropped `read()` breaks the loop with no recovery.

## What is missing for the target system

- No frame source abstraction (only `Webcam`).
- No per-camera state ownership, no scheduler, no frame dropping.
- No `ApproachMetrics` snapshot.
- No decision engine (no scoring, starvation, hysteresis, clamping).
- No network output at all: no gateway client, no heartbeat, no request/response
  correlation, no sequence handling.
- No mock gateway, no simulated cameras, no way to test without hardware.
