# Migration: SEC, MAIN_A, MAIN_B

**Status: implemented and guarded.** This document is the A0 audit for the
approach rename. It is deliberately retained as the only place (with the
migration error and guard test) where the retired identifiers remain.

> **Historical audit.** The inventory below describes the pre-rename tree. One
> finding is now out of date: an exact, read-only wire transcript with
> `--show-wire` and **Controls → Wire traffic…** (**Ctrl+Shift+W**) were added
> after this audit. See
> [`README.md`](../README.md) and [`NETWORKING.md`](NETWORKING.md) for current
> behaviour; the statement in section 4 records what existed at audit time.

## 1. Why this is not a find-and-replace

The requested mapping is:

| Old approach | New approach |
| --- | --- |
| `MAIN` | `SEC` |
| `SEC_A` | `MAIN_A` |
| `SEC_B` | `MAIN_B` |

The junction therefore has **two main-road approaches** (`MAIN_A`, `MAIN_B`)
and **one secondary approach** (`SEC`). The old single "main" approach becomes
the secondary one, and the two old side approaches become the main-road pair.

The wire is not renamed. The current firmware accepts only `MAIN` and
`SECONDARY`, so the compat units remain:

| Compat unit | Approaches | Compat wire movement |
| --- | --- | --- |
| `MAIN` | `MAIN_A`, `MAIN_B` | `MAIN` |
| `SECONDARY` | `SEC` | `SECONDARY` |

**Hazard:** renaming the approach `SEC` to the wire movement `MAIN` would send
its request to the wrong lamp group. The adapter maps units, not approaches:
the new `SEC` approach belongs to the `SECONDARY` unit and sends
`"movement":"SECONDARY"`. The new `MAIN_A`/`MAIN_B` approaches belong to the
`MAIN` unit and send `"movement":"MAIN"`.

## 2. Complete mapping as planned

| Identifier class | Old | New |
| --- | --- | --- |
| Approach ids | `MAIN`, `SEC_A`, `SEC_B` | `SEC`, `MAIN_A`, `MAIN_B` |
| Camera ids (config) | `cam_main`, `cam_sec_a`, `cam_sec_b` | `cam_sec`, `cam_main_a`, `cam_main_b` |
| Camera sources | unchanged | unchanged; only id/approach labels change |
| Compat units | `MAIN`=[`MAIN`]; `SECONDARY`=[`SEC_A`,`SEC_B`] | `MAIN`=[`MAIN_A`,`MAIN_B`]; `SECONDARY`=[`SEC`] |
| Compat wire movements | `MAIN`, `SECONDARY` | unchanged: `MAIN`, `SECONDARY` |
| Three-phase units | `MAIN`, `SEC_A`, `SEC_B` | `MAIN_A`, `MAIN_B`, `SEC` |
| Three-phase proposed movements | `MAIN`, `SECONDARY_A`, `SECONDARY_B` | `MAIN_A`, `MAIN_B`, `SECONDARY` |
| Aggregation target | secondary group | `MAIN` unit (`MAIN_A` + `MAIN_B`), sum or max |
| UI identity tokens | `approach-main`, `approach-sec-a`, `approach-sec-b` | `approach-main-a`, `approach-main-b`, `approach-sec` |

The three-phase movement table remains a proposal, disabled by default, and is
not accepted by current firmware.

## 3. Behaviour changes and open questions

- **By design**, the engine now groups `MAIN_A` + `MAIN_B` into the `MAIN`
  unit, and `SEC` alone into `SECONDARY`. Old results must not be compared as
  if the same approaches were scored.
- **Physical wiring must be confirmed.** The compat mapping assumes the Uno's
  `MAIN` outputs drive both main-road heads and its side outputs drive `SEC`.
  If that is not the wiring, stop and correct the config before sending
  requests. This is an open question for the user.
