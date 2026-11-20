# Roadmap and release gates

This is a sequence of **proposed milestones, not dates or promises**. v0.2.0 is a functional experimental app, not completion of the approved v1 wish list. Hardware is untested. Feature IDs, per-feature priorities, dependencies, acceptance criteria and unassigned issue placeholders live in [FEATURES.md](FEATURES.md); that inventory is the single status authority. No public issues/releases or successful publication are implied here.

## Milestones

| Milestone | Priority / referenced IDs | Dependencies | Exit / acceptance gate |
| --- | --- | --- | --- |
| v0.2 experimental foundation | P0/P1: BASE-01–BASE-08, BASE-11–BASE-14, BASE-16–BASE-18, BASE-22; current portions of BASE-09/10/15/19/20 | Existing legacy estimator and new desktop engine/adapters | Installable package with legacy module and desktop launcher; simulator/offscreen/mock checks; disconnected simulator-first startup; honest quality and hardware warnings. This is not hardware certification or full v1 acceptance. |
| v1 validated baseline | P0 first: BASE-05–BASE-10, BASE-13/14/16–BASE-21; P1: BASE-15/19/22 | Foundation, simulator expansion and staged equipment access | Close named baseline gaps; documented bias/detection-limit tolerances, no false precision; full stop/identity/calibration/geometry/persistence matrix and hardware acceptance. No safety/identity/valid-calibration/honest-uncertainty gate is deferred to v1.2. |
| v1.1 field usability/distribution | UX-01–UX-07, ENG-01 (P1/P2) | v1 acceptance; capabilities and checkpoints; privacy/release review | Clean-machine distribution/rollback, guided verified setup, safe presets/resume, accessible UI, comparable journals/reports and opt-in redacted support bundles. Complete CI/package/docs/regression/hardware compatibility process. Signing remains optional. |
| v1.2 confidence/efficiency | MET-01–MET-08 (MET-02 P0, others P1/P2) | Validated baseline quality, richer simulator and field reference data | Ensemble/coverage and controlled same-pose studies before new accuracy claims; bounded adaptive mapping/sampling; useful star/cadence recommendations, optional recording/replay, asymmetry and drift diagnostics. Preserve original observations and analysis provenance. |
| v2 separately scoped integrations | INT-01–INT-08 (P1/P2) | Stable core, validated hardware ownership/timing and security/license reviews | Capability-gated integrations with explicit camera/motion ownership, safe failure paths and per-configuration hardware acceptance. No exposed remote control, surprise movement or dual-camera independence claim. |
| Research / no release | RES-01–RES-05 (P3) | Suitable telemetry/actuators/independent references and mechanical safety expertise | Establish observability and independently validated protocols first. Autonomous physical adjustment is research only, not implementation authorization. |

## Baseline gaps that must remain visible

| Gap | IDs | Current boundary | Completion evidence |
| --- | --- | --- | --- |
| Uncertainty validation and adaptive acquisition | BASE-09, MET-02 | Diagnostic allowance, not confidence interval; fixed cycle count; no bounded automatic extra cycles/time. Correlation diagnostics do not establish coverage. | Seeded ensembles across low/zero/large backlash, correlated jitter, timing/calibration errors; explicit detection limits and false-candidate tolerances. Bounded/cancellable sampling and recommendations must preserve calibration. |
| Complete disturbance simulator | BASE-10, MET-08 | Fixed latency, limited star/noise model; optional correlated jitter/companion do not cover the planned failure matrix. | Exposure integration, variable latency/readout, slow drift/periodic error, saturation/hot pixels, dropouts/clouds, ROI edges and controllable slew/abort faults; deterministic regression tolerances. |
| Visualization polish | BASE-15 | Basic trail/reference/inferred bracket, final fit plot, diagnostic table/map allowances exist; not the full planned uncertainty/provisional overlay experience. | Checked unit/ROI/zoom/calibration/provisional-state behavior with clearly separate observed and inferred quantities. |
| Capture/export/metadata completeness | BASE-19, MET-05 | Desktop centroid/command CSV and strict JSON/diagnostics/manifests; no desktop PNG/summary CSV or raw camera image recording. Legacy PNG plotting is separate. | Optional off-by-default bounded raw capture, fuller device/observation metadata and reviewed exports. Later replay/reanalysis must retain originals and cannot fabricate images from legacy centroids. |
| Compatibility and exhaustive acceptance | BASE-20, BASE-21, ENG-01 | Automated simulator/mock/offscreen tests are a foundation, not complete failure coverage or hardware/accuracy evidence. | Expand CLI/backward-format/phase/fault/concurrency/packaging checks, then record staged real-device outcomes and explicit untested combinations. |
| Comparison context | BASE-18, UX-04, MET-06/07 | Basic matching-angle/Dec baseline deltas; load/settings not verified. | Session/load/temperature/configuration journal and controlled repeated same-pose/camera studies, with mismatch warnings. |

