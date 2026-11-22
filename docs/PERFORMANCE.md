# GUI and inference performance

This records the benchmarking and optimisation pass requested after the first
console deployment. It is separate from decision correctness: no health
threshold, protocol field or heartbeat-gate rule changed.

## Machine

- Windows 11, AMD64 Family 23 (Ryzen 5 class), 4 physical / 8 logical cores.
- 6.3 GB usable RAM (8 GB installed class), Vega-class integrated graphics.
- Python 3.14.7, torch 2.15.0+cpu, OpenCV 5.1.0, Ultralytics 8.4.215.
- CUDA unavailable, ROCm not used. All inference is CPU.

The operator's report (Ryzen 5, 8 GB, Vega 8, 100 % CPU and 90 %+ memory) is
consistent with this machine class.

## Benchmark

```bash
python -m tools.benchmark --duration 300 --warmup 30 \
  --out docs/benchmarks/after.json --label after
```

The tool runs four modes: headless/gui × dry-run/mock-gateway. Each uses the
real YOLO detector and the three configured looping files. `psutil` samples the
whole process tree every 5 s; CPU is normalised so 100 % means all logical
cores busy. The GUI is the real window on Qt's offscreen platform, including
frame conversion, painting, logs/decisions and the preview scheduler.

## Profiles

`performance.profile` is `auto|low|balanced|high`; explicit fields override the
preset. Auto is **low** when total RAM is <= 8.5 GB, otherwise balanced. On
this machine the resolved profile is **low (auto)**.

| Preset | target_fps | imgsz | preview fps | preview scale | torch threads |
| --- | --- | --- | --- | --- | --- |
| low | 3 | 480 | 5 | 0.50 | 1 |
| balanced | 5 | 640 | 8 | 0.75 | 1 |
| high | 8 | 640 | 10 | 1.00 | 2 |

OpenCV is capped at one thread. The GUI's View > Preview quality setting maps
to 4/8/12 fps at 0.50/0.75/1.00 scale and overrides the preset only for the
preview, not for inference. Capture is downscaled once to `2 * imgsz` wide
(960 px on low) using INTER_AREA before the frame is retained.

## Before / after

Populated from the 5-minute matrix. See tables below.

### What dominated CPU

Before the changes, three YOLO instances at 8 fps plus three full-HD/4K
decoders saturated the CPU, so a 2 s freshness window could not be met. The
GUI added a second full-frame copy/downscale for every tile at 10 Hz. The
gateway was not a factor (dry-run and mock behaved alike).

### What dominated memory

Three full model instances, one latest full-resolution frame per source, a
second full-size annotated copy per pipeline, and repeated GUI-side copies.
Exports also loaded the entire JSONL at once. Logs and tables were bounded, but
the table rebuild was CPU-visible.

## Applied changes

1. Performance presets, auto detection and explicit overrides.
2. `torch.set_num_threads` / `cv2.setNumThreads` caps.
3. One INTER_AREA capture downscale; full frames are dropped immediately.
4. Adaptive target-fps reduction with a warning log and UI readout; no health
   threshold is relaxed.
5. One GUI scheduler: 4/8/12 fps video, 4 Hz panels, 1 Hz chips; hidden,
   minimised and preview-off paths do no frame work.
6. Dirty frame versions, no extra QImage copy, opaque tile, one paint scale,
   no graphics effects or full-frame translucent layers.
7. Bounded logs/tables/events retained; only the visible decisions tab is
   refreshed.
8. Streaming JSONL/CSV exports and bounded diagnostics tails.
9. Opened the table detail pane smaller so eight rows fit at 1280x720.

## Chosen not to do

- **Shared detector / batched inference.** This would save model memory but
  ByteTrack state lives on the model, and the existing isolation guarantee
  would be weakened. Keeping one detector per camera was judged safer; the
  memory measurement is reported instead of hiding the trade-off.
- **ONNX Runtime / DirectML.** Not added. No dependency was installed or
  requested for this pass. It needs an explicit approval and a separate
  accuracy/performance evaluation.
- **CUDA/ROCm.** Not applicable.

## Verification

- Full suite: 142 passed.
- Offscreen only; no hardware, real cameras, ESP32, Uno or high-DPI testing.