- **Two main approaches may be green together in compat mode.** A `MAIN` green
  serves `MAIN_A` and `MAIN_B` at once. The three-phase proposal assumes one
  approach green at a time. This difference is recorded, not resolved here; the
  user must confirm which is true for the junction.
- **Tile order assumption:** `MAIN_A`, `MAIN_B`, `SEC`, schematic. This is an
  assumption to confirm with the user.
- Old configs are **refused**, never silently translated. The error explains
  the role swap and points here.
- Old persisted UI camera ids are ignored and the default focus is used.

## 4. Audit of the starting tree

Prompt 4's GUI fixes and the Prompt 4 follow-up are committed (`fc0e403`,
`a6aaaef`). The test suite was green before this pass.

**Prompt 5 was not present.** There was no `wire_monitor`, wire-log record, wire
report tool, or wire-monitor UI in the pre-rename tree. The inventory therefore
covers the existing decision log, exporters, mock gateway and UI; Part C's
wire-monitor step was recorded as not applicable rather than inventing a monitor
at that time. An exact read-only wire view was added later (see the historical
note above). No new wire message or protocol behaviour is introduced by the
rename itself.

The inventory below was generated from the pre-rename tree, excluding `.git`,
`.venv`, caches, runtime logs and screenshots. It covers code,
`config.example.yaml`, tests, docs, README and UI strings/tokens. Matching
old names appear in historical audit text and in the tests that deliberately
exercise the rejection path; both are allowlisted in the guard.

## 5. Inventory summary

| Category | Hits | Files |
| --- | ---: | ---: |
| Old approach ids (`SEC_A`, `SEC_B`) | 152 | 19 |
| Old camera ids (`cam_main`, `cam_sec_a`, `cam_sec_b`) | 41 | 11 |
| Old proposed movements (`SECONDARY_A`, `SECONDARY_B`) | 37 | 13 |
| Old UI identity tokens | 15 | 3 |
| `MAIN` used as an approach in config/tests | 22 | 13 |

The key non-obvious occurrences are:

| Area | Occurrences | Required action |
| --- | --- | --- |
| `gateway/adapter.py` | `THREE_PHASE_UNITS`, proposed map, docstring | New units and proposal names; compat map unchanged |
| `app/config.py` | compat validation, three-phase validation, proposed-name error | Accept new units; reject old approach/camera ids with migration message |
| `decision/settings.py` | aggregation comment | Describe `MAIN` unit |
| `main.py` | legacy demo camera/unit and mock warning | Use `SEC`/`MAIN_A` demo and new proposal example |
| `app/orchestrator.py` | log entry mode/units/approaches | Add `approach_layout: v2`; keys come from config |
| `app/exporters.py` | dynamic approach/unit columns | No hard-coded names; old logs remain readable |
| `ui/schematic.py`, `ui/theme.py` | labels, unit colours, tokens | Redraw T, new labels/colours |
| `ui/main_window.py`, `tools/capture_ui.py` | tile order, tree, generated config | New order, grouped tree, new ids |
| `docs/*`, `README.md` | layout tables/prose | Update all current docs; keep migration history explicit |
| `tests/*` | fixtures and expected names | Rename except explicit old-config rejection/guard tests |

## 6. Detailed inventory (pre-rename)


### old approach IDs (152 hits, 19 files)

`README.md`
- L8: `cameras watch `MAIN`, `SEC_A` and `SEC_B` over wired Ethernet; the host detects`
- L25: `C2["SEC_A camera"]`
- L26: `C3["SEC_B camera"]`
- L66: `| `compat_two_phase` (default) | `MAIN`, `SECONDARY` (`SEC_A`+`SEC_B`) | `MAIN`, `SECONDARY` | **yes** |`
- L67: `| `three_phase` (opt-in, disabled) | `MAIN`, `SEC_A`, `SEC_B` | `MAIN`, `SECONDARY_A`, `SECONDARY_B` (**proposed**) | **no** |`
- L70: `be fully controlled yet**. The default is a workaround: `SEC_A` and `SEC_B``
- L214: `| `decision.units` | Serving layout: `{id, approaches}` entries. Compat: `MAIN` + `SECONDARY`; three-phase: `MAIN`, `SEC_A`, `SEC_B`. |`
- L386: `only `MAIN`/`SECONDARY`, so the default compat mode merges `SEC_A` and`
- L387: ``SEC_B`; the Uno cannot tell them apart. Three-phase control needs the`

