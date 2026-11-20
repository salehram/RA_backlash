"""Engine regression tests: simulator truth is not hardware validation."""
import json
import math

import numpy as np
import pytest

from ra_backlash_app.core import (
    Cancelled, Cancellation, Config, Tracker, analyze, candidate, create_run,
    generate_angles, load_result, measure, plan_points, save_manifest, save_result,
)
from ra_backlash_app.devices import open_devices


def simulated(cfg=None, seeing=.1):
    cfg = cfg or Config(partial_on_error=False)
    cancel = Cancellation()
    mount, cam, clock = open_devices(True, cfg, cancel, seeing=seeing)
    tracker = Tracker(cam, clock, cfg, cancel)
    assert tracker.acquire()
    return cfg, cancel, mount, cam, clock, tracker


@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("rotation,scale", [(33., .6), (123., 1.5)])
def test_truth_and_exactly_six_measurement_reversals(sign, rotation, scale):
    cfg = Config(sim_sign=sign, sim_rotation=rotation, sim_scale=scale,
                 sim_backlash=12., partial_on_error=False)
    _, cancel, mount, cam, clock, tracker = simulated(cfg)
    events = []
    result = measure(mount, tracker, clock, cfg, cancel, events.append)
    assert result["backlash"] == pytest.approx(12., abs=.8)
    assert result["quality"] == "valid"
    assert result["n_total"] == result["n_good"] == 6
    assert len(result["latency_samples"]) == 2
    assert len(result["u"]) == 2
    assert np.linalg.norm(result["u"]) == pytest.approx(1)
    assert 0 < result["uncertainty_arcsec"] < 4
    assert result["cadence_s"] == pytest.approx(.14, abs=.001)
    assert all(t["k"] >= 5 for t in result["_raw"]["trans"] if t["role"] == "measurement")
    assert any(e["type"] == "calibration" and "v0" in e for e in events)
    assert mount.get_tracking() is True
    assert mount.w.move == 0


def test_default_seeing_and_correlated_seeing():
    for correlation in (0., .9):
        cfg = Config(sim_backlash=12, sim_correlation=correlation, partial_on_error=False)
        _, cancel, mount, cam, clock, tracker = simulated(cfg, seeing=.8)
        result = measure(mount, tracker, clock, cfg, cancel)
        assert abs(result["backlash"] - 12) < 3
        assert result["uncertainty_arcsec"] > abs(result["backlash"] - 12)
        assert "confidence" in result["notes"][0]


def test_zero_backlash_is_not_a_qualified_tight_spot():
    cfg = Config(sim_backlash=0., partial_on_error=False)
    _, cancel, mount, cam, clock, tracker = simulated(cfg, seeing=.8)
    result = measure(mount, tracker, clock, cfg, cancel)
    assert result["quality"] != "valid"
    assert candidate([result]) is None


def test_measured_correlated_baseline_withholds_candidate(monkeypatch):
    from ra_backlash_app import core

    cfg = Config(cal=12., sim_scale=2., sim_backlash=12., partial_on_error=False)
    _, cancel, mount, cam, clock, tracker = simulated(cfg, seeing=.01)
    original_grab, original_analyze = tracker.grab, core.analyze
    phase = []
    analyzed_quality = []

    def event(e):
        if e["type"] == "phase":
            phase[:] = [e["phase"]]

    def grab():
        t, x, y = original_grab()
        # Smooth deterministic residuals only in the drift baseline. This
        # exercises the measured diagnostic, not a mocked correlation value.
        if phase == ["drift"]:
            x += .15 * np.sin(2 * np.pi * t / 2.)
        return t, x, y

    def analyze(*args, **kwargs):
        result = original_analyze(*args, **kwargs)
        analyzed_quality.append(result["quality"])
        return result

    monkeypatch.setattr(tracker, "grab", grab)
    monkeypatch.setattr(core, "analyze", analyze)
    result = measure(mount, tracker, clock, cfg, cancel, event)
    assert analyzed_quality == ["valid"]
    assert result["baseline_lag1"] > .5
    assert result["calibration_fractional_allowance"] < .1
    assert result["backlash"] > result["uncertainty_arcsec"]
    assert result["quality"] == "uncertain"
    assert candidate([result]) is None
    assert any("candidate ineligible" in note.lower() for note in result["notes"])


