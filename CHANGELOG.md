# Change log

## Result interpretation update

- Added a scrollable **Result & next action** tab with evidence-based interpretation, quality reasons, diagnostics, map comparisons and explicit next steps.
- Distinguish measurable, unresolved, unreliable and inconclusive results without calling an unresolved gap zero or diagnosing binding.
- Optional disabled-by-default user target compares the complete diagnostic envelope against a selected maximum; no universal safe mesh gap or EQ6-R tolerance is assumed. Target changes are display-only and do not authorize motion.
- Standalone completion opens the assessment; table-row selection and offline imports expose the same interpretation without changing saved evidence or hardware interlocks.

## Desktop clarity update

- Persistent next-step instructions after connection, star selection, measurement, Stop and faults; distinguish a single-point measurement from an angle map.
- Permanent color/shape keys for camera overlays and map points, readable trajectory legends for samples, commands and before/after fits, and plain-language phase descriptions.
- Visible backlash/quality summary and explanations of inferred movement versus observed motion and diagnostic allowances versus confidence intervals.
- Consistent live/calibrated trajectory units; reset old fits and overlays at each real measurement start, preserve completed plots during preview, and keep missing-data gaps.
- Full recorded centroid path and refitted completed image view; minimum plot height and high-contrast two-column legends.
- 157 local tests passed with warnings treated as errors, including simulator signal-path and display regressions. Hardware verification remains pending; motion control and the estimator are unchanged.

## 0.2.0 — Experimental desktop foundation

### Application scope

- PySide6/pyqtgraph desktop entry point with disconnected, simulation-selected startup; Qt-free measurement core and single device-owning worker.
- Single ASCOM mount/camera adapters with queried capabilities, allowed-rate/ROI/exposure checks, cooperative cancellation and async-only automated slews. Real hardware acceptance remains pending.
- Isolated-star automatic/manual selection, conservative crowded-field/identity gates and explicit reacquisition after loss.
- Single-point measurement, configurable signed-angle maps (default six nonzero points), pose checks, diagnostic plots, baseline import/comparison and user-triggered local refinement.
- Candidate return requires a fresh quality-valid confirmation measurement followed by a second return slew before stationary adjustment with tracking OFF. Explicit finish/reacquisition required.
- Seeded simulator and numerical/mock-device/offscreen GUI tests. These are not hardware certification or statistical coverage proof.
- Versioned JSON results, centroid/command CSV, diagnostics, checkpoint manifests and partial-save attempts; historic results remain readable offline and cannot authorize motion.

### Packaging and documentation

- Setuptools metadata includes both the legacy `ra_backlash` module and `ra_backlash_app` package, Python >=3.11 and the `ra-backlash` desktop command. Development extra supplies pytest; pywin32 is Windows-only.
- Explicit package selection excludes tests/generated runs. Default ignore rules protect local outputs, profiles, credentials and caches without deleting local observations.
- Install/run/test/safety instructions, complete stable-ID feature inventory and gated roadmap. Windows offscreen test CI is defined for Python 3.11 and 3.14; hosted CI execution is not implied by this entry.
- Disable unrelated pytest-flask auto-loading for the Qt `app` fixture; Flask is not an application dependency.

### Analysis compatibility and limits

- New engine identifies calibration versus measurement transitions: three cycles normally yield **six** measured reversals, unlike historic aggregates that can include a seventh calibration transition. Schema version 2 / `drift-line-jump/2-role-filtered` records this distinction; values need not exactly reproduce old aggregates.
- Missing calibration/latency, inadequate fits, identity loss and negative/unresolved estimates are not valid candidate minima. The reported uncertainty is a conservative diagnostic allowance, **not a calibrated confidence interval**. Residual noise and reversal spread remain separate diagnostics.
- The original CLI is separate and retains legacy behavior; this packaging/docs change does not add desktop safety protections to it. Its hardware operations remain risky/blocking and are unverified.
- Raw camera image recording/replay, desktop PNG/map-summary export, adaptive extra cycles, full disturbance simulation, complete uncertainty validation and physical hardware acceptance remain incomplete. See [FEATURES.md](FEATURES.md) and [ROADMAP.md](ROADMAP.md).
- No standalone installer, generalized southern/mount geometry, simultaneous cameras, direct vendor SDKs, PHD2 integration, plate solving or binding/current sensing. Autonomous physical mesh adjustment is research only, not an approved implementation task.

No release date, public repository publication, verified hardware support, license grant or authorship attribution is asserted here. No license selected yet.

## 0.1 — Legacy CLI baseline

- Original `measure`, `map` and `simulate` workflows with CSV/JSON and matplotlib plotting remain available through the legacy module/script.
- Historical output may lack modern quality/uncertainty/pose evidence. Legacy import labels missing quality conservatively and does not reconstruct images from centroid data.