`config.example.yaml`
- L21: `approach: SEC_A`
- L29: `approach: SEC_B`
- L57: `# speaks only MAIN and SECONDARY, so SEC_A and SEC_B share the SECONDARY`
- L63: `approaches: [SEC_A, SEC_B]`
- L69: `#   - {id: SEC_A, approaches: [SEC_A]}`
- L70: `#   - {id: SEC_B, approaches: [SEC_B]}`
- L99: `aggregation: sum       # sum|max for {SEC_A, SEC_B} -> SECONDARY demand`
- L125: `#     SEC_A: SECONDARY_A`
- L126: `#     SEC_B: SECONDARY_B`

`config.yaml`
- L23: `approach: SEC_A`
- L33: `approach: SEC_B`
- L64: `# speaks only MAIN and SECONDARY, so SEC_A and SEC_B share the SECONDARY`
- L70: `approaches: [SEC_A, SEC_B]`
- L76: `#   - {id: SEC_A, approaches: [SEC_A]}`
- L77: `#   - {id: SEC_B, approaches: [SEC_B]}`
- L106: `aggregation: sum       # sum|max for {SEC_A, SEC_B} -> SECONDARY demand`
- L132: `#     SEC_A: SECONDARY_A`
- L133: `#     SEC_B: SECONDARY_B`

`decision/engine.py`
- L8: `(three units for a three-phase T junction, two units with ``SEC_A``/``SEC_B```

`decision/settings.py`
- L63: `# compat mode: how {SEC_A, SEC_B} demand becomes one SECONDARY demand`

`docs/DECISION.md`
- L28: `- {id: SECONDARY, approaches: [SEC_A, SEC_B]}`
- L35: `- {id: SEC_A, approaches: [SEC_A]}`
- L36: `- {id: SEC_B, approaches: [SEC_B]}`
- L45: `- three-phase mode requires exactly `MAIN`/`SEC_A`/`SEC_B`, is disabled by`
- L177: `| `three_phase` (opt-in) | `MAIN`, `SEC_A`, `SEC_B` | `MAIN`, `SECONDARY_A`, `SECONDARY_B` (**proposed**) | **no** |`

`docs/FIRMWARE_THREE_PHASE_PROPOSAL.md`
- L23: `| `SEC_A` | side road A | one |`
- L24: `| `SEC_B` | side road B | one |`
- L48: `| `SEC_A` | `SECONDARY_A` |`
- L49: `| `SEC_B` | `SECONDARY_B` |`
- L71: `Gateway -> Uno:  REQ SEC_A 18`
- L72: `Gateway -> Uno:  REQ SEC_B 12`
- L141: `Proposed fixed round-robin order: `MAIN` → `SEC_A` → `SEC_B` → `MAIN` …`
- L179: ``SEC_A`/`SEC_B` internally under its own cycle so the one-green invariant`
- L188: `- Run a fixed round-robin over `MAIN`, `SEC_A`, `SEC_B` with default green`
- L202: `| compat_two_phase (default) | current two-approach | Supported today. `SEC_A` and `SEC_B` are merged into `SECONDARY`; the Uno cannot tell them apart. |`

