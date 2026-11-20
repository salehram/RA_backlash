# RA Backlash

**v0.2.0 — experimental functional desktop application. Hardware acceptance has not been performed.**

Measure RA-axis backlash on a German equatorial mount using camera drift calibration, command-latency calibration and repeated RA reversals. The PySide6/pyqtgraph workbench includes a simulator, single-point measurements, signed-angle maps, live camera display, diagnostic plots, saved results and a guarded candidate/adjustment workflow. It is not a finished implementation of every proposed v1 feature.

- [Feature inventory and acceptance criteria](FEATURES.md)
- [Roadmap, priorities and release gates](ROADMAP.md)
- [Change log](CHANGELOG.md)

## Install

Python **3.11 or newer**. For hardware: Windows, the ASCOM Platform, a compatible GSS/ASCOM telescope driver and the camera's ASCOM driver, with compatible process/driver architecture. These are separate installations, not supplied by pip. Simulator/offline use does not require ASCOM; non-Windows hardware control is not implemented.

From a checkout, in PowerShell (no activation or execution-policy change required):

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ra_backlash_app
```

Use another installed Python version >=3.11 if preferred. For a non-editable installation without test tools, replace the editable install command with `python -m pip install .` using the intended environment's interpreter. NumPy, matplotlib, PySide6 and pyqtgraph are runtime dependencies; pywin32 is installed only on Windows.

After activating that environment, either launch works:

```powershell
python -m ra_backlash_app
ra-backlash
```

There is no standalone executable/installer yet. The initial window is **disconnected, with simulation selected**; launch does not connect or move hardware. Each connection dialog defaults to simulation too. Local per-camera exposure/binning/ROI profiles use Qt QSettings outside the repository (typically the Windows user registry); they are not portable equipment validation or motion authorization.

## Try the desktop simulator first

1. Select **Connect devices…**, leave **Simulation — no hardware** checked, and connect.
2. Use **Full frame** or **Live preview**, then **Auto-select / retry** or click an isolated star in the main image. Stop live preview before starting a measurement.
3. Use **Measure here** and accept the explicit motion confirmation. Each run reacquires the selected identity and recalibrates. Inspect quality, direction-separated results, latency, cadence, residual noise and diagnostic allowance.
4. Preview the map before **Start map…**. Defaults request six points: **−75, −50, −25, +25, +50, +75°**, three per signed group, first magnitude/spacing 25°, minimum altitude **25°**. Center is off; zero requires an explicit HA branch and is currently refused. Custom ordered nonzero angles within ±82° are supported.
5. Inspect all requested rows, changed feasible declinations and skipped reasons. Signed groups are **not actual pier sides**. A failed acquisition can be retried manually/automatically at that field or skipped; selected-star loss during measurement stops the run, rather than silently switching identities.

The **Next** panel keeps these steps visible and explains the current phase. Simulation uses accelerated virtual time, so calibration/reversal phases can pass quickly. **Measure here** makes one measurement at the current position; **Start map** performs measurements at multiple angles. No image acquisition or motion starts merely by connecting.

### Read the displays

- **Camera image:** cyan = observed star path; white cross = selected star; gray dashed = calibrated RA direction; orange bracket = inferred backlash after a completed fit. The image key stays visible while star-quality readings update. The bracket is not another observed segment or atmospheric-motion subtraction.
- **Trajectory:** before calibration, cyan/purple show sensor x/y displacement in pixels. After calibration, they show RA/orthogonal displacement in RA-axis-equivalent arcseconds. Gray dotted markers identify command/phase times; green and orange lines are the fits before/after commands—not the image's backlash bracket. All displacements use the first valid sample as the origin. Intentional mount drives account for much of the large trajectory; its full height is **not** the backlash amount.
- **Result summary:** backlash, diagnostic allowance, quality and quality reasons appear above the trajectory rather than only in the log. Completed fits remain visible during preview; every new measurement resets the plot and its units.
- **Map:** cyan = current measurements; purple = saved baseline. Circles show the median, triangles forward-to-reverse, squares reverse-to-forward; whiskers show diagnostic allowances, not confidence intervals. Unknown angles are not connected by invented lines.

Automatic/manual selection rejects saturated, poor-shape, edge and conservatively crowded candidates. Close stars with overlapping identity-search envelopes can be rejected even when visually separable. Tracking uses a bounded prediction/local gate; no global-brightest fallback during measurement. This is conservative identity protection, not an infallible identification guarantee.

The simulator shares the desktop controller and view path. It supports seeded known backlash, both axis signs, scale/rotation, fixed latency, independent or correlated centroid jitter, pixel noise and an optional competing star through engine configuration. It is not a complete atmospheric, exposure-integration or hardware-failure model. Advanced simulator parameters are not all GUI controls.

## Hardware precautions and supported assumptions

**Experimental, untested with real mounts/cameras. Software Stop is not a hardware emergency stop.** A hung COM/driver call, disconnect, ignored command or failed abort can prevent software cleanup. Keep GSS/native mount controls and an independent physical stop procedure immediately available. Read faults/logs and verify actual motion/tracking yourself; a stop request is not proof of stopping.

- Intended initial equipment is an EQ6-R Pro through GSS/ASCOM and **one directly connected ASCOM camera**. ZWO ASI220MM Mini, QHYIII 585MM, ASI533MM and ASI2600MM are intended examples, not certified compatibility claims. Any compatible ASCOM camera may be selected; model capabilities are queried, not assumed. No simultaneous cameras or vendor SDK adapters.
- Close other camera clients. Stop PHD2/other guiding, dithering and competing mount controllers; disable conflicting backlash compensation. Balance/load and cable forces affect what is measured: sustained loading can hide backlash. Record the optical/load configuration for comparisons.
- Connection alone sends no mount movement/tracking commands. Measurement deliberately turns tracking off and drives RA through calibration and reversals. Only one device-owning COM worker performs acquisition and motion; GUI settings are frozen while active, and binning changes require reconnection.
- The adapters check allowed RA-rate intervals, exposure/binning/ROI limits and camera abort/stop support. Automated map/return needs asynchronous slew, usable current/destination pier reporting and operator-validated **northern, counterweight-down GEM geometry**. Unsupported/unknown geometry or capabilities are refused, not guaranteed compatible. Driver-specific ROI alignment constraints can still cause faults; there is no universal driver workaround.
- The displayed/recorded angle is inferred from **hour angle and assumed geometry**, not independently observed encoder truth. Operator validation and driver reports do not prove an exact physical mechanical angle or a collision-free path. Southern/equatorial/generalized poses are not supported for automated mapping. The ±82° boundary and endpoint altitude checks are conservative checks, not collision detection; leave driver limits enabled and check the whole path, pier changes and cables.
- Ordinary safe cancellation attempts to restore workflow-entry tracking after stopping. Aborted slews, faults and adjustment do not blindly restart tracking. Stop/close cancel subsequent tasks and never initiate a return slew. Cleanup or partial-save failure is possible and must be treated as a fault, not success.

### Candidate return and stationary adjustment

A map minimum is only a **measured candidate**, not evidence of binding, a universal zero-backlash target, or full RA-wheel coverage. Only completed quality-valid points qualify; overlapping diagnostic allowances are reported as tied/uncertain candidates. Imported results cannot authorize return motion.

Select a live qualified table row from the current connection and use **Return to selected candidate…**. Explicit confirmation authorizes a slew, fresh star acquisition/calibration and a **confirmation measurement**. A valid saved confirmation is required. Because measurement itself moves RA, a **second slew** recomputes the target from current LST and returns to the requested angle before stopping RA and turning tracking off. Requested versus achieved HA-derived pose/pier/altitude is verified; ambiguous or failed checks refuse adjustment, not silently claim physical positioning.

In **adjustment**, automatic measurement/preview/map motion is interlocked and tracking remains off. The star drifts away; motors can remain energized. There is no automatic mesh adjustment or detection of hands, tools or binding. Follow the mount manufacturer's mechanical procedure. **Finish adjustment / enable tracking…** requires explicit hands/tools-clear confirmation, enables tracking without an automatic slew, and requires reacquisition/calibration for remeasurement.

## Interpret results

After **Measure here**, open **Result & next action** (it opens automatically on completion). It explains the measurement and recommends a next step, independently of the plots:

| Assessment | Meaning / next step |
| --- | --- |
| **Measurable backlash — size target not set** | Positive lost motion is resolved above the diagnostic allowance. Repeat at the same pose, then map several angles before considering mesh tuning. It is not automatically “too loose.” |
| **Not resolved above measurement uncertainty** | The estimate is no greater than its diagnostic allowance. This is **not a measured zero**, and does not prove binding. Improve measurement conditions and repeat; do not keep tightening to chase zero. |
| **Inconclusive / measurement unreliable** | Noise, correlation, missing calibration, loss, interrupted acquisition or other quality failures prevent a size/mesh conclusion. Address the quality reasons first. |
| **Above / within selected target** | Optional comparison against **your** maximum acceptable RA-axis backlash, not a manufacturer tolerance or safe physical mesh gap. Only qualified measurements are compared, and the complete diagnostic envelope must be on the relevant side. |
| **Diagnostic envelope overlaps target** | The evidence cannot distinguish above versus within your goal; repeat rather than assuming a pass/fail. |

The optional target is disabled by default, applies only to interpretation, stays local to the current window and is not saved with measurements. Enabling or changing it does not change the estimator, start a measurement, authorize motion or establish a safe adjustment. No universal EQ6-R backlash limit is assumed.

For example, **13.3 ± 3.3 RA-axis arcsec** represents an inferred gap with a diagnostic envelope of approximately **10.0–16.6 arcsec** (not a calibrated confidence interval). With a valid run it is measurable; a selected target of 5 arcsec would be below the whole envelope, 20 arcsec above it, and 12 arcsec inside it. These are comparison examples, **not recommended settings**. At a 2× sidereal measurement rate, the nominal reversal take-up model gives about **0.44 s**, excluding command latency; this is not a prediction of guiding performance.

Select any measured/imported table row to read its assessment. The map summary compares qualified sampled angles, flags overlapping envelopes and excludes unqualified rows. One point cannot establish the axis's tightest location. Simulation results are educational synthetic data, not evidence about the real mount.

**Binding cannot be diagnosed from this measurement alone.** Small measured backlash, unresolved backlash, balance/preload, friction and inadequate measurement sensitivity can produce similar results. If the mount shows abnormal resistance, stalls or other concerning behaviour, stop testing and investigate using the manufacturer's procedure or a qualified service provider; do not force motion or tighten based only on this number. The tool does not sense motor torque/current or certify binding-free operation.

Results are **RA-axis arcseconds**, not on-sky arcseconds. Tracking-off drift calibrates camera direction and scale; same-direction steps estimate latency separately from reversal dead time. Three cycles normally produce **six measurement reversals**; calibration transitions are excluded in the new engine. This intentionally differs from legacy runs that can aggregate seven transitions.

`valid`, `uncertain` and `invalid` are algorithmic quality classifications, not certification. Missing latency/calibration, inadequate fits, loss of identity, negative signed estimates and unresolved near-zero results are not silently promoted to tight spots. Residual noise and reversal spread are separate from `uncertainty_arcsec`: the latter is a **conservative diagnostic allowance, not a calibrated confidence interval or a promised coverage probability**. Strong measured baseline correlation (lag-1 > 0.5) makes results uncertain and candidate-ineligible pending validated correlation-aware uncertainty. Temporal-correlation and calibration diagnostics do not remove atmospheric/systematic motion. No guaranteed accuracy, seeing subtraction or precise-zero claim is made; ensemble/Monte Carlo coverage validation and bounded adaptive extra cycles remain backlog.

The image shows a cyan observed centroid trail, selected-star marker, calibrated RA reference and a separately anchored orange inferred-backlash bracket. Its length is `B × drift_px_s / S`; it is not an extra observed trail segment. Completed-run plots include command times, pre/post fit extrapolations and an orthogonal diagnostic. Tables/map show directional estimates and diagnostic allowances; not every planned overlay/quality visualization is complete.

## Saved data and offline comparison

The desktop writes new unique run directories beneath the current working directory's `ra_backlash_runs` folder:

| Output | Current contents |
| --- | --- |
| `manifest.json` | Schema version, workflow status and checkpointed point rows, including skipped/failed/cancelled points where the workflow reaches persistence. |
| `<tag>_result.json` | Strict JSON (nonfinite values become null), analysis/settings, quality/diagnostics, UTC save timestamp and available requested/achieved pose metadata; no raw image. |
| `<tag>_frames.csv` | Timestamp and absolute full-frame **binned** centroid x/y, not camera frames. |
| `<tag>_commands.csv` | Command times and sidereal rates; consult the recorded convention, especially on partial failures. |
| `<tag>_diagnostics.json` | Available transition fits, command request/return times and per-frame quality/ROI diagnostics. |

Unique point IDs avoid rounded fractional-angle collisions; an existing result tag is not overwritten. Failed/cancelled measurements attempt partial result/centroid/command/diagnostic export (possibly in a separate run folder referenced by the manifest). Storage failure or a blocked driver can prevent saving: this is not crash-proof archival.

**The desktop saver does not currently export PNGs, a map-summary CSV or raw camera image sequences.** Legacy CLI plotting still provides its existing PNG outputs when plotting succeeds. No raw image recording/replay is implemented in the desktop yet.

**Import saved results…** and **Select saved baseline…** accept result/manifest JSON or legacy summary CSV. Legacy results without modern quality metadata are uncertain; offline imports cannot acquire motion authorization. Historic centroid-only files do not reconstruct camera images or a full raw-frame replay. Basic before/after deltas require matching angle/Dec; load/settings are explicitly **not verified**, so this is not a fully controlled comparison journal.

## Legacy CLI (preserved, separate safety behavior)

The original module remains available from a checkout or installed package:

```powershell
python -m ra_backlash --help
python -m ra_backlash simulate
```

Checkout compatibility is retained for `python ra_backlash.py simulate` and the existing `measure`/`map` subcommands. Use each subcommand's `--help` for arguments. **`measure` and `map` control real hardware, unlike `simulate`.** The desktop console command does not route to the CLI.

**The legacy CLI remains unmodified by this packaging/docs work and does not inherit the new desktop engine's safety protections.** Its hardware path uses blocking slews/exposure polling, can reacquire the brightest object, restores tracking unconditionally and lacks the desktop cancellation, quality-qualified candidate confirmation and adjustment interlocks. Its printed minimum is not safe adjustment authorization. Its default map includes zero, unlike the desktop. Prefer simulator use; do not mistake CLI availability for hardware validation or protected operation.

## Development and verification

Install the `dev` extra as above, then run in PowerShell:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m pytest -q
```