def test_missing_latency_is_invalid_not_zero():
    cfg, cancel, mount, cam, clock, tracker = simulated()
    result = measure(mount, tracker, clock, cfg, cancel)
    raw = result["_raw"]
    # Remove frames in fast latency step. Preserve actual commands to test the
    # failed-fit path, rather than altering the model's schedule.
    start, end = raw["cmds"][3][0], raw["cmds"][4][0]
    frames = raw["frames"]
    frames = frames[(frames[:, 0] < start) | (frames[:, 0] > end)]
    measured = analyze(frames, [(t, r * result["sign"]) for t, r in raw["cmds"]],
                       raw["t_stop"], result["sign"], result["u"], result["drift_px_s"], cfg)
    assert math.isnan(measured["latency_s"])
    assert measured["quality"] == "invalid"
    assert measured["n_good"] == 0


@pytest.mark.parametrize("phase", ["drift", "sign", "sign_settle", "latency_fast", "latency_slow", "reverse_1", "forward_1"])
def test_cancel_each_phase_stops_and_persists(tmp_path, phase):
    cfg = Config(out=str(tmp_path), sim_backlash=12)
    _, cancel, mount, cam, clock, tracker = simulated(cfg)
    mount.tracking(False)

    def event(e):
        if e["type"] == "phase" and e["phase"] == phase:
            cancel.cancel()

    with pytest.raises(Cancelled) as caught:
        measure(mount, tracker, clock, cfg, cancel, event)
    error = caught.value
    assert error.partial_path.exists()
    saved = load_result(error.partial_path)
    assert saved["status"] == "cancelled"
    assert saved["quality"] == "invalid"
    assert saved["initial_tracking"] is False
    assert mount.get_tracking() is False
    assert mount.w.move == 0
    assert not any(k == "move" and v != 0 for _, k, v in mount.w.pending)
    assert error.partial_path.with_name("partial_frames.csv").exists()


def test_cancel_during_sample_saves_that_sample(tmp_path):
    cfg = Config(out=str(tmp_path))
    _, cancel, mount, cam, clock, tracker = simulated(cfg)
    def event(e):
        if e["type"] == "sample":
            cancel.cancel()
    with pytest.raises(Cancelled) as caught:
        measure(mount, tracker, clock, cfg, cancel, event)
    assert len(caught.value.partial_result["_raw"]["frames"]) == 1
    assert mount.get_tracking() is True


def test_cleanup_failure_does_not_restore_tracking():
    cfg, cancel, mount, cam, clock, tracker = simulated()
    original = mount.stop
    calls = []
    def stop():
        calls.append(True)
        if len(calls) > 1:
            raise RuntimeError("stop failure")
        original()
    mount.stop = stop
    def event(e):
        if e["type"] == "sample":
            cancel.cancel()
    with pytest.raises(Cancelled) as caught:
        measure(mount, tracker, clock, cfg, cancel, event)
    assert "stop failure" in caught.value.partial_result["cleanup_errors"][0]
    assert mount.get_tracking() is False


class ImageCamera:
    W = H = 200
    max_adu = 65535
    def __init__(self):
        self.roi = (0, 0, self.W, self.H)
        self.stars = [(70., 80., 2000.), (120., 80., 5000.)]
        self.t = 0
    def set_roi(self, *roi):
        self.roi = roi
    def expose(self, clock, exposure):
        self.t += .14
        x0, y0, w, h = self.roi
        yy, xx = np.mgrid[y0:y0 + h, x0:x0 + w]
        image = np.full((h, w), 100.)
        for x, y, amplitude in self.stars:
            image += amplitude * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / 8)
        image += np.random.default_rng(42).normal(0, 2, image.shape)
        return self.t, image, (x0, y0)


def sparse_camera(shape, stars):
    """Main-camera fixture without full-frame coordinate grids/temporaries."""
    h, w = shape
    cam = ImageCamera()
    cam.W, cam.H = w, h
    image = np.empty(shape, dtype=np.float32)
    rng = np.random.default_rng(42)
    for y in range(0, h, 256):
        rows = image[y:y + 256]
        rows[:] = 100 + 2 * rng.standard_normal(rows.shape, dtype=np.float32)
    for x, y, amplitude, fwhm in stars:
        ix, iy = round(x), round(y)
        x0, x1 = max(0, ix - 12), min(w, ix + 13)
        y0, y1 = max(0, iy - 12), min(h, iy + 13)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        image[y0:y1, x0:x1] += amplitude * np.exp(
            -((xx - x) ** 2 + (yy - y) ** 2) / (2 * (fwhm / 2.355) ** 2))
    cam.expose = lambda clock, exposure: (1., image, (0, 0))
    return cam, image