`docs/FOLLOWUP_AUDIT.md`
- L7: `## 1. Where `SEC_A` and `SEC_B` are merged today`
- L13: `| Config | `app/config.py::build_config` | Reads top-level `approaches: {approach_id: movement}` and only accepts `MAIN`/`SECONDARY`. Both `SEC_A` and `SEC_B` map to `SECONDARY`. |`
- L30: `SEC_A: SECONDARY`
- L31: `SEC_B: SECONDARY`
- L62: `- `APPROACHES` maps `SEC_A`/`SEC_B` to `SECONDARY`; `engine()` builds the`
- L123: `2. **The compat merge is hard-wired.** `{SEC_A, SEC_B} -> SECONDARY` exists in`
- L160: `- `MAIN`, `SEC_A`, `SEC_B` are approaches in the target layout, one camera each.`

`docs/UI_DESIGN.md`
- L70: `| 3 x 3 dense grid | Adapted to 2 x 2 with 2 px gaps: MAIN, SEC_A, SEC_B and the T-junction schematic. Focus mode gives one large tile plus two small. |`
- L234: `| Source fps MAIN / SEC_A / SEC_B | 14.89 / 14.97 / 14.85 | 14.94 / 14.94 / 14.94 |`
- L235: `| Processed fps MAIN / SEC_A / SEC_B | 12.28 / 12.50 / 12.73 | 12.79 / 12.59 / 12.71 |`

`gateway/adapter.py`
- L11: `is also its wire movement. ``SEC_A``/``SEC_B`` may share the ``SECONDARY```
- L40: `THREE_PHASE_UNITS: tuple[str, ...] = ("MAIN", "SEC_A", "SEC_B")`
- L46: `"SEC_A": "SECONDARY_A",`
- L47: `"SEC_B": "SECONDARY_B",`

`tests/test_adapter.py`
- L36: `{"MAIN": "MAIN", "SEC_A": "SECONDARY_A", "SEC_B": "SECONDARY_B"},`
- L37: `["MAIN", "SEC_A", "SEC_B"],`
- L59: `assert adapter.to_wire(recommendation("SEC_A", 20)).movement == "SECONDARY_A"`
- L68: `{"MAIN": "MAIN", "SEC_A": "SECONDARY_A", "SEC_B": "SECONDARY_B"},`
- L69: `["MAIN", "SEC_A", "SEC_B"],`
- L78: `{"MAIN": "MAIN", "SEC_A": "SECONDARY_A"},`
- L79: `["MAIN", "SEC_A"],`
- L92: `{"MAIN": "MAIN", "SEC_A": "SIDE", "SEC_B": "SIDE"},`
- L93: `["MAIN", "SEC_A", "SEC_B"],`
- L100: `compat_adapter().to_wire(recommendation("SEC_A"))`
- L105: `message = adapter.build_message(recommendation("SEC_B", 15), sequence=104)`

`tests/test_config.py`
- L32: `ServingUnit("SECONDARY", ("SEC_A", "SEC_B")),`
- L110: `_camera(id="cam_sec", approach="SEC_A"),`
- L120: `decision={"units": _units(("MAIN", ("MAIN", "SEC_A")))},`
- L141: `cameras=[_camera(), _camera(approach="SEC_A")],`
- L145: `("SECONDARY", ("SEC_A",)),`
- L213: `("SEC_A", ("SEC_A",)),`
- L214: `("SEC_B", ("SEC_B",)),`
- L218: `"SEC_A": "SECONDARY_A",`
- L219: `"SEC_B": "SECONDARY_B",`
- L226: `_camera(id="cam_sec_a", approach="SEC_A"),`
- L227: `_camera(id="cam_sec_b", approach="SEC_B"),`
- L250: `assert [unit.id for unit in config.units] == ["MAIN", "SEC_A", "SEC_B"]`
- L263: `del raw["gateway"]["three_phase"]["movements"]["SEC_B"]`
- L297: `"SEC_A": "SIDE",`
- L298: `"SEC_B": "SIDE",`