## Hardware acceptance sequence (all physical checks pending)

1. **Offline/simulator:** pass automated checks with required Qt dependencies (do not count GUI skips as a pass); inspect loss, invalid calibration, conservative uncertainty, map rows and partial exports. Review driver limits and an independent stopping procedure.
2. **Per-camera bench checks:** one ASCOM camera at a time; full-frame, ROI/bin/exposure limits, real cadence, orientation/coordinate transforms, abort/stop, ownership conflict and disconnect/reconnect. Intended camera examples are not a certified list.
3. **Safe single-point:** on supported GSS/GEM equipment, check tracking/MoveAxis signs, small calibration/reversal moves and repeated same-pose estimates. Exercise Stop during exposure, rate drive, acquisition and slew; verify actual stopping with native controls available. Never infer safety from a software acknowledgement alone.
4. **Known geometry:** independently validate northern counterweight-down home/HA/pier and destination-pier relations, requested/achieved pose tolerances, wrap/meridian behavior and full path/cable clearance. HA-derived angle is not an independent encoder measurement.
5. **Map:** one point in each signed group, then six-point default. Check changed Dec/skipped/failed rows, fresh per-field calibration, cancellation/close, partial persistence and stale-pose refusal. Signed groups must never be renamed physical pier sides without evidence.
6. **Candidate/adjustment:** explicit return confirmation, fresh quality-valid saved measurement, second return to the requested pose, actual tracking OFF and stationary interlocks; failures must refuse adjustment. Verify explicit hands/tools-clear finish, reacquisition/remeasurement, reconnect/fault recovery and no return slew on Stop/close.

Hardware availability does not block publishing an honestly labeled experimental source release. It **does** block marking BASE-21 accepted or advertising verified compatibility, physical pose accuracy, calibrated confidence or safe autonomous adjustment.

## Scope preservation / deferred ideas

Every excluded first-version item is retained: executable packaging (**UX-01**), simultaneous cameras (**INT-03**), direct vendor SDKs (**INT-02**), PHD2 (**INT-01**), plate solving (**INT-05**), generalized hemisphere/mount geometry (**INT-06**), binding/current sensing (**RES-01**) and autonomous physical mesh adjustment (**RES-04**). The feature inventory also retains all UX-01–07, ENG-01, MET-01–08, INT-01–08 and RES-01–05 ideas, including network/Alpaca/browser support, DEC measurement, plugins/API, encoder references, worm-phase studies and characterization experiments.

User-triggered bounded local refinement is already in the desktop baseline (BASE-16); autonomous coarse-to-fine mapping is separate (MET-03). Basic optional raw recording was a proposed baseline item and remains incomplete (BASE-19), not retrospectively declared delivered because full replay is scheduled later (MET-05). Core safety, selected identity, valid calibration and honest uncertainty remain release gates, not optional future polish.

## Publication and maintenance gate

| Gate | Required evidence |
| --- | --- |
| Content/privacy | Review staged files **and tracked history** for credentials, personal/site/device data, local paths and raw runs; ignore rules alone do not remove tracked data. Keep local observations on disk; publish only deliberately sanitized test fixtures. |
| Packaging/docs | Build and inspect distributions (legacy module and package present; tests/runs/private data absent), install/launch/test documented paths, review limitations and version/algorithm migration notes. |
| Repository ownership | Verify authenticated GitHub owner, existing repository/remotes/history, visibility and permissions; no inferred ownership from email and no destructive overwrite/force-push or global identity changes. |
| Licensing | No license selected yet; do not invent attribution, a permission grant, or SDK redistribution rights. |
| Publication verification | Separately verify public remote URL, pushed commit, branch and upstream synchronization. Report blockers honestly; local files/CI definitions are not proof that a push or hosted CI run occurred. |
| Issue tracking | Create issues/milestones when authenticated tooling permits; reference stable feature IDs and link assigned issues back to the inventory. Bulk issue creation is not a publication prerequisite. |

Reorder proposed milestones only with explicit priorities and updated acceptance criteria. No automatic opt-in telemetry, default exposed remote control, software-only physical safety or guaranteed accuracy claims.