@pytest.mark.parametrize("shape", [(3000, 3000), (4176, 6248)])
def test_main_camera_acquisition_uses_bounded_median_tiles(monkeypatch, shape):
    from ra_backlash_app import core

    h, w = shape
    selected = (w - 221.3, h - 189.6)
    # Median filtering attenuates a narrow PSF more than a broad one; keep the
    # narrow star unambiguously brightest after filtering, not only in raw ADU.
    cam, image = sparse_camera(shape, [(*selected, 10000., 2.), (89., 91., 1800., 4.)])
    original = core.legacy.median3
    calls = []

    def bounded_median(sub):
        assert max(sub.shape) <= 528
        calls.append(sub.shape)
        return original(sub)

    monkeypatch.setattr(core.legacy, "median3", bounded_median)
    tracker = Tracker(cam, None, Config(), Cancellation())
    assert tracker.acquire()
    assert tracker.selected == pytest.approx(selected, abs=.15)
    assert len(calls) >= math.ceil(h / 512) * math.ceil(w / 512)
    assert image.dtype == np.float32  # Detection did not promote the input frame.


@pytest.mark.parametrize("position", [(511.4, 512.3), (512., 300.), (1024., 1024.)])
def test_narrow_star_on_tile_boundary_is_selected_once(position):
    cam, image = sparse_camera((1200, 1400), [(*position, 5000., 2.)])
    tracker = Tracker(cam, None, Config(), Cancellation())
    peaks = tracker._full_frame_candidates(image)
    assert len(peaks) == 1
    assert tracker.acquire()
    assert tracker.selected == pytest.approx(position, abs=.15)


def test_tile_boundary_crowding_uses_neighbors_outside_shortlist(monkeypatch):
    # The companion is across the tile seam and deliberately absent from the
    # proposal list, as can happen with per-tile/global peak limits.
    cam, image = sparse_camera((900, 1200), [
        (505., 500., 5000., 4.), (535., 500., 1000., 4.), (900., 700., 2000., 4.)])
    tracker = Tracker(cam, None, Config(), Cancellation())
    monkeypatch.setattr(tracker, "_full_frame_candidates",
                        lambda frame: [(505, 500, 100.), (900, 700, 50.)])
    assert tracker.acquire()
    assert tracker.selected == pytest.approx((900., 700.), abs=.15)
    # Manual acquisition still refuses the ambiguous identity.
    assert tracker._detect(image, (505., 500.))[0] is None


def test_full_frame_shortlist_is_bounded_and_does_not_hide_companions():
    stars = []
    for y in range(80, 1100, 120):
        for x in range(80, 1500, 180):
            stars.extend([(x, y, 5000., 3.), (x + 30, y, 300., 3.)])
    cam, image = sparse_camera((1200, 1600), stars)
    tracker = Tracker(cam, None, Config(), Cancellation())
    peaks = tracker._full_frame_candidates(image)
    assert len(peaks) == 128
    assert not tracker.acquire()


@pytest.mark.parametrize("manual", [False, True])
def test_cancellation_inside_detection_stops_before_next_median(monkeypatch, manual):
    from ra_backlash_app import core

    cam, _ = sparse_camera((1800, 2000), [(1200., 1200., 5000., 3.)])
    cancel = Cancellation()
    tracker = Tracker(cam, None, Config(), cancel)
    original = core.legacy.median3
    calls = []

    def interrupt(sub):
        calls.append(sub.shape)
        result = original(sub)
        if len(calls) == (1 if manual else 3):
            cancel.cancel()
        return result

    monkeypatch.setattr(core.legacy, "median3", interrupt)
    with pytest.raises(Cancelled):
        tracker.acquire((1200., 1200.) if manual else None)
    assert len(calls) == (1 if manual else 3)
    assert not tracker._locked
    assert tracker.selected is None