`tests/test_decision_engine.py`
- L13: `ServingUnit("SECONDARY", ("SEC_A", "SEC_B")),`
- L17: `ServingUnit("SEC_A", ("SEC_A",)),`
- L18: `ServingUnit("SEC_B", ("SEC_B",)),`
- L51: `"SEC_A": metrics("SEC_A", now),`
- L52: `"SEC_B": metrics("SEC_B", now),`
- L70: `SEC_A={"queue": 1},`
- L89: `SEC_A={"queue": 3},`
- L90: `SEC_B={"queue": 1},`
- L95: `assert decision.unit_scores == {"MAIN": 4.0, "SEC_A": 3.0, "SEC_B": 1.0}`
- L100: `snapshots = feed(100.0, MAIN={"queue": 5}, SEC_A={"queue": 4}, SEC_B={"queue": 0})`
- L107: `three.unit_scores["SEC_A"] + three.unit_scores["SEC_B"]`
- L142: `feed(100.0, MAIN={"queue": 12}, SEC_B={"healthy": False}), 100.0`
- L152: `stale["SEC_A"] = metrics("SEC_A", 100.0, timestamp=90.0)`
- L160: `feed(100.0, MAIN={"queue": 12}, SEC_A={"healthy": False}), 100.0`
- L168: `# SEC_B is blind while SEC_A is very busy. The merged unit must not be`
- L169: `# scored from SEC_A alone, because that would treat missing data as traffic`
- L176: `SEC_A={"queue": 30},`
- L177: `SEC_B={"healthy": False},`
- L221: `feed(61.0, MAIN={"queue": 20}, SEC_A={"queue": 3}), 61.0`
- L242: `feed(100.0, MAIN={"queue": 5}, SEC_A={"queue": 4}, SEC_B={"queue": 4}),`
- L250: `feed(100.0, MAIN={"queue": 5}, SEC_A={"queue": 4}, SEC_B={"queue": 4}),`
- L260: `feed(100.0, MAIN={"queue": 1}, SEC_A={"queue": 4}, SEC_B={"queue": 4}),`
- L263: `assert decision.unit_scores["SEC_A"] == pytest.approx(4.0)`
- L264: `assert decision.unit_scores["SEC_B"] == pytest.approx(4.0)`
- L265: `assert decision.recommendation.unit_id == "SEC_A"  # tie -> config order`
- L317: `feed(1.0, MAIN={"queue": 20}, SEC_A={"queue": 2}), 1.0`
- L357: `ServingUnit("MAIN", ("SEC_A",)),`
- L364: `ServingUnit("SECONDARY", ("MAIN", "SEC_A")),`

`tests/test_orchestrator.py`
- L106: `_camera("cam_sec_a", "SEC_A", sec_a_clip),`
- L107: `_camera("cam_sec_b", "SEC_B", sec_b_clip),`
- L112: `{"id": "SECONDARY", "approaches": ["SEC_A", "SEC_B"]},`
- L150: `assert set(entry["approaches"]) == {"MAIN", "SEC_A", "SEC_B"}`
- L169: `assert all(entry["approaches"]["SEC_B"]["healthy"] for entry in entries[-3:])`
- L184: `"SEC_A": make_video("sec_a.avi", frames=40, start_x=80, start_y=20, step_y=8, moving=False),`
- L185: `"SEC_B": make_video("sec_b.avi", frames=40, start_x=120, start_y=20, step_y=8, moving=False),`
- L190: `"SEC_A": "SECONDARY_A",`
- L191: `"SEC_B": "SECONDARY_B",`
- L206: `_camera("cam_sec_a", "SEC_A", clips["SEC_A"]),`
- L207: `_camera("cam_sec_b", "SEC_B", clips["SEC_B"]),`
- L212: `{"id": "SEC_A", "approaches": ["SEC_A"]},`
- L213: `{"id": "SEC_B", "approaches": ["SEC_B"]},`
- L229: `counts = {"MAIN": 1, "SEC_A": 4, "SEC_B": 2}`
- L244: `# SEC_A has the most demand, and its proposal name is what goes on the wire.`
- L256: `assert set(entry["units"]) == {"MAIN", "SEC_A", "SEC_B"}`
- L257: `assert set(entry["approaches"]) == {"MAIN", "SEC_A", "SEC_B"}`
- L260: `"SEC_A",`
- L261: `"SEC_B",`
- L266: `assert entry["recommendation"]["unit_id"] == "SEC_A"`