Tests cover numerical/simulator cases, conservative identity handling, mock ASCOM capabilities/abort behavior, persistence and offscreen desktop/controller interlocks. The pytest configuration disables the unrelated `pytest-flask` plugin (`-p no:flask`) because this suite's `app` fixture is a Qt application; Flask is not needed. GUI tests can skip if Qt dependencies are missing, so a skipped GUI suite is not desktop validation. CI installs runtime plus development dependencies on Python 3.11/3.14 with offscreen Qt; CI is not hardware acceptance or proof of statistical coverage.

Local v0.2 verification on Windows / Python 3.14.2: **123 tests passed**, including the desktop suite, with runtime warnings treated as errors. A six-point desktop simulation completed, and a separate simulated candidate-confirmation/second-return/adjustment/resume sequence passed. Acquisition tests include an ASI2600-sized 6248 × 4176 synthetic image using memory-bounded tiled detection. Wheel build/install and desktop startup were also checked. These are software/synthetic checks, not real-camera compatibility or measurement-accuracy certification; hosted CI and Python 3.11 results must be checked separately.

Before real six-point mapping: verify each camera's connection/full-frame/ROI/bin/exposure/cadence, safe single-point behavior, Stop during exposure/rate/slew/acquisition, known home/HA/pier relations, then one point in each signed group. Independently validate candidate confirmation/second return, adjustment/resume, reconnect/fault recovery and repeated same-pose accuracy. These hardware checks are **pending**.

## Privacy and license

Generated runs, images, logs, environments, credentials and local profiles are ignored by default; ignore rules do not sanitize already tracked files/history or arbitrary custom output folders. Runs can contain site/pointing/device data and local paths. Review staged content and history before publication; only deliberately sanitized fixtures should be shared. No automatic telemetry or remote-control service is implemented. Existing local runs are not deleted by packaging.

**No license selected yet.** No permission grant or authorship attribution is implied by public availability.