def test_nonfinite_pixels_cannot_be_selected_as_stars():
    cam, image = sparse_camera((700, 900), [(600., 500., 5000., 3.)])
    image[100, 100] = np.inf
    image[250, 250] = np.nan
    tracker = Tracker(cam, None, Config(), Cancellation())
    assert tracker.acquire()
    assert tracker.selected == pytest.approx((600., 500.), abs=.15)
    assert tracker._detect(image, (np.nan, 500.))[0] is None
    image[:] = np.nan
    assert not tracker.acquire()


def test_manual_identity_not_brightest_and_absolute_roi_coordinates():
    cam = ImageCamera()
    frames = []
    tracker = Tracker(cam, None, Config(roi=80), Cancellation(), frames.append)
    assert tracker.acquire((70, 80))
    t, x, y = tracker.grab()
    assert x == pytest.approx(70, abs=.1)
    assert y == pytest.approx(80, abs=.1)
    assert frames[-1]["origin"] != (0, 0)
    assert set(("image", "origin", "t", "x", "y", "snr", "fwhm", "saturated")) <= frames[-1].keys()
    cam.stars = [(120., 80., 5000.)]
    assert math.isnan(tracker.grab()[1])
    with pytest.raises(RuntimeError, match="reacquisition"):
        tracker.grab()


def test_saturation_and_hot_pixel_rejected():
    cam = ImageCamera()
    cam.stars = [(70, 80, 100000)]
    tracker = Tracker(cam, None, Config(), Cancellation())
    assert not tracker.acquire()
    cam.stars = []
    assert not tracker.acquire()


def test_stable_identity_does_not_jump_to_nearby_replacement():
    cam = ImageCamera()
    cam.stars = [(70., 80., 2000.)]
    tracker = Tracker(cam, None, Config(roi=100), Cancellation())
    assert tracker.acquire((70, 80))
    for _ in range(5):
        assert np.isfinite(tracker.grab()).all()
    # Within acquisition radius but outside stable prediction gate.
    cam.stars = [(90., 80., 2000.)]
    assert math.isnan(tracker.grab()[1])


@pytest.mark.parametrize("search_radius,separation", [(24., 15.6), (24., 23.), (32., 30.), (40., 50.)])
@pytest.mark.parametrize("manual", [False, True])
def test_ambiguous_pair_cannot_lock_then_hop_after_disappearance(search_radius, separation, manual):
    cam = ImageCamera()
    cam.stars = [(70., 80., 2000.), (70. + separation, 80., 2000.)]
    tracker = Tracker(cam, None, Config(search_radius=search_radius), Cancellation())
    # Equal photometry cannot distinguish these identities inside overlapping
    # search envelopes, even when the old fixed 15px crowding test passed.
    assert not tracker.acquire((70., 80.) if manual else None)
    cam.stars = cam.stars[1:]
    with pytest.raises(RuntimeError, match="reacquisition"):
        tracker.grab()


def test_auto_acquisition_skips_ambiguous_pair_for_isolated_star():
    cam = ImageCamera()
    cam.stars = [(70., 80., 5000.), (85.6, 80., 5000.), (150., 140., 2000.)]
    tracker = Tracker(cam, None, Config(), Cancellation())
    assert tracker.acquire()
    assert tracker.selected == pytest.approx((150., 140.), abs=.1)
    # A click on the ambiguous pair must not silently select the isolated star.
    manual_cam = ImageCamera()
    manual_cam.stars = list(cam.stars)
    manual = Tracker(manual_cam, None, Config(), Cancellation())
    assert not manual.acquire((70., 80.))
    for _ in range(5):
        assert tracker.grab()[1:] == pytest.approx((150., 140.), abs=.1)
    cam.stars = cam.stars[:2]
    assert math.isnan(tracker.grab()[1])


@pytest.mark.parametrize("transition", [False, True])
@pytest.mark.parametrize("separation", [15.6, 30.])
def test_tracking_rejects_new_ambiguous_neighbor_and_latches_loss(transition, separation):
    cam = ImageCamera()
    cam.stars = [(70., 80., 2000.)]
    tracker = Tracker(cam, None, Config(), Cancellation())
    assert tracker.acquire((70., 80.))
    for _ in range(5):
        assert np.isfinite(tracker.grab()).all()
    if transition:
        tracker.phase_changed()
    # Include the audited 15.6px pair and a companion outside even the old
    # transition search crop. Both have overlapping identity envelopes.
    cam.stars.append((70. + separation, 80., 2000.))
    assert math.isnan(tracker.grab()[1])
    cam.stars = cam.stars[1:]
    with pytest.raises(RuntimeError, match="reacquisition"):
        tracker.grab()