`tests/test_pipeline_isolation.py`
- L54: `pipeline_b = _build_pipeline("cam_b", "SEC_A", idle_path)`
- L84: `assert metrics_b.approach_id == "SEC_A"`

`tests/test_ui_fixes.py`
- L42: `"approach": "SEC_A",`
- L48: `"approach": "SEC_B",`
- L56: `{"id": "SECONDARY", "approaches": ["SEC_A", "SEC_B"]},`

`tests/test_ui_smoke.py`
- L42: `"approach": "SEC_A",`
- L48: `"approach": "SEC_B",`
- L56: `{"id": "SECONDARY", "approaches": ["SEC_A", "SEC_B"]},`
- L137: `assert set(tile.cards) == {"MAIN", "SEC_A", "SEC_B"}`

`tools/capture_ui.py`
- L68: `for name, shift in (("MAIN", 2), ("SEC_A", 5), ("SEC_B", 3))`
- L84: `for approach in ("MAIN", "SEC_A", "SEC_B")`
- L89: `{"id": "SECONDARY", "approaches": ["SEC_A", "SEC_B"]},`
- L122: `{"MAIN": 2, "SEC_A": 5, "SEC_B": 3}[camera.approach]`

`ui/schematic.py`
- L24: `"SEC_A": "approach-sec-a",`
- L25: `"SEC_B": "approach-sec-b",`
- L140: `if highlighted("SEC_A"):`
- L143: `if highlighted("SEC_B"):`
- L148: `self._label(painter, 8, int(junction_y) - 30, "SEC_A", "approach-sec-a")`
- L153: `"SEC_B",`
- L184: `"SEC_A + SEC_B → SECONDARY (compat)",`


### old camera IDs (41 hits, 11 files)

`README.md`
- L122: `- id: cam_main`

`config.example.yaml`
- L11: `- id: cam_main`
- L20: `- id: cam_sec_a`
- L28: `- id: cam_sec_b`
- L39: `# - id: cam_main`
- L49: `# - id: cam_main`

`config.yaml`
- L11: `- id: cam_main`
- L22: `- id: cam_sec_a`
- L32: `- id: cam_sec_b`
- L46: `# - id: cam_main`
- L56: `# - id: cam_main`

`tests/test_config.py`
- L29: `assert config.camera_ids == ("cam_main", "cam_sec_a", "cam_sec_b")`
- L60: `"id": "cam_main",`
- L226: `_camera(id="cam_sec_a", approach="SEC_A"),`
- L227: `_camera(id="cam_sec_b", approach="SEC_B"),`

`tests/test_controller.py`
- L54: `"id": "cam_main",`
- L113: `assert snapshot.cameras[0].camera_id == "cam_main"`
- L221: `lambda: controller.frame("cam_main", max_size=160) is not None,`
- L224: `frame = controller.frame("cam_main", max_size=160)`

`tests/test_orchestrator.py`
- L105: `_camera("cam_main", "MAIN", main_clip),`
- L106: `_camera("cam_sec_a", "SEC_A", sec_a_clip),`
- L107: `_camera("cam_sec_b", "SEC_B", sec_b_clip),`
- L155: `assert set(entry["cameras"]) == {"cam_main", "cam_sec_a", "cam_sec_b"}`
- L172: `assert entries[-1]["cameras"]["cam_main"]["state"] == "LIVE"`
- L205: `_camera("cam_main", "MAIN", clips["MAIN"]),`
- L206: `_camera("cam_sec_a", "SEC_A", clips["SEC_A"]),`
- L207: `_camera("cam_sec_b", "SEC_B", clips["SEC_B"]),`

