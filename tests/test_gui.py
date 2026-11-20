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
from ra_backlash_app.gui import DeviceWorker, MainWindow, backlash_pixels, image_rect


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