def test_all_rates_prevalidated_before_any_motion():
    cfg, cancel, mount, cam, clock, tracker = simulated()
    original = mount.validate_rate
    def reject_latency(rate):
        original(rate)
        if abs(rate) > .015:
            raise ValueError("latency rate unsupported")
    mount.validate_rate = reject_latency
    with pytest.raises(ValueError, match="latency rate"):
        measure(mount, tracker, clock, cfg, cancel)
    assert mount.w.pending == []
    assert mount.w.move == 0
    assert mount.get_tracking() is True


def test_callback_fault_stops_without_resuming_tracking():
    cfg, cancel, mount, cam, clock, tracker = simulated()
    def fail(event):
        if event["type"] == "sample":
            raise RuntimeError("consumer failed")
    with pytest.raises(RuntimeError, match="consumer failed") as caught:
        measure(mount, tracker, clock, cfg, cancel, fail)
    assert caught.value.partial_result["status"] == "failed"
    assert mount.get_tracking() is False
    assert mount.w.move == 0


def test_angles_and_planning_no_silent_reduction():
    assert generate_angles() == [-75, -50, -25, 25, 50, 75]
    assert generate_angles(2, 1.5, 20.25, "positive") == [20.25, 21.75]
    rows = plan_points([-25, 0, 25, 25, 83], 24.7)
    assert len(rows) == 5
    assert [r["status"] for r in rows] == ["ready", "skipped", "ready", "skipped", "skipped"]
    assert not any(r["geometry_verified"] for r in rows)
    assert plan_points([25], -30)[0]["status"] == "skipped"
    assert plan_points([25], 24.7, min_alt=80)[0]["status"] == "skipped"
    with pytest.raises(ValueError):
        generate_angles(4)


@pytest.mark.parametrize("kw", [{"rate":1}, {"exposure":float("nan")}, {"cycles":0},
                                    {"phase":.5}, {"roi":10}, {"binning":1.5}])
def test_bad_configuration(kw):
    with pytest.raises(ValueError):
        Config(**kw).validate()


def test_cancellation_wait_and_reset():
    cfg, cancel, mount, cam, clock, tracker = simulated()
    start = clock.now()
    cancel.wait(.3, clock)
    assert clock.now() == pytest.approx(start + .3)
    cancel.cancel()
    with pytest.raises(Cancelled):
        cancel.wait(0)
    cancel.reset()
    cancel.check()
    with pytest.raises(ValueError):
        cancel.wait(-1)


def test_storage_roundtrip_and_unique_runs(tmp_path):
    result = dict(backlash=12., quality="valid", uncertainty_arcsec=2.,
                  noise_arcsec=float("nan"), u=[1., 0.],
                  _raw=dict(frames=np.array([[1., 2., 3.]]), cmds=[(1, -2)]))
    folder = create_run(tmp_path)
    assert create_run(tmp_path) != folder
    path = save_result(result, folder, "point_25.25", {"angle":25.25, "quality":"invalid"})
    loaded = load_result(path)
    assert loaded["quality"] == "valid"  # metadata cannot override analysis
    assert loaded["angle"] == 25.25
    assert loaded["noise_arcsec"] is None
    assert "_raw" not in loaded
    with pytest.raises(FileExistsError):
        save_result(result, folder, "point_25.25", {})
    with pytest.raises(ValueError):
        save_result(result, folder, "../escape", {})
    manifest = save_manifest(folder, [loaded], "cancelled")
    assert json.loads(manifest.read_text())["status"] == "cancelled"


def test_legacy_reader_and_candidate_quality_filter(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text('{"backlash": 1, "spread": 0.1}')
    assert load_result(path)["quality"] == "uncertain"
    rows = [dict(angle=25, backlash=10, quality="valid", uncertainty_arcsec=3),
            dict(angle=50, backlash=12, quality="valid", uncertainty_arcsec=3),
            dict(angle=-25, backlash=0, quality="invalid", uncertainty_arcsec=.1),
            dict(angle=75, backlash=-1, quality="valid", uncertainty_arcsec=.1)]
    selected = candidate(rows)
    assert selected["angle"] == 25
    assert selected["tied"] is True
    assert selected["candidate_angles"] == [25, 50]
    assert candidate([load_result(path)]) is None