`tests/test_performance.py`
- L22: `"id": "cam_main",`

`tests/test_ui_controls.py`
- L30: `"id": "cam_main",`

`tests/test_ui_fixes.py`
- L35: `"id": "cam_main",`
- L41: `"id": "cam_sec_a",`
- L47: `"id": "cam_sec_b",`
- L209: `tile = window._tiles["cam_main"]`
- L264: `assert window._tiles["cam_main"]._image is None`

`tests/test_ui_logs.py`
- L98: `"id": "cam_main",`
- L112: `controller._events.publish("camera", "cam_main", "LIVE", "fps 8")`
- L116: `assert window.events_table.item(0, 1).text() == "cam_main"`

`tests/test_ui_smoke.py`
- L35: `"id": "cam_main",`
- L41: `"id": "cam_sec_a",`
- L47: `"id": "cam_sec_b",`
- L79: `assert set(window._tiles) == {"cam_main", "cam_sec_a", "cam_sec_b"}`


### old proposed movements (37 hits, 13 files)

`README.md`
- L67: `| `three_phase` (opt-in, disabled) | `MAIN`, `SEC_A`, `SEC_B` | `MAIN`, `SECONDARY_A`, `SECONDARY_B` (**proposed**) | **no** |`
- L157: `python -m gateway.mock_gateway --movements MAIN,SECONDARY_A,SECONDARY_B`
- L260: ``movement` is `MAIN`/`SECONDARY` in compat mode. `SECONDARY_A`/`SECONDARY_B` are`
- L389: `- **Three-phase movement names are unconfirmed proposals.** `SECONDARY_A` and`
- L390: ``SECONDARY_B` exist only in config, the adapter and the hypothetical mock.`

`app/config.py`
- L509: `f"missing {missing}. Proposed names are SECONDARY_A/SECONDARY_B, "`

`config.example.yaml`
- L125: `#     SEC_A: SECONDARY_A`
- L126: `#     SEC_B: SECONDARY_B`

`config.yaml`
- L132: `#     SEC_A: SECONDARY_A`
- L133: `#     SEC_B: SECONDARY_B`

`docs/DECISION.md`
- L171: `The engine does not know `MAIN`, `SECONDARY`, `SECONDARY_A` or any other wire`
- L177: `| `three_phase` (opt-in) | `MAIN`, `SEC_A`, `SEC_B` | `MAIN`, `SECONDARY_A`, `SECONDARY_B` (**proposed**) | **no** |`

`docs/FIRMWARE_THREE_PHASE_PROPOSAL.md`
- L48: `| `SEC_A` | `SECONDARY_A` |`
- L49: `| `SEC_B` | `SECONDARY_B` |`
- L209: `- [ ] New gateway firmware accepts exactly `MAIN`, `SECONDARY_A`,`
- L210: ``SECONDARY_B` and still accepts legacy `MAIN`/`SECONDARY`.`

`docs/FOLLOWUP_AUDIT.md`
- L151: ``SIDE_A`/`SIDE_B`; the follow-up brief proposes `SECONDARY_A`/`SECONDARY_B`.`
- L161: `- The current firmware accepts only `MAIN`/`SECONDARY`; `SECONDARY_A` and`
- L162: ``SECONDARY_B` are **proposed names**, disabled by default.`

`gateway/adapter.py`
- L46: `"SEC_A": "SECONDARY_A",`
- L47: `"SEC_B": "SECONDARY_B",`

`gateway/mock_gateway.py`
- L367: `"exist yet, e.g. MAIN,SECONDARY_A,SECONDARY_B."`

`main.py`
- L186: `"--movements MAIN,SECONDARY_A,SECONDARY_B or it will reject "`

