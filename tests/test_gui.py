"""Offscreen desktop tests. No ASCOM devices, chooser or physical motion."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
import json
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
from PySide6 import QtCore, QtWidgets

from ra_backlash_app import core, devices
from ra_backlash_app.gui import DeviceWorker, FrameSnapshot, MainWindow, backlash_pixels, image_rect


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def until(app, predicate, timeout=30):
    end = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        if time.monotonic() > end:
            pytest.fail("Timed out waiting for Qt worker")
        # Test-only pacing; never used to interrupt a workflow.
        QtCore.QThread.msleep(5)
    app.processEvents()


@pytest.fixture
def window(app, tmp_path):
    settings = QtCore.QSettings(str(tmp_path / "profiles.ini"), QtCore.QSettings.Format.IniFormat)
    widget = MainWindow(start_worker=False, settings=settings)
    yield widget
    widget.close()


def test_offscreen_smoke_and_state_gating(window, app):
    window.show()
    app.processEvents()
    assert window.connect_button.isEnabled()
    assert not window.measure_button.isEnabled()
    assert "SIMULATION" in window.banner.text()
    window.connected = True
    window.can_slew = True
    window._on_ready("idle")
    assert window.measure_button.isEnabled()
    assert window.map_button.isEnabled()
    assert not window.fields["binning"].isEnabled()
    window.pending = True
    window._on_state("preview")
    assert window.auto_button.isEnabled()
    assert not window.measure_button.isEnabled()
    assert not window.settings_box.isEnabled()
    window._on_state("awaiting_star")
    assert window.skip_button.isEnabled()
    window._on_ready("adjustment")
    assert window.finish_button.isEnabled()
    assert not window.measure_button.isEnabled()
    assert not window.map_button.isEnabled()
    assert not window.preview_button.isEnabled()
    window._on_ready("fault")
    assert not window.measure_button.isEnabled()
    assert window.disconnect_button.isEnabled()


def test_coordinate_overlay_conversion():
    assert backlash_pixels(12, 25) == pytest.approx(12 * 25 / core.S_ARCSEC)
    assert np.isnan(backlash_pixels(-1, 25))
    assert np.isnan(backlash_pixels(12, None))
    rect = image_rect((120, 80), (20, 40), 3)
    assert (rect.x(), rect.y(), rect.width(), rect.height()) == (119.5, 79.5, 120, 60)


def test_default_map_and_import_interlock(window, tmp_path):
    assert window._angles() == [-75, -50, -25, 25, 50, 75]
    path = tmp_path / "result.json"
    path.write_text('{"angle":25,"dec":0,"backlash":12,"quality":"valid",'
                    '"uncertainty_arcsec":1,"geometry_verified":true,"connection":"forged"}', encoding="utf-8")
    rows = window.read_saved(path)
    assert rows[0]["imported"]
    assert rows[0]["geometry_verified"] is False
    assert rows[0]["connection"] is None
    window.connected = window.can_slew = True
    window._on_ready("idle")
    window._on_rows(rows)
    window.table.selectRow(0)
    assert not window.candidate_button.isEnabled()


@pytest.mark.parametrize("directional", [False, True])
def test_map_scatter_bounds_only_receive_finite_pairs(window, app, directional):
    rows = [dict(angle=25., backlash=12., fwd_rev=None, rev_fwd=float("nan")),
            dict(angle=50., backlash=13., fwd_rev=float("inf"))]
    if directional:
        rows[1].update(fwd_rev=14., rev_fwd=15.)
    window.baseline = [dict(row) for row in rows]
    window._on_rows(rows)
    # Force deferred bounds calculation even when the offscreen window has
    # not been painted; all-NaN scatter data otherwise warn intermittently.
    for item in window.map_plot.listDataItems():
        for axis in (0, 1):
            item.scatter.dataBounds(axis)
        x, y = item.getData()
        assert np.isfinite(x).all()
        assert np.isfinite(y).all()
    window.map_plot.autoRange()
    app.processEvents()


def test_snapshot_is_immutable_and_simulated_measure(app, tmp_path):
    worker = DeviceWorker()
    events, results, frames, ready, logs = [], [], [], [], []
    worker.event.connect(events.append)
    worker.result.connect(results.append)
    worker.frame.connect(frames.append)
    worker.ready.connect(ready.append)
    worker.log.connect(logs.append)
    cfg = core.Config(out=str(tmp_path), sim_backlash=12)
    worker.start()
    try:
        assert worker.submit("connect", cfg=cfg, simulation=True)
        until(app, lambda: len(ready) == 1)
        assert ready[-1] == "idle", logs
        assert worker.submit("preview", cfg=cfg, continuous=False)
        until(app, lambda: len(ready) == 2)
        assert frames
        snapshot = frames[-1]
        assert not snapshot.image.flags.writeable
        with pytest.raises(ValueError):
            snapshot.image.setflags(write=True)
        assert worker.submit("measure", cfg=cfg)
        assert not worker.submit("map", cfg=cfg, angles=[25], confirmed=True)
        until(app, lambda: len(ready) == 3, 60)
        assert ready[-1] == "completed", logs
        # Conservative correlation diagnostics may honestly withhold validity.
        assert results[-1]["quality"] in {"valid", "uncertain"}, logs
        assert results[-1]["backlash"] == pytest.approx(12, abs=3)
        assert list(tmp_path.rglob("manifest.json"))
        assert any(e["type"] == "pose" for e in events)
    finally:
        worker.shutdown()
        assert worker.wait(10000), "Worker must cooperatively exit, never terminate"


def test_stop_before_dispatch_does_not_get_reset(app, tmp_path):
    worker = DeviceWorker()
    ready = []
    worker.ready.connect(ready.append)
    assert worker.submit("connect", cfg=core.Config(out=str(tmp_path)), simulation=True)
    worker.stop()
    assert worker.cancel._event.is_set()
    # The pending accepted command was removed, not silently restarted.
    assert not worker.submit("connect", cfg=core.Config(), simulation=True)
    worker.shutdown()
    worker.start()
    assert worker.wait(10000)


def test_preview_stop_and_shutdown(app, tmp_path):
    worker = DeviceWorker()
    ready, frames, logs = [], [], []
    worker.ready.connect(ready.append)
    worker.frame.connect(frames.append)
    worker.log.connect(logs.append)
    cfg = core.Config(out=str(tmp_path))
    worker.start()
    try:
        assert worker.submit("connect", cfg=cfg, simulation=True)
        until(app, lambda: len(ready) == 1)
        assert worker.submit("preview", cfg=cfg, continuous=True)
        until(app, lambda: len(frames) >= 2)
        worker.stop()
        until(app, lambda: len(ready) == 2)
        assert ready[-1] == "idle", logs
        assert worker.cancel._event.is_set()
        assert worker.commands.empty()
    finally:
        worker.shutdown()
        assert worker.wait(10000)


def test_map_manual_retry_current_point_and_manifest(app, tmp_path):
    worker = DeviceWorker()
    ready, events, rows, logs = [], [], [], []
    worker.ready.connect(ready.append)
    worker.event.connect(events.append)
    worker.rows_changed.connect(rows.append)
    worker.log.connect(logs.append)
    cfg = core.Config(out=str(tmp_path), sim_backlash=12)
    original = worker._acquire
    attempts = []

    def first_acquire_fails(position=None):
        attempts.append(position)
        return False if len(attempts) == 1 else original(position)

    worker._acquire = first_acquire_fails
    worker.start()
    try:
        assert worker.submit("connect", cfg=cfg, simulation=True)
        until(app, lambda: len(ready) == 1)
        assert worker.submit("map", cfg=cfg, angles=[25], confirmed=True)
        until(app, lambda: any(e["type"] == "awaiting_star" for e in events))
        assert worker.submit("auto_select", cfg=cfg)
        until(app, lambda: len(ready) == 2, 60)
        assert ready[-1] == "completed", logs
        assert len(attempts) == 2
        assert rows[-1][0]["status"] == "completed"
        assert rows[-1][0]["angle"] == 25
        assert rows[-1][0]["geometry_verified"]
        assert list(tmp_path.rglob("manifest.json"))
    finally:
        worker.shutdown()
        assert worker.wait(10000)


@pytest.mark.parametrize("workflow", ["measure", "map"])
@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize("phase", ["drift", "reverse_1"])
def test_cancel_measurement_phase_discards_pending_commands(app, tmp_path, monkeypatch, workflow, initial, phase):
    worker = DeviceWorker()
    ready, logs, poses = [], [], []
    worker.ready.connect(ready.append)
    worker.log.connect(logs.append)
    worker.event.connect(lambda e: poses.append(e) if e["type"] == "pose" else None)
    cfg = core.Config(out=str(tmp_path), sim_backlash=12)
    original_open = devices.open_devices

    def open_with_tracking(*args, **kwargs):
        mount, cam, clock = original_open(*args, **kwargs)
        mount.tracking(initial)
        return mount, cam, clock

    monkeypatch.setattr(devices, "open_devices", open_with_tracking)

    def stop_on_drift(event):
        if event.get("type") == "phase" and event.get("phase") == phase:
            worker.stop()

    # This signal callback manipulates only the cancellation Event and Queue;
    # no Qt widgets, device calls, or queued interrupt slots are involved.
    worker.event.connect(stop_on_drift, QtCore.Qt.ConnectionType.DirectConnection)
    worker.start()
    try:
        assert worker.submit("connect", cfg=cfg, simulation=True)
        until(app, lambda: len(ready) == 1)
        assert worker.submit(workflow, cfg=cfg, angles=[25], confirmed=True)
        until(app, lambda: len(ready) == 2)
        assert ready[-1] == "idle", logs
        assert worker.cancel._event.is_set()
        assert worker.commands.empty()
        assert poses[-1]["tracking"] is initial
        assert list(tmp_path.rglob("partial_result.json"))
    finally:
        worker.shutdown()
        assert worker.wait(10000)


class SafetyMount:
    """Deterministic no-hardware mount; calls retain ordering for safety checks."""
    can_slew = True

    def __init__(self):
        self.on = True
        self.ra = (-core.legacy.ha_from_angle(25)) % 24
        self.dec = 45.
        self.pier = "sim"
        self.calls = []
        self.fail_stop = False
        self.on_slew = None

    def get_tracking(self):
        return self.on

    def tracking(self, on):
        self.calls.append(("tracking", on))
        self.on = bool(on)

    def stop(self):
        self.calls.append(("stop",))
        if self.fail_stop:
            raise RuntimeError("failed zero/abort")

    def slew(self, ra, dec, cancel):
        self.calls.append(("slew", ra, dec))
        self.tracking(True)
        if self.on_slew:
            self.on_slew()
        cancel.check()
        self.ra, self.dec = ra, dec

    def ra_dec(self):
        return self.ra, self.dec

    def lst(self):
        return 0.

    def latitude(self):
        return 45.

    def pier_side(self):
        return self.pier

    def disconnect(self):
        self.calls.append(("disconnect",))


@pytest.fixture
def safety_worker(app, tmp_path, monkeypatch):
    worker = DeviceWorker()
    mount = SafetyMount()
    cfg = core.Config(out=str(tmp_path), min_alt=0, settle=0)
    cam = SimpleNamespace(W=1024, H=1024, disconnect=lambda: None)
    monkeypatch.setattr(devices, "open_devices", lambda *a, **k: (mount, cam, None))
    ready, logs = [], []
    worker.ready.connect(ready.append)
    worker.log.connect(logs.append)
    worker.start()
    try:
        assert worker.submit("connect", cfg=cfg, simulation=True)
        until(app, lambda: len(ready) == 1)
        assert ready[-1] == "idle", logs
        yield worker, mount, ready, logs, cfg
    finally:
        worker.shutdown()
        assert worker.wait(10000)


def candidate_for(worker):
    row = dict(point_id="mapped_000", angle=25., dec=45., quality="valid",
               backlash=12., uncertainty_arcsec=1., status="completed",
               connection=worker._connection, geometry_verified=True,
               # The measurement END angle differs from its requested angle.
               pose=dict(angle=25.2, dec=45., pier="sim"))
    worker._live_results[row["point_id"]] = row
    return row


def mock_confirmation(worker, mount, monkeypatch, quality="valid", fail=None):
    def acquire(position=None):
        mount.calls.append(("acquire", position))
        worker.tracker = SimpleNamespace(_last=None, selected=(512., 512.), on_frame=None)
        return True

    def measure(m, tracker, clock, cfg, cancel, event):
        mount.calls.append(("measure",))
        event(dict(type="phase", phase="drift", duration=4., raw_rate=0))
        m.tracking(False)
        event(dict(type="phase", phase="reverse_1", duration=5., raw_rate=-2))
        if fail:
            fail()
        cancel.check()
        # Deliberately move by > pose tolerance: a second slew is mandatory.
        m.ra = (-core.legacy.ha_from_angle(27)) % 24
        return dict(quality=quality, backlash=12., uncertainty_arcsec=1.,
                    status="completed", cleanup_errors=[], notes=[])

    monkeypatch.setattr(worker, "_acquire", acquire)
    monkeypatch.setattr(core, "measure", measure)
    return acquire


@pytest.mark.parametrize("late_action", ["stop", "shutdown"])
def test_fault_latched_when_stop_arrives_in_final_pose(safety_worker, app, monkeypatch, late_action):
    worker, mount, ready, logs, cfg = safety_worker
    armed = []
    original_pose = worker._pose

    def fail():
        armed.append(True)
        raise RuntimeError("measurement fault")

    def final_pose():
        if armed:
            armed.clear()
            getattr(worker, late_action)()
        return original_pose()

    monkeypatch.setattr(worker, "_measure_here", fail)
    monkeypatch.setattr(worker, "_pose", final_pose)
    assert worker.submit("measure", cfg=cfg)
    until(app, lambda: len(ready) >= 2)
    if late_action == "shutdown":
        assert worker.wait(10000)
    assert all(state == "fault" for state in ready[1:]), logs
    assert worker._fault
    assert not worker.submit("measure", cfg=cfg)
    assert mount.on is False


def test_fault_requires_disconnect_and_successful_reconnect(safety_worker, app, monkeypatch):
    worker, mount, ready, logs, cfg = safety_worker
    worker._state("fault")
    worker.stop()
    until(app, lambda: len(ready) == 2)
    assert ready[-1] == "fault"
    assert not worker.submit("connect", cfg=cfg)
    assert not worker.submit("slew_candidate", cfg=cfg)
    assert worker.submit("disconnect")
    until(app, lambda: len(ready) == 3)
    assert worker._fault and ready[-1] == "disconnected"
    original_open = devices.open_devices

    def failed_open(*a, **k):
        raise RuntimeError("reconnect failed")

    monkeypatch.setattr(devices, "open_devices", failed_open)
    assert worker.submit("connect", cfg=cfg)
    until(app, lambda: len(ready) == 4)
    assert worker._fault and ready[-1] == "fault"
    assert not worker.submit("measure", cfg=cfg)
    assert worker.submit("disconnect")
    until(app, lambda: len(ready) == 5)
    monkeypatch.setattr(devices, "open_devices", original_open)
    assert worker.submit("connect", cfg=cfg)
    until(app, lambda: len(ready) == 6)
    assert not worker._fault and ready[-1] == "idle", logs


@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize("workflow", ["map", "slew_candidate"])
def test_cancel_active_slew_never_restores_tracking(safety_worker, app, initial, workflow):
    worker, mount, ready, logs, cfg = safety_worker
    mount.on = initial
    candidate_for(worker)
    mount.on_slew = worker.stop
    assert worker.submit(workflow, cfg=cfg, confirmed=True, angles=[25], point_id="mapped_000")
    until(app, lambda: len(ready) == 2)
    assert ready[-1] == "idle", logs
    assert not worker._adjustment
    assert mount.on is False
    assert mount.calls[-1] == ("tracking", False)


@pytest.mark.parametrize("cleanup_failure", ["stop", "core"])
def test_cancel_cleanup_failure_never_restores_tracking(safety_worker, app, monkeypatch, cleanup_failure):
    worker, mount, ready, logs, cfg = safety_worker

    def cancelled():
        mount.tracking(False)
        if cleanup_failure == "stop":
            mount.fail_stop = True
        worker.stop()
        exc = core.Cancelled("cancel during calibration")
        if cleanup_failure == "core":
            setattr(exc, "partial_result", dict(cleanup_errors=["core zero failed"]))
        raise exc

    monkeypatch.setattr(worker, "_measure_here", cancelled)
    assert worker.submit("measure", cfg=cfg)
    until(app, lambda: len(ready) == 2)
    assert ready[-1] == "fault", logs
    assert mount.on is False
    assert ("tracking", True) not in mount.calls


def test_candidate_requires_fresh_confirmation_and_second_return(safety_worker, app, monkeypatch, tmp_path):
    worker, mount, ready, logs, cfg = safety_worker
    candidate_for(worker)
    mock_confirmation(worker, mount, monkeypatch)
    rows = []
    worker.rows_changed.connect(rows.append)
    assert worker.submit("slew_candidate", cfg=cfg, confirmed=True, point_id="mapped_000")
    until(app, lambda: len(ready) == 2)
    assert ready[-1] == "adjustment", logs
    order = [call[0] for call in mount.calls]
    assert order == ["slew", "tracking", "acquire", "measure", "tracking", "slew", "tracking", "stop", "tracking"]
    targets = [call[1:] for call in mount.calls if call[0] == "slew"]
    assert targets == [((-core.legacy.ha_from_angle(25)) % 24, 45.)] * 2
    assert mount.on is False and worker._adjustment
    assert not worker.submit("measure", cfg=cfg)
    manifest = json.loads(next(tmp_path.rglob("manifest.json")).read_text())
    assert manifest["status"] == "completed"
    confirmation = manifest["rows"][0]
    assert confirmation["role"] == "candidate_confirmation"
    assert confirmation["confirmation_status"] == "confirmed"
    assert confirmation["candidate_point_id"] == "mapped_000"
    assert confirmation["confirmation_start_pose"]["angle"] == pytest.approx(25)
    assert confirmation["pose"]["angle"] == pytest.approx(27)
    assert confirmation["adjustment_pose"]["angle"] == pytest.approx(25)
    assert confirmation["adjustment_pose"]["tracking"] is False
    assert rows[-1][0]["confirmation_status"] == "confirmed"
    assert next(tmp_path.rglob("confirmation_000_result.json")).exists()
    worker.stop()
    until(app, lambda: len(ready) == 3)
    assert ready[-1] == "adjustment" and not mount.on


@pytest.mark.parametrize("outcome", ["uncertain", "invalid", "fault", "cancelled"])
def test_candidate_no_adjustment_on_failed_confirmation(safety_worker, app, monkeypatch, tmp_path, outcome):
    worker, mount, ready, logs, cfg = safety_worker
    candidate_for(worker)

    def fail():
        if outcome == "fault":
            raise RuntimeError("star lost")
        if outcome == "cancelled":
            worker.stop()

    mock_confirmation(worker, mount, monkeypatch, quality=outcome, fail=fail)
    assert worker.submit("slew_candidate", cfg=cfg, confirmed=True, point_id="mapped_000")
    until(app, lambda: len(ready) == 2)
    assert ready[-1] == ("idle" if outcome == "cancelled" else "fault"), logs
    assert not worker._adjustment
    assert len([c for c in mount.calls if c[0] == "slew"]) == 1
    assert mount.on is (outcome == "cancelled")
    manifest = json.loads(next(tmp_path.rglob("manifest.json")).read_text())
    assert manifest["status"] == ("cancelled" if outcome == "cancelled" else "failed")
    assert manifest["rows"][0]["confirmation_status"] == manifest["status"]


def test_candidate_manual_fallback_rechecks_same_pose(safety_worker, app, monkeypatch):
    worker, mount, ready, logs, cfg = safety_worker
    candidate_for(worker)
    acquire = mock_confirmation(worker, mount, monkeypatch)
    attempts = []

    def retry(position=None):
        attempts.append(position)
        return False if len(attempts) == 1 else acquire(position)

    monkeypatch.setattr(worker, "_acquire", retry)
    assert worker.submit("slew_candidate", cfg=cfg, confirmed=True, point_id="mapped_000")
    until(app, lambda: worker.state == "awaiting_star")
    assert worker.submit("select", position=(501., 502.))
    until(app, lambda: len(ready) == 2)
    assert ready[-1] == "adjustment", logs
    assert attempts == [None, (501., 502.)]


def test_candidate_confirmation_dialog_explains_motion(window, monkeypatch):
    row = candidate_for(window.worker)
    window._on_rows([row])
    window.table.selectRow(0)
    prompts, launches = [], []
    monkeypatch.setattr(window, "_confirm_motion", lambda text: prompts.append(text) or True)
    monkeypatch.setattr(window, "_launch", lambda *a, **k: launches.append((a, k)))
    window._return_candidate()
    assert "calibration" in prompts[0]
    assert "confirmation measurement" in prompts[0]
    assert "SECOND slew" in prompts[0]
    assert "requested angle 25.000" in prompts[0]
    assert launches[0][0] == ("slew_candidate",)


@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize("phase", ["drift", "reverse_1"])
def test_candidate_safe_cancellation_restores_workflow_entry(safety_worker, app, monkeypatch, initial, phase):
    worker, mount, ready, logs, cfg = safety_worker
    mount.on = initial
    candidate_for(worker)
    mock_confirmation(worker, mount, monkeypatch)

    def cancel(event):
        if event.get("type") == "phase" and event.get("phase") == phase:
            worker.stop()

    worker.event.connect(cancel, QtCore.Qt.ConnectionType.DirectConnection)
    assert worker.submit("slew_candidate", cfg=cfg, confirmed=True, point_id="mapped_000")
    until(app, lambda: len(ready) == 2)
    assert ready[-1] == "idle", logs
    assert mount.on is initial
    assert not worker._adjustment
    assert len([c for c in mount.calls if c[0] == "slew"]) == 1


@pytest.mark.parametrize("late_action", ["stop", "shutdown"])
@pytest.mark.parametrize("initial", [True, False])
def test_late_safe_cancellation_restores_entry(safety_worker, app, monkeypatch, late_action, initial):
    worker, mount, ready, logs, cfg = safety_worker
    mount.on = initial
    armed = []
    original_pose = worker._pose

    def completed():
        mount.tracking(True)  # Like a completed map slew / measurement.
        armed.append(True)

    def final_pose():
        if armed:
            armed.clear()
            getattr(worker, late_action)()
        return original_pose()

    monkeypatch.setattr(worker, "_map", lambda rows: completed())
    monkeypatch.setattr(worker, "_pose", final_pose)
    assert worker.submit("map", cfg=cfg, confirmed=True, angles=[25])
    until(app, lambda: len(ready) >= 2)
    if late_action == "shutdown":
        assert worker.wait(10000)
    assert ready[-1] == "idle", logs
    assert mount.on is initial


@pytest.mark.parametrize("failure", ["second_slew_cancel", "pier", "tracking", "save"])
def test_candidate_final_return_gates_adjustment(safety_worker, app, monkeypatch, tmp_path, failure):
    worker, mount, ready, logs, cfg = safety_worker
    candidate_for(worker)
    mock_confirmation(worker, mount, monkeypatch)
    original_measure = core.measure
    original_tracking = mount.tracking
    original_save = core.save_manifest
    original_slew = mount.slew

    def measure(*args):
        result = original_measure(*args)
        if failure == "pier":
            mount.pier = "West"
        return result

    def slew(*args):
        if failure == "second_slew_cancel" and any(c[0] == "measure" for c in mount.calls):
            worker.stop()
        original_slew(*args)

    def tracking(on):
        if failure == "tracking" and not on and sum(c[0] == "slew" for c in mount.calls) == 2:
            return  # Driver silently ignores OFF after second arrival.
        original_tracking(on)

    def save(path, rows, status):
        if failure == "save" and status == "confirming":
            raise OSError("manifest write failed")
        return original_save(path, rows, status)

    monkeypatch.setattr(core, "measure", measure)
    monkeypatch.setattr(mount, "slew", slew)
    monkeypatch.setattr(mount, "tracking", tracking)
    monkeypatch.setattr(core, "save_manifest", save)
    assert worker.submit("slew_candidate", cfg=cfg, confirmed=True, point_id="mapped_000")
    until(app, lambda: len(ready) == 2)
    assert ready[-1] == ("idle" if failure == "second_slew_cancel" else "fault"), logs
    assert not worker._adjustment
    if failure != "tracking":
        assert mount.on is False
    manifest = json.loads(next(tmp_path.rglob("manifest.json")).read_text())
    assert manifest["status"] in {"failed", "cancelled"}


def test_candidate_manual_retry_refuses_drifted_pose(safety_worker, app, monkeypatch):
    worker, mount, ready, logs, cfg = safety_worker
    candidate_for(worker)
    mock_confirmation(worker, mount, monkeypatch)
    monkeypatch.setattr(worker, "_acquire", lambda position=None: False)
    assert worker.submit("slew_candidate", cfg=cfg, confirmed=True, point_id="mapped_000")
    until(app, lambda: worker.state == "awaiting_star")
    mount.ra = (-core.legacy.ha_from_angle(28)) % 24
    assert worker.submit("auto_select")
    until(app, lambda: len(ready) == 2)
    assert ready[-1] == "fault", logs
    assert not worker._adjustment and not mount.on
    assert not any(c[0] == "measure" for c in mount.calls)


def test_stop_queued_during_state_publication_blocks_new_motion(safety_worker, app, monkeypatch):
    worker, mount, ready, logs, cfg = safety_worker
    mount.on = False
    rejected = []
    monkeypatch.setattr(worker, "_map", lambda rows: mount.tracking(True))

    def stop_after_final_snapshot(state):
        if state == "completed":
            worker.stop()

    def try_new_motion(state):
        if state == "completed":
            rejected.append(not worker.submit("map", cfg=cfg, angles=[25], confirmed=True))

    worker.state_changed.connect(stop_after_final_snapshot, QtCore.Qt.ConnectionType.DirectConnection)
    worker.ready.connect(try_new_motion, QtCore.Qt.ConnectionType.DirectConnection)
    assert worker.submit("map", cfg=cfg, angles=[25], confirmed=True)
    until(app, lambda: len(ready) == 3)
    assert rejected == [True]
    assert ready[-1] == "idle", logs
    assert mount.on is False


def legend_names(plot):
    return [label.text for _, label in plot.plotItem.legend.items]


def phase_event(window, name):
    window._on_event(dict(type="phase", phase=name, duration=4., raw_rate=0.))


def sample_event(window, t, x, y):
    window._on_event(dict(type="sample", t=t, x=x, y=y))


def completed_display_result():
    return dict(backlash=12., uncertainty_arcsec=1.5, quality="uncertain",
                notes=["Directional disagreement; repeat measurement."],
                u=[.6, .8], drift_px_s=2 * core.S_ARCSEC,
                _raw=dict(frames=[[10., 100., 200.], [11., 103., 204.], [12., 106., 208.]],
                          cmds=[(10., 0.), (11., 1.)],
                          trans=[dict(t=11., fit_before=[2., 112., .1], fit_after=[3., 114., .1])]))


def preview_snapshot(x=120., y=220., quality=None):
    return FrameSnapshot(np.ones((32, 32)), (100, 200), 1, 25., x, y, quality)


def test_connected_guidance_is_persistent_without_launching_work(window, monkeypatch):
    launches = []
    monkeypatch.setattr(window.worker, "submit", lambda *a, **k: launches.append((a, k)))
    window._on_event(dict(type="connected", simulation=True, camera="simulator", telescope="simulator",
                          can_slew=True, settings={"binning": 1}, connection="test", width=100, height=100))
    window._on_ready("idle")
    assert not launches
    guidance = window.guidance_label.text()
    for text in ("1 Full frame", "2 Auto-select or click an isolated star", "3 Measure here",
                 "auto-selects if no choice", "one point", "multiple signed angles", "fast virtual time"):
        assert text in guidance
    for button in (window.connect_button, window.preview_button, window.live_button,
                   window.auto_button, window.measure_button, window.map_button, window.plan_button):
        assert button.toolTip()


@pytest.mark.parametrize("state,expected", [
    ("preview", "Preview exposure"), ("acquiring", "reacquiring"),
    ("awaiting_star", "Skip field"), ("completed", "diagnostic allowance"),
    ("fault", "disconnect"), ("adjustment", "Clear hands/tools"),
    ("disconnected", "no exposures"), ("stopping", "cooperative cleanup"),
])
def test_guidance_states(window, state, expected):
    window._on_ready(state)
    assert expected in window.guidance_label.text()
    assert "1 Full frame" in window.guidance_label.text()


@pytest.mark.parametrize("phase,expected", [
    ("drift", "Drift calibration"), ("sign", "Drive direction check"),
    ("sign_settle", "Drive direction check"), ("latency_fast", "Drive response delay"),
    ("latency_slow", "Drive response delay"), ("reverse_1", "Reverse drive"),
    ("forward_1", "Forward drive"),
])
def test_plain_language_phase_guidance(window, phase, expected):
    phase_event(window, phase)
    assert expected in window.guidance_label.text()
    assert expected.upper() in window.state_label.text()
    assert "phases may flash" in window.guidance_label.text()


def test_selected_and_disconnect_guidance(window):
    window._on_event(dict(type="selected", position=(12., 13.)))
    assert "Star selected" in window.guidance_label.text()
    window._on_event(dict(type="selected", position=None))
    assert "No suitable star" in window.guidance_label.text()
    window._on_event(dict(type="disconnected"))
    assert "Disconnected" in window.guidance_label.text()
    assert "No automatic exposures" in window.guidance_label.text()


def test_image_legend_survives_quality_and_result_updates(window):
    legend = window.image_legend_label.text()
    for text in ("Cyan line", "White cross", "Gray dashed", "Orange bracket", "completed fit only", "seeing removal"):
        assert text in legend
    assert 'style="color:' in legend
    phase_event(window, "drift")
    window._on_frame(preview_snapshot(quality={"snr": 25., "fwhm": 3., "saturated": False}))
    assert "25.0" in window.quality_label.text()
    bracket = window.bracket_item.getData()[0]
    assert bracket is None or not bracket.size
    window._on_result(completed_display_result())
    assert window.bracket_item.getData()[0].size
    window._on_frame(preview_snapshot(quality={"snr": 40.}))
    assert "40.0" in window.quality_label.text()
    assert window.image_legend_label.text() == legend
    assert "12.000 ± 1.500" in window.result_summary.text()
    assert "Quality: uncertain" in window.result_summary.text()
    assert "Directional disagreement" in window.result_summary.text()


def test_live_trajectory_projects_all_samples_and_updates_without_frame(window):
    phase_event(window, "drift")
    sample_event(window, 10., 100., 200.)
    sample_event(window, 11., 103., 204.)
    np.testing.assert_allclose(window.x_curve.getData(), [[0., 1.], [0., 3.]])
    np.testing.assert_allclose(window.y_curve.getData()[1], [0., 4.])
    assert window.time_plot.getAxis("left").labelUnits == "px"
    assert legend_names(window.time_plot)[:2] == ["Sensor x displacement (cyan)", "Sensor y displacement (purple)"]
    window._on_event(dict(type="calibration", u=[.6, .8], v0=2 * core.S_ARCSEC, sign=1.))
    np.testing.assert_allclose(window.x_curve.getData()[1], [0., 2.5])
    np.testing.assert_allclose(window.y_curve.getData()[1], [0., 0.], atol=1e-14)
    sample_event(window, 12., 106., 208.)
    np.testing.assert_allclose(window.x_curve.getData()[1], [0., 2.5, 5.])
    assert window.time_plot.getAxis("left").labelUnits == "arcsec"
    assert "RA-axis-equivalent" in window.time_plot.getAxis("left").labelText
    assert "first valid sample" in window.time_plot.getAxis("bottom").labelText
    assert legend_names(window.time_plot)[:2] == ["RA displacement (cyan)", "Orthogonal displacement (purple)"]
    assert "NOT backlash alone" in window.trajectory_caption.text()
    assert "seeing removal" in window.trajectory_caption.text()


def test_completed_and_live_use_same_origin_units_and_preview_preserves_fits(window):
    result = completed_display_result()
    phase_event(window, "drift")
    for t, x, y in result["_raw"]["frames"]:
        sample_event(window, t, x, y)
    window._on_event(dict(type="calibration", u=result["u"], v0=result["drift_px_s"], sign=1.))
    live = [np.array(values) for values in window.x_curve.getData()]
    window._on_result(result)
    np.testing.assert_allclose(window.x_curve.getData(), live)
    # Absolute fit intercepts use exactly the same projected origin (110).
    np.testing.assert_allclose(window.fit_items[-2].getData(), [[0., 1.], [0., 2.]])
    np.testing.assert_allclose(window.fit_items[-1].getData(), [[1., 2.], [4., 7.]])
    fits = list(window.fit_items)
    bracket = [a.copy() for a in window.bracket_item.getData()]
    window._on_ready("completed")
    window._on_state("preview")
    window._on_frame(preview_snapshot())
    np.testing.assert_allclose(window.x_curve.getData(), live)
    np.testing.assert_allclose(window.bracket_item.getData(), bracket)
    assert window.fit_items == fits
    assert window.time_plot.getAxis("left").labelUnits == "arcsec"
    assert len(legend_names(window.time_plot)) == 5
    assert window._before_sample.scene() is None  # Legend sample is not fake plotted data.
    assert window._after_sample.scene() is None
    assert len(window.time_plot.listDataItems()) == 4  # Two trajectories and two real fits.
    assert "image's inferred-backlash bracket" in window.trajectory_caption.text()


@pytest.mark.parametrize("workflow", ["repeat", "map point", "candidate confirmation"])
def test_each_actual_drift_resets_all_completed_display_state(window, workflow):
    for _ in range(3):
        window._on_result(completed_display_result())
        assert len(legend_names(window.time_plot)) == 5
        window._on_state("acquiring")  # Keep old result until actual measurement starts.
        assert window.last_result is not None, workflow
        phase_event(window, "drift")
        assert not window.samples and not window.trail
        assert window.calibration is None and window.last_result is None
        assert not window.fit_items
        for item in (window.trail_item, window.star_item, window.direction_item, window.bracket_item,
                     window.x_curve, window.y_curve):
            data = item.getData()[0]
            assert data is None or not len(data)
        assert window.time_plot.getAxis("left").labelUnits == "px"
        assert "Raw sensor displacement" in window.time_plot.plotItem.titleLabel.text
        assert "No completed fit yet" in window.result_summary.text()
        assert len(legend_names(window.time_plot)) == 2
        assert len(window.time_plot.listDataItems()) == 2


@pytest.mark.parametrize("accepted", [True, False])
def test_measure_submission_does_not_erase_completed_plot(window, monkeypatch, accepted):
    window._on_result(completed_display_result())
    result = window.last_result
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(window.worker, "submit", lambda *a, **k: accepted)
    window._measure()
    assert window.last_result is result
    assert window.fit_items
    if accepted:
        assert window.tabs.currentWidget() is window.trajectory_panel


def test_completed_image_uses_full_recorded_path_and_refits(window, monkeypatch):
    fitted = []
    monkeypatch.setattr(window, "_fit_image", lambda: fitted.append(True))
    result = completed_display_result()
    window._on_result(result)
    recorded = np.asarray(result["_raw"]["frames"])
    x, y = window.trail_item.getData()
    np.testing.assert_allclose(x, recorded[:, 1])
    np.testing.assert_allclose(y, recorded[:, 2])
    assert fitted == [True]
    assert window.time_plot.minimumHeight() >= 220
    assert window.time_plot.plotItem.legend.columnCount == 2


def test_missing_partial_samples_leave_gaps_and_use_first_valid_origin(window):
    phase_event(window, "drift")
    sample_event(window, 8., None, None)
    sample_event(window, 10., 100., 200.)
    sample_event(window, 11., 103., None)
    sample_event(window, 12., 106., 208.)
    window._on_event(dict(type="calibration", u=[.6, .8], v0=2 * core.S_ARCSEC, sign=1.))
    times, values = window.x_curve.getData()
    np.testing.assert_allclose(times, [-2., 0., 1., 2.])
    np.testing.assert_allclose(values, [np.nan, 0., np.nan, 5.], equal_nan=True)
    assert window.x_curve.opts["connect"] == "finite"
    assert window.y_curve.opts["connect"] == "finite"
    window._on_frame(preview_snapshot(x=None, y=None))
    window._on_frame(preview_snapshot(x=120., y=None))
    window.time_plot.autoRange()


@pytest.mark.parametrize("frames,u,v0", [
    ([], None, None), ([[None, None, None]], [.6, .8], 30.),
    ([[10., 100.], [11., None, 200.]], [1.], 0.),
    ([[9., None, 200.], [10., 100., 200.], [11., None, None], [12., 106., 208.]], [.6, .8], 2 * core.S_ARCSEC),
])
def test_partial_results_never_crash_or_invent_fits(window, frames, u, v0):
    window._on_result(dict(_raw={"frames": frames, "trans": [{"t": None}, {"t": 11., "fit_before": None}]},
                           u=u, drift_px_s=v0, status="failed", quality="invalid", reason="Selected star lost"))
    assert "Selected star lost" in window.result_summary.text()
    assert not window._has_fits
    data = window.bracket_item.getData()[0]
    assert data is None or not len(data)
    window.time_plot.autoRange()


def test_map_direction_names_captions_and_scatter_only(window):
    assert "Run Start map" in window.map_caption.text()
    assert "Measure here produces one point, no full map" in window.map_caption.text()
    rows = [dict(angle=25., backlash=12., fwd_rev=11., rev_fwd=13., uncertainty_arcsec=1.)]
    window.baseline = [dict(rows[0], angle=-25.)]
    for _ in range(3):
        window._on_rows(rows)
        names = legend_names(window.map_plot)
        assert len(names) == len(set(names)) == 6
        for source in ("Current (cyan)", "Saved baseline (purple)"):
            assert source + " · median (circle)" in names
            assert source + " · forward → reverse (triangle)" in names
            assert source + " · reverse → forward (square)" in names
        for item in window.map_plot.listDataItems():
            assert item.opts["pen"] is None
    assert "NOT a confidence interval" in window.map_caption.text()
    assert "no connections across unknown samples or pier sides" in window.map_caption.text()


def test_tabs_follow_measure_and_map_but_not_plan_or_preview(window, monkeypatch):
    monkeypatch.setattr(window.worker, "submit", lambda *a, **k: True)
    window.tabs.setCurrentWidget(window.table)
    window._launch("plan")
    assert window.tabs.currentWidget() is window.table
    window._launch("map")
    assert window.tabs.currentWidget() is window.map_panel
    window._launch("preview")
    assert window.tabs.currentWidget() is window.map_panel
    window._launch("measure")
    assert window.tabs.currentWidget() is window.trajectory_panel


def test_real_simulator_signals_complete_then_preview_without_reverting(window, app, tmp_path):
    ready, frames, results, phases = [], [], [], []
    window.worker.ready.connect(ready.append)
    window.worker.frame.connect(frames.append)
    window.worker.result.connect(results.append)
    window.worker.event.connect(lambda event: phases.append(event["phase"]) if event["type"] == "phase" else None)
    cfg = core.Config(out=str(tmp_path), sim_backlash=12.)
    window.worker.start()
    try:
        assert window._launch("connect", cfg=cfg, simulation=True)
        until(app, lambda: len(ready) == 1)
        assert not frames and not results  # Connecting itself never exposes.
        assert "1 Full frame" in window.guidance_label.text()
        assert window._launch("measure", cfg=cfg)
        until(app, lambda: len(ready) == 2, 60)
        assert ready[-1] == "completed"
        assert phases[0] == "drift"
        assert window.tabs.currentWidget() is window.assessment_panel
        assert "educational synthetic" in window.assessment_details.toPlainText()
        assert f"B = {results[-1]['backlash']:.3f}" in window.result_summary.text()
        assert len(legend_names(window.time_plot)) == 5
        data = [a.copy() for a in window.x_curve.getData()]
        fits = list(window.fit_items)
        assert window._launch("preview", cfg=cfg, continuous=False)
        until(app, lambda: len(ready) == 3)
        np.testing.assert_allclose(window.x_curve.getData(), data)
        assert window.fit_items == fits
        assert window.time_plot.getAxis("left").labelUnits == "arcsec"
    finally:
        window.worker.shutdown()
        assert window.worker.wait(10000)
        app.processEvents()


def test_result_tab_is_first_readable_and_does_not_replace_trajectory(window):
    assert window.tabs.widget(0) is window.assessment_panel
    assert window.tabs.tabText(0) == "Result & next action"
    assert window.assessment_details.isReadOnly()
    assert window.assessment_panel.widgetResizable()
    assert not window.target_check.isChecked()
    assert not window.target_spin.isEnabled()
    window._on_result(dict(completed_display_result(), quality="valid"))
    assert window.assessment_title.text() == "Measurable backlash — size target not set"
    assert "Start map" in window.assessment_action.text()
    assert "cannot detect binding" in window.assessment_warning.text()
    assert "12.000 ± 1.500" in window.result_summary.text()
    assert window.fit_items  # Existing trajectory remains intact.


def test_comparison_goal_is_display_only_reassesses_without_rerun(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window.worker, "submit", lambda *a, **k: calls.append((a, k)))
    r = dict(completed_display_result(), quality="valid")
    window._on_result(r)
    old_config = window._config()
    old_fits = list(window.fit_items)
    window.target_spin.setValue(20.)
    assert "size target not set" in window.assessment_title.text()
    window.target_check.setChecked(True)
    assert window.target_spin.isEnabled()
    assert "Within selected target" in window.assessment_title.text()
    window.target_spin.setValue(5.)
    assert "Above selected target" in window.assessment_title.text()
    window.target_spin.setValue(12.)
    assert "overlaps target" in window.assessment_title.text()
    window.target_check.setChecked(False)
    assert "size target not set" in window.assessment_title.text()
    assert not calls and window._config() == old_config
    assert window.last_result is r and window.fit_items == old_fits
    assert not any("target" in key for key in window.settings.allKeys())


def test_goals_stay_only_in_this_window(window, app):
    window.target_spin.setValue(20.)
    window.target_check.setChecked(True)
    other = MainWindow(start_worker=False, settings=window.settings)
    try:
        assert not other.target_check.isChecked()
        assert not other.target_spin.isEnabled()
    finally:
        other.close()


def test_actual_drift_clears_interpretation_and_target_changes_do_not_revive_it(window):
    window._on_result(dict(completed_display_result(), quality="valid"))
    window.target_check.setChecked(True)
    phase_event(window, "drift")
    window.target_spin.setValue(100.)
    assert window.last_result is None and window._assessment_result is None
    assert window.assessment_title.text() == "No completed result selected"
    assert not window.assessment_details.toPlainText()
    assert "cannot detect binding" in window.assessment_warning.text()
    assert window.target_check.isChecked()  # The user's window-local goal survives.


@pytest.mark.parametrize("workflow", ["measure", "map", "slew_candidate"])
def test_only_completed_standalone_measure_selects_results(window, monkeypatch, workflow):
    monkeypatch.setattr(window.worker, "submit", lambda *a, **k: True)
    window._launch(workflow)
    window.tabs.setCurrentWidget(window.map_panel)
    window._on_result(completed_display_result())
    assert window.tabs.currentWidget() is window.map_panel  # No point signal steals focus.
    window._on_ready("completed")
    expected = window.assessment_panel if workflow == "measure" else window.map_panel
    assert window.tabs.currentWidget() is expected
    window.tabs.setCurrentWidget(window.trajectory_panel)
    window._on_ready("completed")
    assert window.tabs.currentWidget() is window.trajectory_panel  # No stale workflow.


def test_failed_measure_does_not_automatically_select_result(window, monkeypatch):
    monkeypatch.setattr(window.worker, "submit", lambda *a, **k: True)
    window._launch("measure")
    window._on_result(dict(status="cancelled", quality="invalid", backlash=None))
    window._on_ready("fault")
    assert window.tabs.currentWidget() is window.trajectory_panel
    assert "Measurement unreliable" in window.assessment_title.text()


def test_row_selection_interprets_without_camera_reconstruction_or_authorization(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window.worker, "submit", lambda *a, **k: calls.append(a))
    original = dict(completed_display_result(), quality="valid")
    window._on_result(original)
    fits = list(window.fit_items)
    window._on_frame(preview_snapshot())
    image = window.image_item.image.copy()
    window.connected = window.can_slew = True
    window.current_connection = "live"
    window._on_ready("idle")
    window._on_rows([
        dict(angle=25., backlash=12., quality="valid", uncertainty_arcsec=1., imported=True),
        dict(angle=50., backlash=30., quality="valid", uncertainty_arcsec=1., imported=True)])
    window.table.selectRow(1)
    window.target_spin.setValue(20.)
    window.target_check.setChecked(True)
    assert "Above selected target" in window.assessment_title.text()
    assert "B: 30" in window.assessment_details.toPlainText()
    assert "source not recorded" in window.assessment_details.toPlainText()
    assert window.last_result is original and window.fit_items == fits
    np.testing.assert_array_equal(window.image_item.image, image)
    assert not window.candidate_button.isEnabled() and not calls
    assert "Lowest measured gap" in window.map_assessment_label.text()
    window.table.selectRow(0)
    assert "Within selected target" in window.assessment_title.text()


@pytest.mark.parametrize("simulation", [None, False, True])
def test_import_interprets_row_zero_offline_not_current_simulator(window, tmp_path, monkeypatch, simulation):
    r = dict(backlash=12., uncertainty_arcsec=1., quality="valid", angle=25.)
    if simulation is not None:
        r["simulation"] = simulation
    path = tmp_path / "saved.json"
    path.write_text(json.dumps(r), encoding="utf-8")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    window.connected = window.simulation = True
    window._import(False)
    assert window.table.currentRow() == 0
    assert window.tabs.currentWidget() is window.assessment_panel
    assert "Measurable backlash" in window.assessment_title.text()
    expected = {None: "source not recorded", False: "Recorded hardware measurement", True: "educational synthetic"}
    assert expected[simulation] in window.assessment_details.toPlainText()
    assert window.last_result is None  # Import/selection is not a camera result.
    assert window.image_item.image is None
    assert not window.candidate_button.isEnabled()


def test_import_legacy_missing_uncertainty_is_unreliable_even_disconnected(window, tmp_path, monkeypatch):
    path = tmp_path / "legacy.json"
    path.write_text('{"backlash":12,"spread":0.1}', encoding="utf-8")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    window._import(False)
    assert window.state == "disconnected"
    assert "Measurement unreliable" in window.assessment_title.text()


def test_live_connection_context_only_and_map_summary_does_not_overrule_point(window):
    window.connected = True
    window.simulation = False
    window.current_connection = "live"
    window._on_result(dict(completed_display_result(), connection="live"))
    assert "Recorded hardware measurement" in window.assessment_details.toPlainText()
    window._on_rows([dict(angle=25., backlash=12., uncertainty_arcsec=1., quality="valid",
                          simulation=True)])
    assert "No tight spot across the axis" in window.map_assessment_label.text()
    assert "educational synthetic" in window.map_assessment_label.text()
    assert "Inconclusive" in window.assessment_title.text()
    window._on_result(dict(completed_display_result(), connection="old"))
    assert "source not recorded" in window.assessment_details.toPlainText()