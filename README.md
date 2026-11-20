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