`tests/test_adapter.py`
- L36: `{"MAIN": "MAIN", "SEC_A": "SECONDARY_A", "SEC_B": "SECONDARY_B"},`
- L59: `assert adapter.to_wire(recommendation("SEC_A", 20)).movement == "SECONDARY_A"`
- L60: `assert adapter.wire_movements == ("MAIN", "SECONDARY_A", "SECONDARY_B")`
- L68: `{"MAIN": "MAIN", "SEC_A": "SECONDARY_A", "SEC_B": "SECONDARY_B"},`
- L78: `{"MAIN": "MAIN", "SEC_A": "SECONDARY_A"},`
- L110: `"movement": "SECONDARY_B",`
- L116: `b'{"type":"TRAFFIC_REQUEST","movement":"SECONDARY_B","green_time":15,'`

`tests/test_config.py`
- L218: `"SEC_A": "SECONDARY_A",`
- L219: `"SEC_B": "SECONDARY_B",`

`tests/test_orchestrator.py`
- L190: `"SEC_A": "SECONDARY_A",`
- L191: `"SEC_B": "SECONDARY_B",`
- L195: `port=0, movements=["MAIN", "SECONDARY_A", "SECONDARY_B"]`
- L245: `assert {item["movement"] for item in requests} == {"SECONDARY_A"}`
- L267: `assert entry["recommendation"]["wire_movement"] == "SECONDARY_A"`


### old UI tokens (15 hits, 3 files)

`docs/UI_DESIGN.md`
- L108: `| `approach-main` | `#5CC8D8` | 5.3:1 on raised |`
- L109: `| `approach-sec-a` | `#C4B5FD` | lightened from `#A78BFA`, which was 3.81:1 on raised |`
- L110: `| `approach-sec-b` | `#F4A3C0` | 6.2:1 on panel |`

`ui/schematic.py`
- L23: `"MAIN": "approach-main",`
- L24: `"SEC_A": "approach-sec-a",`
- L25: `"SEC_B": "approach-sec-b",`
- L147: `self._label(painter, int(cx), 18, "MAIN", "approach-main", center=True)`
- L148: `self._label(painter, 8, int(junction_y) - 30, "SEC_A", "approach-sec-a")`
- L154: `"approach-sec-b",`

`ui/theme.py`
- L36: `"approach-main": "#5CC8D8",`
- L37: `"approach-sec-a": "#C4B5FD",`
- L38: `"approach-sec-b": "#F4A3C0",`
- L72: `"approach-main": "#0E6B7A",`
- L73: `"approach-sec-a": "#5B3FBF",`
- L74: `"approach-sec-b": "#A33B67",`


### MAIN as approach (22 hits, 13 files)

`README.md`
- L123: `approach: MAIN`

`config.example.yaml`
- L12: `approach: MAIN`
- L40: `#   approach: MAIN`
- L50: `#   approach: MAIN`

`config.yaml`
- L12: `approach: MAIN`
- L47: `#   approach: MAIN`
- L57: `#   approach: MAIN`

`main.py`
- L107: `approach="MAIN",`
- L118: `approach="MAIN",`
- L130: `approach="MAIN",`

`tests/test_config.py`
- L61: `"approach": "MAIN",`
- L378: `common = {"id": "cam", "approach": "MAIN"}`

`tests/test_controller.py`
- L55: `"approach": "MAIN",`

`tests/test_exporters.py`
- L119: `"approach": "MAIN",`
- L138: `"approach": "MAIN",`

`tests/test_orchestrator.py`
- L105: `_camera("cam_main", "MAIN", main_clip),`
- L205: `_camera("cam_main", "MAIN", clips["MAIN"]),`

`tests/test_performance.py`
- L23: `"approach": "MAIN",`

`tests/test_ui_controls.py`
- L31: `"approach": "MAIN",`

`tests/test_ui_fixes.py`
- L36: `"approach": "MAIN",`

`tests/test_ui_logs.py`
- L99: `"approach": "MAIN",`

`tests/test_ui_smoke.py`
- L36: `"approach": "MAIN",`

