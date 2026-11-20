"""Local desktop controller. All device access belongs to DeviceWorker.run().

Stop is cooperative: a hung COM call can still require native mount controls.
Image coordinates throughout this module are full-frame *binned* pixels.
"""
from __future__ import annotations

import csv
import hashlib
import html
import math
import os
import queue
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from . import core, devices
from .assessment import assess_map, assess_result


IDLE_STATES = {"idle", "completed"}
ACTIVE_STATES = {"connecting", "preview", "acquiring", "slewing", "settling",
                 "calibrating", "measuring", "analyzing", "awaiting_star", "stopping"}


def finite(value, default=float("nan")):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def backlash_pixels(backlash, drift_px_s):
    """Inferred RA-axis displacement, NOT an additional observed trail."""
    b, v = finite(backlash), finite(drift_px_s)
    return b * v / core.S_ARCSEC if b >= 0 and v > 0 else float("nan")


def image_rect(origin, shape, step=1):
    """Preserve absolute binned coordinates when displaying a decimated frame."""
    return QtCore.QRectF(float(origin[0]) - .5, float(origin[1]) - .5,
                        float(shape[1] * step), float(shape[0] * step))


@dataclass(frozen=True)
class FrameSnapshot:
    image: np.ndarray
    origin: tuple
    step: int
    t: float
    x: float
    y: float
    quality: object


class DeviceWorker(QtCore.QThread):
    """Persistent single device owner; no Qt event loop or interrupting slots.

    submit/stop/shutdown ONLY manipulate Python synchronization primitives.
    Cancellation is cleared under the same lock as acceptance of a NEW task,
    never by run() after a Stop. Controls within preview/map never clear it.
    """
    state_changed = QtCore.Signal(str)
    event = QtCore.Signal(object)
    frame = QtCore.Signal(object)
    result = QtCore.Signal(object)
    rows_changed = QtCore.Signal(object)
    log = QtCore.Signal(str)
    ready = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.commands = queue.Queue()
        self.cancel = core.Cancellation()
        self._lock = threading.Lock()
        self._busy = False
        self._closing = threading.Event()
        self.state = "disconnected"
        # Dynamic ASCOM dispatch and simulator adapters share a runtime API;
        # never expose these handles outside the owning worker.
        self.mount: Any = None
        self.cam: Any = None
        self.clock: Any = None
        self.tracker: core.Tracker | None = None
        self.cfg = core.Config()
        self.simulation = True
        self.selected = None
        self.geometry = False
        self._last_frame = 0.
        # Set when a frame is emitted; the GUI clears it so a slow GUI never builds a backlog.
        self._frame_sent_at = 0.
        self._last_pose = 0.
        self._live_results = {}
        self._connection = ""
        self._adjustment = False
        self._fault = False
        self._reconnect_allowed = True
        self._workflow_tracking = None
        # Kept true on an interrupted/failed slew, even if the adapter aborts.
        self._slew_in_progress = False

    def submit(self, name, **payload):
        with self._lock:
            if self._closing.is_set():
                return False
            if self._fault and name != "disconnect" and not (
                    name == "connect" and self.state == "disconnected" and self._reconnect_allowed):
                return False
            if self._busy:
                if self.state in {"preview", "awaiting_star"} and name in {"select", "auto_select", "skip"} and not self.cancel._event.is_set():
                    self.commands.put((name, payload))
                    return True
                return False
            allowed = ({"connect"} if self.state == "disconnected" else
                       {"disconnect"} if self.state == "fault" else
                       {"finish_adjustment", "disconnect"} if self._adjustment else
                       {"disconnect", "preview", "select", "auto_select", "measure", "map", "plan", "slew_candidate", "pose"})
            if name not in allowed:
                return False
            self.cancel.reset()
            self._busy = True
            self.commands.put((name, payload))
            return True

    def stop(self):
        with self._lock:
            self._busy = True
            self.cancel.cancel()
            self._drain()
            # Wake awaiting_star / preview without a queued Qt slot.
            self.commands.put(("_stop", {}))

    def shutdown(self):
        with self._lock:
            self._closing.set()
            self.cancel.cancel()
            self._drain()
            self.commands.put(("_shutdown", {}))

    def _drain(self):
        while True:
            try:
                self.commands.get_nowait()
            except queue.Empty:
                break

    def _state(self, state):
        with self._lock:
            if state == "fault":
                self._fault = True
                self._reconnect_allowed = False
            if self._fault and state not in {"disconnected", "connecting"}:
                state = "fault"
            self.state = state
        self.state_changed.emit(state)

    def _stop_motion(self, restore=False):
        """Stop first; only safe cancellation may restore workflow-entry tracking."""
        errors = []
        try:
            self.mount.stop()
        except Exception as exc:
            errors.append(str(exc))
        tracking = (self._workflow_tracking if restore and not errors and not self._fault
                    and not self._slew_in_progress and not self._adjustment else False)
        try:
            # With no workflow snapshot, ordinary preview/idle Stop leaves
            # tracking alone. Fault, slew abort and adjustment always force OFF.
            if tracking is not None:
                self.mount.tracking(tracking)
        except Exception as exc:
            errors.append(str(exc))
            try:
                self.mount.tracking(False)
            except Exception as off_error:
                errors.append(str(off_error))
        if errors:
            self._state("fault")
            raise RuntimeError("; ".join(errors))

    def run(self):
        com = None
        try:
            if os.name == "nt":
                import pythoncom
                pythoncom.CoInitialize()
                com = pythoncom
            while not self._closing.is_set():
                name, payload = self.commands.get()
                if name.startswith("_"):
                    if name == "_stop":
                        final = "adjustment" if self._adjustment else "idle" if self.mount else "disconnected"
                        try:
                            if self.mount:
                                self._stop_motion(restore=True)
                        except Exception as exc:
                            self.log.emit(f"Stop failed: {exc}; use native controls")
                            final = "fault"
                        self._state(final)
                        with self._lock:
                            self._busy = not self.commands.empty()
                        self.ready.emit(self.state)
                    continue
                final = "idle"
                try:
                    self.cancel.check()
                    self._workflow_tracking = None
                    self._slew_in_progress = False
                    if self.mount and name in {"map", "measure", "slew_candidate"}:
                        self._workflow_tracking = self.mount.get_tracking()
                    self._dispatch(name, payload)
                    self.cancel.check()
                    final = "adjustment" if self._adjustment else "completed" if name in {"measure", "map"} else "idle"
                    if self.mount is None:
                        final = "disconnected"
                except core.Cancelled as exc:
                    self.log.emit(f"Stopped: {exc}")
                    self._partial(exc)
                    final = "adjustment" if self._adjustment else "idle" if self.mount else "disconnected"
                    if getattr(exc, "partial_result", {}).get("cleanup_errors"):
                        self._state("fault")
                    if self.mount:
                        try:
                            self._stop_motion(restore=True)
                        except Exception as cleanup:
                            self.log.emit(f"Stop cleanup failed: {cleanup}; use native controls")
                            final = "fault"
                    if getattr(exc, "partial_result", {}).get("cleanup_errors"):
                        final = "fault"
                except Exception as exc:
                    self.log.emit(f"FAULT: {type(exc).__name__}: {exc}")
                    self._partial(exc)
                    self._state("fault")
                    final = "fault"
                    if self.mount:
                        try:
                            self._stop_motion()
                        except Exception as cleanup:
                            self.log.emit(f"Cleanup failed: {cleanup}. Use native controls.")
                finally:
                    if self.mount:
                        try:
                            self._pose()
                        except Exception as exc:
                            self.log.emit(f"Pose unavailable: {exc}")
                    # Stop/close can arrive during the final pose read, after
                    # dispatch/exception cleanup. It must not be discarded.
                    with self._lock:
                        late_cancel = self.cancel._event.is_set()
                        self._drain()
                    # A Stop AFTER this atomic snapshot remains queued and is
                    # handled next; never drain an unhandled late Stop.
                    if self.mount and late_cancel:
                        try:
                            self._stop_motion(restore=True)
                            if final == "completed":
                                final = "idle"
                        except Exception as exc:
                            self.log.emit(f"Late Stop cleanup failed: {exc}; use native controls")
                            final = "fault"
                    # Busy remains true until state is published. GUI's ready
                    # handler is the only point that re-enables launch actions.
                    self._state(final)
                    with self._lock:
                        self._busy = not self.commands.empty()
                    self.ready.emit(self.state)
        except Exception as exc:
            self.log.emit(f"Worker initialization failed: {exc}")
            self._state("fault")
        finally:
            try:
                if self.mount and self._closing.is_set():
                    try:
                        self._stop_motion(restore=True)
                    except Exception as exc:
                        self.log.emit(f"Close cleanup failed: {exc}; use native controls")
                self._disconnect()
            finally:
                if com is not None:
                    com.CoUninitialize()

    def _partial(self, exc):
        partial = getattr(exc, "partial_result", None)
        if partial:
            self.result.emit(dict(partial, imported=False))
        path = getattr(exc, "partial_path", None)
        if path:
            self.log.emit(f"Partial data saved: {path}")

    def _dispatch(self, name, p):
        if name == "connect":
            self._state("connecting")
            self.cfg = replace(p["cfg"]).validate()
            self.simulation = p.get("simulation", True)
            telescope, camera = p.get("telescope", ""), p.get("camera", "")
            if not self.simulation:
                telescope = telescope or devices._choose("Telescope")
                self.cancel.check()
                camera = camera or devices._choose("Camera")
                # Chooser-resolved camera gets its own local profile too.
                profile = QtCore.QSettings("RA Backlash", "Desktop")
                profile.beginGroup("cameras/" + hashlib.sha256(camera.encode()).hexdigest())
                for key, cast in (("exposure", float), ("binning", int), ("roi", int)):
                    if profile.contains(key):
                        setattr(self.cfg, key, cast(profile.value(key)))
                profile.endGroup()
                self.cfg.validate()
            self.mount, self.cam, self.clock = devices.open_devices(
                self.simulation, self.cfg, self.cancel, telescope, camera)
            self._connection = uuid.uuid4().hex
            self._live_results.clear()
            self.selected = None
            self.geometry = self.simulation
            self.event.emit(dict(type="connected", simulation=self.simulation,
                                 camera=camera or "simulator", telescope=telescope or "simulator",
                                 width=self.cam.W, height=self.cam.H, settings=asdict(self.cfg),
                                 can_slew=bool(self.mount.can_slew), connection=self._connection))
            self._pose()
            self.cancel.check()
            if self._reconnect_allowed:
                self._fault = False
            self._reconnect_allowed = False
        elif name == "disconnect":
            self._disconnect()
        elif name == "finish_adjustment":
            if not self._adjustment or not p.get("confirmed"):
                raise ValueError("Explicit motion confirmation is required")
            self.cancel.check()
            self.mount.stop()
            self.mount.tracking(True)
            self._adjustment = False
            self.selected = None
            self.log.emit("Adjustment finished by confirmation. Reacquire and recalibrate before measurement.")
        elif name == "pose":
            self._pose()
        else:
            cfg = replace(p.get("cfg", self.cfg)).validate()
            if cfg.binning != self.cfg.binning:
                raise ValueError("Reconnect to change camera binning")
            self.cfg = cfg
            self.mount.cfg = self.cam.cfg = cfg
            if name == "preview":
                self._preview(p.get("continuous", False))
            elif name in {"select", "auto_select"}:
                if not self._acquire(p.get("position") if name == "select" else None):
                    raise ValueError("No suitable star at selection; preview and retry")
            elif name == "measure":
                self._measure_here()
            elif name in {"plan", "map"}:
                if name == "map":
                    self._authorize(p)
                rows = core.plan_points(p["angles"], self.mount.latitude(), cfg.min_alt, cfg.dec)
                self.log.emit(f"Plan: {len(rows)} requested; {sum(row['status'] == 'ready' for row in rows)} feasible endpoints. Inspect adjusted Dec / skipped reasons; paths are not collision-validated.")
                self.rows_changed.emit([dict(row) for row in rows])
                if name == "map":
                    self._map(rows)
            elif name == "slew_candidate":
                self._authorize(p)
                row = self._live_results.get(p.get("point_id"))
                if not row or core.candidate([row]) is None or row.get("connection") != self._connection:
                    raise ValueError("Candidate must be a quality-valid measurement from this connection; imports are read-only")
                if not row.get("geometry_verified") or not row.get("pose"):
                    raise ValueError("Candidate pose/geometry has not been verified")
                self._confirm_candidate(row)
            else:
                raise ValueError(f"Unknown command {name}")

    def _disconnect(self):
        errors = []
        # Drop Tracker's camera reference before COM is uninitialized.
        self.tracker = None
        # No movement on ordinary idle connection/disconnection. Adapters stop
        # their own active operations; workflows handle motion cleanup first.
        for device in (self.cam, self.mount):
            if device is not None:
                try:
                    device.disconnect()
                except Exception as exc:
                    errors.append(str(exc))
        self.cam = self.mount = self.clock = None
        self.selected = None
        self._live_results.clear()
        self._adjustment = False
        self._reconnect_allowed = not errors
        if errors:
            self._state("fault")
        self.event.emit(dict(type="disconnected"))
        if errors:
            self.log.emit("Disconnect errors; verify native controls: " + "; ".join(errors))

    def _pose(self) -> dict[str, Any]:
        ra, dec = self.mount.ra_dec()
        lst = self.mount.lst()
        ha = (lst - ra + 12) % 24 - 12
        lat = self.mount.latitude()
        pose: dict[str, Any] = dict(ra=ra, dec=dec, lst=lst, ha=ha,
                    angle=core.legacy.angle_from_ha(ha), latitude=lat,
                    alt=core.legacy.altitude(lat, dec, ha),
                    pier=self.mount.pier_side(), tracking=self.mount.get_tracking())
        if not all(math.isfinite(pose[key]) for key in ("ra", "dec", "lst", "ha", "angle", "latitude", "alt")):
            raise RuntimeError("Nonfinite mount pose")
        self.event.emit(dict(type="pose", **pose))
        return pose

    def _authorize(self, p):
        if not p.get("confirmed"):
            raise ValueError("Motion/limits/collision confirmation required")
        pose = self._pose()
        if not self.mount.can_slew:
            raise ValueError("Async slew required")
        if not self.simulation:
            if not p.get("geometry_verified") or pose["latitude"] <= 0 or pose["pier"] not in {"East", "West"}:
                raise ValueError("Validate northern counterweight-down GEM geometry and known driver pier state")
            if int(self.mount.t.AlignmentMode) != 2:
                raise ValueError("Driver does not report German equatorial alignment")
        self.geometry = True

    def _verify_pose(self, target, dec, pier=None, tolerance=.5):
        pose = self._pose()
        if (abs(pose["angle"] - target) > tolerance or abs(pose["dec"] - dec) > .25
                or pose["alt"] < self.cfg.min_alt or (pier and pose["pier"] != pier)):
            raise RuntimeError("Actual pose/pier/altitude differs from validated destination; replan in native controls")
        return pose

    def _slew(self, angle, dec, expected_pier=None):
        if not self.geometry or not math.isfinite(angle) or angle == 0 or abs(angle) > 82:
            raise ValueError("Unsupported/unverified mechanical angle")
        before = self._pose()
        if not self.simulation and before["pier"] not in {"East", "West"}:
            raise RuntimeError("Current driver pier state is unknown")
        ha = core.legacy.ha_from_angle(angle)
        # Recomputed immediately before EACH slew, including candidate return.
        ra = (self.mount.lst() - ha) % 24
        if core.legacy.altitude(self.mount.latitude(), dec, ha) < self.cfg.min_alt:
            raise ValueError("Destination below minimum altitude")
        pier = "sim"
        if not self.simulation:
            try:
                pier = {0: "East", 1: "West"}.get(int(self.mount.t.DestinationSideOfPier(ra, dec)), "Unknown")
            except Exception as exc:
                raise RuntimeError("Destination pier prediction unavailable; automated motion refused") from exc
        if pier == "Unknown" or (expected_pier is not None and pier != expected_pier):
            raise ValueError("Unknown/different destination pier state")
        self._state("slewing")
        self.cancel.check()
        self._slew_in_progress = True
        self.mount.slew(ra, dec, self.cancel)
        self.cancel.check()
        self._verify_pose(angle, dec, pier)
        self._slew_in_progress = False
        self._state("settling")
        self.cancel.wait(self.cfg.settle, self.clock)
        return self._verify_pose(angle, dec, pier)

    def _confirm_candidate(self, candidate):
        """Return, independently remeasure, then return again before adjustment."""
        angle, dec = candidate["angle"], candidate["pose"]["dec"]
        pier = candidate["pose"]["pier"]
        folder = core.create_run(self.cfg.out)
        confirmation: dict[str, Any] = dict(
            angle=angle, dec=dec, expected_pier=pier,
            candidate_point_id=candidate["point_id"],
            role="candidate_confirmation", status="running")
        rows = [confirmation]
        core.save_manifest(folder, rows, "running")
        try:
            self._slew(angle, dec, expected_pier=pier)
            if not self._acquire() and not self._wait_star(confirmation):
                raise core.Cancelled("Candidate confirmation skipped; no adjustment")
            # Acquisition/manual waiting must not move us to a different pose.
            start_pose = self._verify_pose(angle, dec, pier)
            self.cancel.check()
            result = self._perform()
            result.update(role="candidate_confirmation", candidate_point_id=candidate["point_id"],
                          confirmation_start_pose=start_pose, expected_pier=pier)
            confirmation.update(self._record(result, folder, "confirmation_000", angle))
            core.save_manifest(folder, rows, "confirming")
            self.rows_changed.emit([dict(confirmation)])
            self.cancel.check()
            if (core.candidate([confirmation]) is None or result.get("cleanup_errors")
                    or confirmation["pose"]["pier"] != pier):
                raise RuntimeError("Confirmation is not quality-valid; adjustment refused")
            # Calibration/reversals moved the axis. Recompute the requested
            # mechanical destination using current LST, never the end pose.
            self._slew(angle, dec, expected_pier=pier)
            self.cancel.check()
            self.mount.stop()
            self.mount.tracking(False)
            achieved = self._verify_pose(angle, dec, pier)
            if achieved["tracking"]:
                raise RuntimeError("Driver did not confirm tracking OFF")
            self.cancel.check()
            confirmation.update(adjustment_pose=achieved, confirmation_status="confirmed")
            core.save_manifest(folder, rows, "completed")
            self.cancel.check()
            self._adjustment = True
            self.selected = None
            self.log.emit("ADJUSTMENT: fresh quality-valid confirmation saved; requested pose verified again, RA stopped / tracking OFF. No automatic mesh changes. Motors may remain energized.")
        except Exception as exc:
            confirmation.update(confirmation_status="cancelled" if isinstance(exc, core.Cancelled) else "failed",
                                reason=str(exc), partial_path=str(getattr(exc, "partial_path", "")))
            if confirmation["status"] == "running":
                confirmation["status"] = confirmation["confirmation_status"]
            core.save_manifest(folder, rows, confirmation["confirmation_status"])
            raise
        finally:
            self.rows_changed.emit([dict(confirmation)])

    def _publish_frame(self, frame, force=False):
        now = time.monotonic()
        if not force and (now - self._last_frame < .10 or 0 < now - self._frame_sent_at < 2.):
            return
        self._last_frame = self._frame_sent_at = now
        source = frame["image"]
        step = max(1, math.ceil(max(source.shape) / 1400))
        # bytes backing prevents re-enabling WRITEABLE, not just accidental writes.
        small = np.asarray(source[::step, ::step], dtype=np.float32)
        image = np.frombuffer(small.tobytes(), dtype=np.float32).reshape(small.shape)
        quality = MappingProxyType({k: frame.get(k) for k in ("snr", "fwhm", "saturated")})
        self.frame.emit(FrameSnapshot(image, tuple(frame["origin"]), step,
                                      frame["t"], frame.get("x", float("nan")),
                                      frame.get("y", float("nan")), quality))

    def _acquire(self, position=None):
        self._state("acquiring")
        self.tracker = core.Tracker(self.cam, self.clock, self.cfg, self.cancel,
                                    lambda f: self._publish_frame(f, force=True))
        found = self.tracker.acquire(position)
        self.selected = self.tracker.selected if found else None
        self.event.emit(dict(type="selected", position=self.selected))
        return found

    def _preview(self, continuous):
        self._state("preview")
        while True:
            self.cancel.check()
            self.cam.set_roi(0, 0, self.cam.W, self.cam.H)
            t, image, origin = self.cam.expose(self.clock, self.cfg.exposure)
            x, y = self.selected or (float("nan"), float("nan"))
            self._publish_frame(dict(image=image, origin=origin, t=t, x=x, y=y), force=not continuous)
            if time.monotonic() - self._last_pose > 2:
                self._last_pose = time.monotonic()
                self._pose()
            if not continuous:
                return
            # Real wall pacing prevents the fast simulator flooding the GUI.
            if self.cancel._event.wait(.05):
                self.cancel.check()
            try:
                command, p = self.commands.get_nowait()
            except queue.Empty:
                continue
            self.cancel.check()
            if command in {"select", "auto_select"}:
                if not self._acquire(p.get("position") if command == "select" else None):
                    self.log.emit("No suitable star; click to retry or Stop")
                self._state("preview")

    def _measurement_event(self, event):
        if event["type"] == "phase":
            self._state("measuring" if event["phase"].startswith(("reverse", "forward")) else "calibrating")
        self.event.emit(dict(event))

    def _perform(self):
        assert self.tracker is not None, "Acquire a star before measurement"
        self.tracker.on_frame = self._publish_frame
        result = core.measure(self.mount, self.tracker, self.clock, self.cfg,
                              self.cancel, self._measurement_event)
        self._state("analyzing")
        # Tracker.selected is the acquisition position; use the last tracked
        # centroid instead, so a subsequent manual-preserving measurement does
        # not search at an obsolete pre-calibration coordinate.
        last = self.tracker._last
        self.selected = tuple(last[1:]) if last is not None else self.tracker.selected
        return result

    def _record(self, result, folder, tag, requested=None):
        pose = self._pose()
        row = {k: v for k, v in result.items() if not k.startswith("_")}
        row.update(point_id=tag, angle=pose["angle"] if requested is None else requested,
                   achieved_angle=pose["angle"], dec=pose["dec"], pose=pose,
                   simulation=self.simulation, connection=self._connection,
                   geometry_verified=self.geometry, imported=False, status=result.get("status", "completed"))
        path = core.save_result(result, folder, tag, row)
        row["path"] = str(path)
        self._live_results[tag] = dict(row)
        self.result.emit(dict(result, **{k: v for k, v in row.items() if k not in result}))
        self.log.emit(f"Saved {path}")
        return row

    def _measure_here(self):
        folder = core.create_run(self.cfg.out)
        rows = []
        try:
            # Preserve a manual/previous identity through fresh full-frame
            # acquisition; failure NEVER silently falls back to brightest.
            if not self._acquire(self.selected):
                raise ValueError("Selected star unavailable; preview and explicitly select/auto-select again")
            rows.append(self._record(self._perform(), folder, "point_000"))
            core.save_manifest(folder, rows, "completed")
            self.rows_changed.emit(rows)
        except Exception as exc:
            rows.append(dict(status="cancelled" if isinstance(exc, core.Cancelled) else "failed", reason=str(exc),
                             partial_path=str(getattr(exc, "partial_path", ""))))
            core.save_manifest(folder, rows, rows[-1]["status"])
            raise

    def _wait_star(self, row):
        """Safe retry of THIS field, no hidden next-field/reposition command."""
        self._state("awaiting_star")
        self.event.emit(dict(type="awaiting_star", angle=row["angle"], dec=row["dec"]))
        while True:
            self.cancel.check()
            try:
                name, p = self.commands.get(timeout=.1)
            except queue.Empty:
                continue
            self.cancel.check()
            if name == "skip":
                return False
            if name not in {"select", "auto_select"}:
                continue
            # Time spent selecting changes HA if not tracking. Never reuse a
            # stale target or automatically slew on a manual retry.
            self._verify_pose(row["angle"], row["dec"], row["expected_pier"])
            if self._acquire(p.get("position") if name == "select" else None):
                self._verify_pose(row["angle"], row["dec"], row["expected_pier"])
                return True
            self.log.emit("Retry found no qualified star; select another or Skip")
            self._state("awaiting_star")

    def _map(self, rows):
        folder = core.create_run(self.cfg.out)
        for row in rows:
            if row["status"] == "ready":
                row["status"] = "pending"
        status = "running"
        core.save_manifest(folder, rows, status)
        try:
            for index, row in enumerate(rows):
                self.cancel.check()
                if row["status"] == "skipped":
                    continue
                row["status"] = "running"
                core.save_manifest(folder, rows, status)
                self.event.emit(dict(type="map_point", index=index, total=len(rows), angle=row["angle"]))
                pose = self._slew(row["angle"], row["dec"])
                row["expected_pier"] = pose["pier"]
                if not self._acquire() and not self._wait_star(row):
                    row.update(status="skipped", reason="Operator skipped: no selected star")
                else:
                    measured = self._record(self._perform(), folder, f"point_{index:03d}", row["angle"])
                    row.update(measured)
                core.save_manifest(folder, rows, status)
                self.rows_changed.emit([dict(r) for r in rows])
            status = "completed"
        except Exception as exc:
            status = "cancelled" if isinstance(exc, core.Cancelled) else "failed"
            for row in rows:
                if row["status"] == "running":
                    row.update(status=status, reason=str(exc), partial_path=str(getattr(exc, "partial_path", "")))
                elif row["status"] == "pending":
                    row.update(status="cancelled" if status == "cancelled" else "skipped", reason="Map ended before this point")
            raise
        finally:
            core.save_manifest(folder, rows, status)
            self.rows_changed.emit([dict(r) for r in rows])


class ConnectionDialog(QtWidgets.QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect one mount + one camera")
        self.resize(540, 250)
        form = QtWidgets.QFormLayout(self)
        self.simulation = QtWidgets.QCheckBox("Simulation — no hardware")
        self.simulation.setChecked(True)
        self.telescope = QtWidgets.QLineEdit()
        self.camera = QtWidgets.QComboBox()
        self.camera.setEditable(True)
        self.camera.addItem("")
        for item in settings.value("camera_ids", [], type=list):
            if item != "simulator":
                self.camera.addItem(item)
        self.telescope.setPlaceholderText("Blank opens ASCOM telescope chooser")
        self.camera.lineEdit().setPlaceholderText("Blank opens ASCOM camera chooser (any vendor)")
        form.addRow(self.simulation)
        form.addRow("Telescope ProgID", self.telescope)
        form.addRow("Camera ProgID / saved profile", self.camera)
        note = QtWidgets.QLabel("Close other camera clients and stop guiding/dithering first.\n"
                                "Connection alone sends no mount motion or tracking commands.")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.simulation.toggled.connect(lambda on: (self.telescope.setEnabled(not on), self.camera.setEnabled(not on)))
        self.telescope.setEnabled(False)
        self.camera.setEnabled(False)


DARK_STYLE = """
QMainWindow, QDialog, QWidget { background: #101923; color: #dbe5ef; font-size: 12px; }
QLabel#title { font-size: 22px; font-weight: 650; color: #f0f7ff; }
QLabel#banner { background: #213d51; color: #91ddff; padding: 10px; border-radius: 6px; font-weight: 600; }
QGroupBox { border: 1px solid #304253; border-radius: 7px; margin-top: 16px; padding: 12px 7px 7px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QPushButton { background: #263b4d; border: 1px solid #40596c; border-radius: 5px; padding: 7px 12px; }
QPushButton:hover { background: #34516a; } QPushButton:disabled { color: #667685; background: #192630; border-color: #263541; }
QPushButton#primary { background: #116c8c; color: white; font-weight: bold; }
QPushButton#primary:disabled { background: #203d49; color: #788e9a; }
QPushButton#stop { background: #a53d43; color: white; font-weight: bold; font-size: 14px; }
QPushButton#stop:disabled { background: #493039; color: #97797f; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit { background: #172633; border: 1px solid #344959; border-radius: 4px; padding: 4px; selection-background-color: #176f8d; }
QTableWidget { background: #14212c; alternate-background-color: #1b2b38; gridline-color: #2b3f50; border: 0; }
QHeaderView::section { background: #233848; color: #b6ccde; padding: 5px; border: 0; }
QTabBar::tab { background: #192b3a; padding: 8px 18px; } QTabBar::tab:selected { background: #28516a; }
QSplitter::handle { background: #263949; } QScrollArea { border: 0; }
QStatusBar { background: #192b38; } QToolTip { background: #294254; color: white; border: 1px solid #6c91aa; }
QFrame#verdict { background: #1b2b38; border: 2px solid #40596c; border-radius: 8px; }
QFrame#verdict[verdict="large"] { background: #4a2327; border-color: #d4545c; }
QFrame#verdict[verdict="acceptable"] { background: #1d4030; border-color: #4fc98a; }
QFrame#verdict[verdict="borderline"], QFrame#verdict[verdict="none_detected"], QFrame#verdict[verdict="inconclusive"] { background: #46391c; border-color: #e0b04a; }
QFrame#verdict[verdict="measured"] { background: #1d3a4d; border-color: #4bb4e0; }
QFrame#verdict[verdict="unreliable"] { background: #3a2a2a; border-color: #a06060; }
QLabel#verdict_headline { font-size: 17px; font-weight: bold; color: #ffffff; background: transparent; }
QFrame#verdict QLabel, QFrame#verdict QCheckBox { background: transparent; }
"""


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, parent=None, *, start_worker=True, settings=None):
        super().__init__(parent)
        self.setWindowTitle("RA Backlash · Observatory Workbench")
        self.resize(1500, 980)
        self.setMinimumSize(980, 680)
        self.settings = settings or QtCore.QSettings("RA Backlash", "Desktop")
        self.state = "disconnected"
        self.connected = False
        self.simulation = True
        self.pending = False
        self.closing = False
        self.camera_id = "simulator"
        self.connected_binning = 1
        self.current_connection = None
        self.can_slew = False
        self.pose = None
        self.rows = []
        self.baseline = []
        self.last_result = None
        self._assessment_result = None
        self._display_workflow = None
        self.trail = deque(maxlen=2000)
        self.samples = deque(maxlen=12000)
        self._trajectory_dirty = False
        self._pending_markers = []
        self._trajectory_drawn = 0.
        self._trajectory_timer = QtCore.QTimer(self)
        self._trajectory_timer.setSingleShot(True)
        self._trajectory_timer.timeout.connect(self._flush_trajectory)
        self.calibration = None
        self.selected_position = None
        self._phase_description = ""
        self._map_progress = ""
        self._auto_range = True
        self._build_ui()
        self.setStyleSheet(DARK_STYLE)
        self.worker = DeviceWorker(self)
        self.worker.state_changed.connect(self._on_state)
        self.worker.ready.connect(self._on_ready)
        self.worker.event.connect(self._on_event)
        self.worker.frame.connect(self._on_frame)
        self.worker.result.connect(self._on_result)
        self.worker.rows_changed.connect(self._on_rows)
        self.worker.log.connect(self._log)
        self.worker.finished.connect(self._worker_finished)
        if start_worker:
            self.worker.start()
        self._gate()
        self._update_guidance()
        self._log("Simulator selected by default. Software Stop cannot interrupt a hung driver; keep native mount controls available.")

    def _build_ui(self):
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        title = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel("RA BACKLASH  /  Workbench")
        label.setObjectName("title")
        title.addWidget(label)
        title.addStretch()
        self.state_label = QtWidgets.QLabel("DISCONNECTED")
        title.addWidget(self.state_label)
        layout.addLayout(title)
        self.banner = QtWidgets.QLabel("SIMULATION DEFAULT · No hardware connected · RA-axis arcseconds, not on-sky arcseconds")
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        layout.addWidget(self.banner)
        self.guidance_label = QtWidgets.QLabel()
        self.guidance_label.setWordWrap(True)
        self.guidance_label.setObjectName("banner")
        layout.addWidget(self.guidance_label)
        self.verdict_card = QtWidgets.QFrame()
        self.verdict_card.setObjectName("verdict")
        card = QtWidgets.QVBoxLayout(self.verdict_card)
        self.verdict_headline = QtWidgets.QLabel()
        self.verdict_headline.setObjectName("verdict_headline")
        self.verdict_detail = QtWidgets.QLabel()
        for label in (self.verdict_headline, self.verdict_detail):
            label.setWordWrap(True)
            label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            card.addWidget(label)
        limit_row = QtWidgets.QHBoxLayout()
        self.target_check = QtWidgets.QCheckBox("Compare with my acceptable limit:")
        self.target_check.setChecked(self.settings.value("limit/enabled", True, type=bool))
        self.target_spin = QtWidgets.QDoubleSpinBox()
        self.target_spin.setDecimals(1)
        self.target_spin.setRange(0., 1_000_000.)
        self.target_spin.setValue(self.settings.value("limit/arcsec", 10., type=float))
        self.target_spin.setSuffix(" arcsec")
        self.target_spin.setEnabled(self.target_check.isChecked())
        limit_tip = ("Starting value only. This app does not know your mount's specification or your guiding "
                     "error. A common-sense rule: backlash matters when it is larger than your typical guiding "
                     "error or seeing. Set a value that suits your setup. It is only used to word the result "
                     "and is remembered on this computer. It is not saved in the measurement files.")
        self.target_check.setToolTip(limit_tip)
        self.target_spin.setToolTip(limit_tip)
        limit_row.addWidget(self.target_check)
        limit_row.addWidget(self.target_spin)
        limit_row.addWidget(QtWidgets.QLabel("(starting guess — set your own; not a manufacturer spec)"))
        limit_row.addStretch()
        card.addLayout(limit_row)
        layout.addWidget(self.verdict_card)
        split = QtWidgets.QSplitter()
        layout.addWidget(split, 1)
        sidebar_scroll = QtWidgets.QScrollArea()
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setMinimumWidth(345)
        sidebar_scroll.setMaximumWidth(430)
        sidebar = QtWidgets.QWidget()
        self.sidebar_layout = QtWidgets.QVBoxLayout(sidebar)
        sidebar_scroll.setWidget(sidebar)
        split.addWidget(sidebar_scroll)
        connection = QtWidgets.QGroupBox("Step 0 · Connect")
        box = QtWidgets.QVBoxLayout(connection)
        self.device_label = QtWidgets.QLabel("No devices connected")
        self.device_label.setWordWrap(True)
        box.addWidget(self.device_label)
        self.connect_button = self._button("Connect camera && mount… (or Simulation)", self._connect_dialog, box)
        self.disconnect_button = self._button("Disconnect", lambda: self._launch("disconnect"), box)
        self.sidebar_layout.addWidget(connection)
        self.settings_box = QtWidgets.QGroupBox("Camera && measurement settings")
        form = QtWidgets.QFormLayout(self.settings_box)
        self.fields = {}
        self._field(form, "exposure", "Exposure per frame (s)", .1, .001, 30, 3)
        self._field(form, "acq_exposure", "Exposure for finding stars (s)", 1., .001, 30, 3)
        self._field(form, "binning", "Pixel binning (set when connecting)", 1, 1, 16)
        self._field(form, "roi", "Star tracking box size (px)", 160, 24, 4096)
        self._field(form, "cycles", "Repeats (more = steadier, slower)", 3, 1, 30)
        self.sidebar_layout.addWidget(self.settings_box)
        self.advanced_box = QtWidgets.QGroupBox("Advanced (defaults are fine)")
        adv = QtWidgets.QFormLayout(self.advanced_box)
        for key, label, value, low, high in (
            ("min_alt", "Lowest allowed altitude (°)", 25., 0., 85.),
            ("dec", "Declination to test at (°)", 0., -75., 75.),
            ("settle", "Wait after moving (s)", 3., 0., 120.),
            ("rate", "Back-and-forth speed (× sidereal)", 2., 1.01, 20.),
            ("phase", "Seconds per back-and-forth leg", 5., 2., 120.),
            ("cal", "Seconds for star-motion calibration", 4., 2., 120.),
            ("min_snr", "Minimum star brightness (SNR)", 6., 1., 100.)):
            self._field(adv, key, label, value, low, high, 2)
        self.sidebar_layout.addWidget(self.advanced_box)
        self.planner_box = QtWidgets.QGroupBox("Optional · measure at several angles")
        form = QtWidgets.QFormLayout(self.planner_box)
        self._field(form, "count", "Angles per side", 3, 1, 20)
        self._field(form, "first", "First angle (°)", 25., .1, 82., 2)
        self._field(form, "spacing", "Step between angles (°)", 25., .1, 82., 2)
        self.side = QtWidgets.QComboBox()
        self.side.addItems(["both", "negative", "positive"])
        form.addRow("Which side(s) of the meridian*", self.side)
        self.angle_list = QtWidgets.QLineEdit()
        self.angle_list.setPlaceholderText("Or type your own: -75,-50,-25,25,50,75")
        form.addRow("Custom angle list", self.angle_list)
        self.geometry_check = QtWidgets.QCheckBox("I verified northern, counterweight-down\nGEM geometry and driver pier interpretation")
        self.geometry_check.setToolTip("Operator validation is not a guarantee of collision clearance. Unsupported geometry is refused.")
        form.addRow(self.geometry_check)
        self.plan_button = QtWidgets.QPushButton("Check which angles are reachable")
        self.plan_button.clicked.connect(lambda: self._plan_or_map(False))
        form.addRow(self.plan_button)
        self.map_button = QtWidgets.QPushButton("Measure at several angles…")
        self.map_button.clicked.connect(lambda: self._plan_or_map(True))
        form.addRow(self.map_button)
        form.addRow(QtWidgets.QLabel("* Angles are signed mount positions, not the physical pier side."))
        self.refine_button = QtWidgets.QPushButton("Measure more around the best angle…")
        self.refine_button.clicked.connect(self._refine)
        form.addRow(self.refine_button)
        self.sidebar_layout.addWidget(self.planner_box)
        self.sidebar_layout.addStretch()
        center = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        split.addWidget(center)
        split.setSizes([365, 1135])
        split.setStretchFactor(1, 1)
        image_panel = QtWidgets.QWidget()
        image_layout = QtWidgets.QVBoxLayout(image_panel)
        actions = QtWidgets.QHBoxLayout()
        self.preview_button = self._button("1 · Take picture", lambda: self._launch("preview", continuous=False), actions)
        self.live_button = self._button("Live view (optional)", lambda: self._launch("preview", continuous=True), actions)
        self.auto_button = self._button("2 · Pick star automatically", lambda: self._launch("auto_select"), actions)
        self.measure_button = self._button("3 · Measure backlash", self._measure, actions)
        self.measure_button.setObjectName("primary")
        self.skip_button = self._button("Skip this position", lambda: self._launch("skip"), actions)
        self._button("Reset zoom", self._fit_image, actions)
        image_layout.addLayout(actions)
        views = QtWidgets.QSplitter()
        self.image_plot = pg.PlotWidget(title="Camera picture — click a star to select it")
        self.image_plot.setAspectLocked(True)
        self.image_plot.invertY(True)
        self.image_plot.setLabel("bottom", "Sensor x", units="px")
        self.image_plot.setLabel("left", "Sensor y", units="px")
        self.image_item = pg.ImageItem(axisOrder="row-major")
        self.image_plot.addItem(self.image_item)
        self.trail_item = self.image_plot.plot(pen=pg.mkPen("#47dfed", width=1.5))
        self.star_item = self.image_plot.plot(pen=None, symbol="+", symbolPen="#e9f4ff", symbolSize=18)
        self.bracket_item = self.image_plot.plot(pen=pg.mkPen("#ffac59", width=2))
        self.direction_item = self.image_plot.plot(pen=pg.mkPen("#92a7bc", style=QtCore.Qt.PenStyle.DashLine))
        self.image_plot.scene().sigMouseClicked.connect(self._image_clicked)
        views.addWidget(self.image_plot)
        self.zoom_plot = pg.PlotWidget(title="Selected star (zoom)")
        self.zoom_plot.setAspectLocked(True)
        self.zoom_plot.invertY(True)
        self.zoom_item = pg.ImageItem(axisOrder="row-major")
        self.zoom_plot.addItem(self.zoom_item)
        views.addWidget(self.zoom_plot)
        views.setSizes([850, 230])
        image_layout.addWidget(views, 1)
        self.image_legend_label = QtWidgets.QLabel(
            '<b>Image key:</b> <span style="color:#47dfed">Cyan line: observed star path</span> · '
            '<span style="color:#e9f4ff">White cross: selected star</span> · '
            '<span style="color:#92a7bc">Gray dashed line: calibrated RA direction</span> · '
            '<span style="color:#ffac59">Orange bracket: inferred backlash, completed fit only</span>. '
            'Bracket is not extra observed movement or seeing removal.')
        self.image_legend_label.setWordWrap(True)
        image_layout.addWidget(self.image_legend_label)
        self.quality_label = QtWidgets.QLabel("Star quality appears after acquisition · absolute binned sensor coordinates")
        self.quality_label.setWordWrap(True)
        image_layout.addWidget(self.quality_label)
        center.addWidget(image_panel)
        self.tabs = tabs = QtWidgets.QTabWidget()
        self.assessment_panel = QtWidgets.QScrollArea()
        self.assessment_panel.setWidgetResizable(True)
        assessment_body = QtWidgets.QWidget()
        assessment_layout = QtWidgets.QVBoxLayout(assessment_body)
        self.assessment_panel.setWidget(assessment_body)
        self.assessment_title = QtWidgets.QLabel()
        self.assessment_title.setObjectName("title")
        self.assessment_meaning = QtWidgets.QLabel()
        self.assessment_action = QtWidgets.QLabel()
        self.assessment_warning = QtWidgets.QLabel()
        for label in (self.assessment_title, self.assessment_meaning,
                      self.assessment_action, self.assessment_warning):
            label.setWordWrap(True)
            label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            assessment_layout.addWidget(label)
        goal_note = QtWidgets.QLabel(
            "The acceptable limit above is a comparison goal you choose — not a manufacturer tolerance or safe mesh gap. "
            "It only changes the wording of the result and never reruns a measurement or moves hardware.")
        goal_note.setWordWrap(True)
        assessment_layout.addWidget(goal_note)
        self.assessment_details = QtWidgets.QPlainTextEdit()
        self.assessment_details.setReadOnly(True)
        self.assessment_details.setMinimumHeight(90)
        assessment_layout.addWidget(self.assessment_details)
        self.map_assessment_label = QtWidgets.QLabel()
        self.map_assessment_label.setWordWrap(True)
        self.map_assessment_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.map_assessment_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        empty_map = assess_map([])
        self.map_assessment_label.setText("Map summary (separate from selected point)\n" +
            "\n".join([empty_map["title"], empty_map["meaning"], empty_map["action"]]))
        assessment_layout.addWidget(self.map_assessment_label)
        self.target_check.toggled.connect(self._reassess)
        self.target_spin.valueChanged.connect(self._reassess)
        self.target_check.toggled.connect(self._save_limit)
        self.target_spin.valueChanged.connect(self._save_limit)
        tabs.addTab(self.assessment_panel, "Result details")
        self.time_plot = pg.PlotWidget(title="Raw sensor coordinates vs time · calibration required for RA projection")
        self.time_plot.setMinimumHeight(130)
        self.time_plot.addLegend(colCount=2, brush=pg.mkBrush("#172633"),
                     pen=pg.mkPen("#40596c"), labelTextColor="#dbe5ef")
        self.time_plot.setLabel("bottom", "Time from first valid sample", units="s")
        self.x_curve = self.time_plot.plot(pen="#48dfea", symbol="o", symbolSize=3, connect="finite")
        self.y_curve = self.time_plot.plot(pen="#9a9eea", symbol="o", symbolSize=3, connect="finite")
        self.fit_items = []
        self._command_sample = pg.PlotDataItem(pen=pg.mkPen("#92a7bc", style=QtCore.Qt.PenStyle.DotLine))
        self._before_sample = pg.PlotDataItem(pen=pg.mkPen("#77d6a5", width=2))
        self._after_sample = pg.PlotDataItem(pen=pg.mkPen("#ffac59", width=2))
        self._trajectory_legend_names = []
        self._trajectory_style = None
        self._has_commands = self._has_fits = False
        self._pending_phase = False
        self.trajectory_caption = QtWidgets.QLabel()
        self.trajectory_caption.setWordWrap(True)
        self.result_summary = QtWidgets.QLabel()
        self.result_summary.setWordWrap(True)
        self.result_summary.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.trajectory_panel = QtWidgets.QWidget()
        trajectory_layout = QtWidgets.QVBoxLayout(self.trajectory_panel)
        trajectory_layout.addWidget(self.result_summary)
        trajectory_layout.addWidget(self.time_plot, 1)
        trajectory_layout.addWidget(self.trajectory_caption)
        tabs.addTab(self.trajectory_panel, "Trajectory graph")
        self.map_plot = pg.PlotWidget(title="Measured backlash · diagnostic allowances, not confidence intervals")
        self.map_plot.setLabel("bottom", "Signed mechanical angle (not pier side)", units="°")
        self.map_plot.setLabel("left", "RA-axis backlash", units="arcsec")
        self.map_plot.setMinimumHeight(130)
        self.map_plot.addLegend(colCount=2, brush=pg.mkBrush("#172633"),
                    pen=pg.mkPen("#40596c"), labelTextColor="#dbe5ef")
        self.map_caption = QtWidgets.QLabel()
        self.map_caption.setWordWrap(True)
        self.map_panel = QtWidgets.QWidget()
        map_layout = QtWidgets.QVBoxLayout(self.map_panel)
        map_layout.addWidget(self.map_plot, 1)
        map_layout.addWidget(self.map_caption)
        tabs.addTab(self.map_panel, "Several-angle map")
        self._update_map_caption()
        self._reset_measurement_display()
        self.table = QtWidgets.QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels(["Angle °", "HA h", "Dec °", "Alt °", "Status / quality", "Backlash (arcsec)", "± allowance (arcsec)", "Fwd → rev", "Rev → fwd", "N", "Reason / comparison"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._gate)
        self.table.itemSelectionChanged.connect(self._assess_selection)
        tabs.addTab(self.table, "Measurements table")
        center.addWidget(tabs)
        center.setSizes([620, 180])
        footer = QtWidgets.QHBoxLayout()
        self.import_button = self._button("Open saved results…", lambda: self._import(False), footer)
        self.baseline_button = self._button("Compare with saved results…", lambda: self._import(True), footer)
        self.candidate_button = self._button("Go back to selected angle to adjust…", self._return_candidate, footer)
        self.finish_button = self._button("Done adjusting — resume tracking…", self._finish_adjustment, footer)
        self.stop_button = self._button("■  STOP", self._stop, footer)
        self.stop_button.setObjectName("stop")
        self.stop_button.setMinimumWidth(120)
        layout.addLayout(footer)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1500)
        self.log_view.setMaximumHeight(80)
        layout.addWidget(self.log_view)
        self.pose_label = QtWidgets.QLabel("No pose · Stop is not a hardware emergency stop")
        self.statusBar().addWidget(self.pose_label, 1)
        refresh = QtWidgets.QPushButton("Refresh pose")
        refresh.clicked.connect(lambda: self._launch("pose"))
        self.pose_button = refresh
        self.statusBar().addPermanentWidget(refresh)
        for button, tip in (
            (self.connect_button, "Connects only: no picture is taken and the mount does not move. Choose Simulation to practice."),
            (self.preview_button, "Step 1: take one picture of the whole sensor so you can see the stars. The mount does not move."),
            (self.live_button, "Keep taking pictures until you press STOP. Stop it before measuring."),
            (self.auto_button, "Step 2: let the app choose an isolated star. You can also just click a star in the picture."),
            (self.measure_button, "Step 3: measure the backlash at the current position. Takes about a minute and a half on a real mount; asks before moving. Picks a star itself if you have not."),
            (self.map_button, "Repeat the measurement at several angles to see where the mount is tightest or loosest. Moves the mount; picks its own star at each angle; asks first."),
            (self.plan_button, "Lists which angles can be reached. Nothing moves and no pictures are taken."),
            (self.stop_button, "Ask the app to stop. This is a software request, not an emergency stop; use the mount's own controls if a driver hangs."),
            (self.skip_button, "Skip this angle instead of measuring an unselected star."),
        ):
            button.setToolTip(tip)

    def _button(self, text, callback, layout):
        button = QtWidgets.QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _field(self, form, key, label, value, low, high, decimals=None):
        widget = QtWidgets.QSpinBox() if decimals is None else QtWidgets.QDoubleSpinBox()
        if decimals is not None:
            widget.setDecimals(decimals)
        widget.setRange(low, high)
        widget.setValue(value)
        self.fields[key] = widget
        form.addRow(label, widget)

    def _config(self):
        keys = asdict(core.Config())
        values = {key: widget.value() for key, widget in self.fields.items() if key in keys}
        return core.Config(**values).validate()

    def _angles(self):
        text = self.angle_list.text().strip()
        if text:
            values = [float(v.strip()) for v in text.split(",")]
            if not values or len(values) > 60 or len(set(values)) != len(values) or any(not math.isfinite(v) or v == 0 or abs(v) > 82 for v in values):
                raise ValueError("Use at most 60 unique nonzero finite angles within ±82°")
            return values
        return core.generate_angles(self.fields["count"].value(), self.fields["spacing"].value(),
                                    self.fields["first"].value(), self.side.currentText())

    def _launch(self, name, **payload):
        try:
            if name not in {"connect", "disconnect", "finish_adjustment", "skip", "pose"}:
                payload.setdefault("cfg", self._config())
            accepted = self.worker.submit(name, **payload)
            if accepted and name in {"measure", "slew_candidate", "map"}:
                self._display_workflow = name
                self.tabs.setCurrentWidget(self.map_panel if name == "map" else self.trajectory_panel)
            if accepted and self.connected and name not in {"connect", "skip", "pose"}:
                self._save_profile()
            if accepted and not (self.state in {"preview", "awaiting_star"} and name in {"select", "auto_select", "skip"}):
                self.pending = True
                self._gate()
            return accepted
        except Exception as exc:
            self._warn(str(exc))
            return False

    def _connect_dialog(self):
        dialog = ConnectionDialog(self.settings, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        try:
            camera = dialog.camera.currentText().strip()
            simulation = dialog.simulation.isChecked()
            self._load_profile("simulator" if simulation else camera)
            self._launch("connect", cfg=self._config(), simulation=simulation,
                         camera=camera, telescope=dialog.telescope.text().strip())
        except Exception as exc:
            self._warn(str(exc))

    def _load_profile(self, camera):
        if not camera:
            return
        prefix = "cameras/" + hashlib.sha256(camera.encode()).hexdigest() + "/"
        for key in ("exposure", "binning", "roi"):
            if self.settings.contains(prefix + key):
                self.fields[key].setValue(float(self.settings.value(prefix + key)) if key == "exposure" else int(self.settings.value(prefix + key)))

    def _save_profile(self):
        prefix = "cameras/" + hashlib.sha256(self.camera_id.encode()).hexdigest() + "/"
        for key in ("exposure", "binning", "roi"):
            self.settings.setValue(prefix + key, self.fields[key].value())
        ids = self.settings.value("camera_ids", [], type=list)
        if self.camera_id not in ids:
            self.settings.setValue("camera_ids", ids + [self.camera_id])
        self.settings.sync()

    def _confirm_motion(self, description):
        if not self.simulation and not self.geometry_check.isChecked():
            self._warn("First validate the supported northern counterweight-down GEM geometry and driver pier interpretation.")
            return False
        pose = self.pose or {}
        text = (description + f"\n\nCurrent pier: {pose.get('pier', 'unknown')}; tracking: {pose.get('tracking', 'unknown')}.\n"
                "Targets are recomputed from current LST. Driver async slew and actual/destination pier states are checked.\n\n"
                "Confirm driver limits remain enabled, guiding is stopped, and the complete path / possible meridian crossing is clear of collisions and cable snags. "
                "Endpoint altitude checks do not prove a safe path. Keep native controls available. No software safety guarantee.")
        return QtWidgets.QMessageBox.question(self, "Confirm motion and geometry", text,
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No) == QtWidgets.QMessageBox.StandardButton.Yes

    def _plan_or_map(self, move):
        try:
            angles = self._angles()
            cfg = self._config()
            if move and not self._confirm_motion(
                    f"Measure backlash at {len(angles)} angles: {', '.join(f'{a:+g}°' for a in angles)}.\n"
                    f"At each angle the mount moves there, the app picks its own star (the star you clicked is not used), "
                    f"then runs the full measurement — about {self._estimated_minutes()} min per angle on a real mount "
                    f"(simulation: seconds).\n"
                    f"Declination {cfg.dec:g}°, lowest altitude {cfg.min_alt:g}°; reachable angles may differ — use 'Check which angles are reachable' first."):
                return
            self._save_profile()
            self._launch("map" if move else "plan", angles=angles, confirmed=move,
                         geometry_verified=self.geometry_check.isChecked())
        except Exception as exc:
            self._warn(str(exc))

    def _measure(self):
        if QtWidgets.QMessageBox.question(self, "Start the backlash measurement?",
                "The mount will stop tracking and drive RA slowly back and forth for about "
                f"{self._estimated_minutes()} minute(s) on a real mount (a simulation takes seconds). "
                "Stop guiding first, make sure nothing can collide or snag, and keep the mount's own controls within reach.\n\nStart?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._save_profile()
        self._launch("measure")

    def _selected_candidate(self):
        index = self.table.currentRow()
        if not 0 <= index < len(self.rows):
            return None
        row = self.rows[index]
        if core.candidate([row]) is None:
            return None
        return row

    def _return_candidate(self):
        row = self._selected_candidate()
        if not row or row.get("imported") or not row.get("pose"):
            return
        pose = row["pose"]
        if self._confirm_motion(f"Return to selected valid measured candidate: requested angle {row['angle']:.3f}°, Dec {pose['dec']:.3f}°, expected pier {pose['pier']}.\n"
                    "This MOVES the mount: slew, fresh star acquisition, calibration and RA reversal confirmation measurement, then a SECOND slew to the requested angle because measurement moves the axis.\n"
                    "Only a saved quality-valid confirmation and verified final pose / tracking OFF permit stationary adjustment. Failed, uncertain or cancelled confirmation refuses adjustment. No automatic mesh changes; a low value is not a binding diagnosis."):
            self._launch("slew_candidate", point_id=row["point_id"], confirmed=True,
                         geometry_verified=self.geometry_check.isChecked())

    def _refine(self):
        row = self._selected_candidate()
        if not row:
            return
        angle = finite(row.get("angle"))
        angles = [round(angle + delta, 4) for delta in (-5, -2.5, 0, 2.5, 5)]
        if any(a == 0 or abs(a) > 82 or a * angle <= 0 for a in angles):
            self._warn("Refinement would cross zero or exceed ±82°. Enter a bounded explicit list instead.")
            return
        if self._confirm_motion(f"User-triggered refinement; explicit bounded list: {angles}. No inferred unsampled minimum."):
            self._launch("map", angles=angles, confirmed=True, geometry_verified=self.geometry_check.isChecked())

    def _finish_adjustment(self):
        if QtWidgets.QMessageBox.question(self, "Enable tracking / movement?",
                "Confirm all hands/tools are clear and motion may resume. This enables tracking only; no automatic slew. Reacquire and recalibrate for another measurement.",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No) == QtWidgets.QMessageBox.StandardButton.Yes:
            self._launch("finish_adjustment", confirmed=True)

    def _stop(self):
        self.worker.stop()
        self.pending = True
        self._on_state("stopping")
        self._log("Stop requested; waiting for cooperative cleanup. No return slew. Use native controls if driver hangs.")

    @QtCore.Slot(str)
    def _on_state(self, state):
        self.state = state
        self._phase_description = ""
        if state in {"preview", "awaiting_star"}:
            self.pending = False
        self.state_label.setText(state.replace("_", " ").upper())
        if state == "adjustment":
            self.banner.setText("ADJUSTMENT INTERLOCK · RA stopped / tracking OFF reported · No automatic motion · Motors may remain energized")
        elif self.connected:
            self.banner.setText("SIMULATION · Synthetic camera and mount · No hardware control" if self.simulation else
                                "ASCOM HARDWARE · Single camera / single worker · Cooperative Stop only; keep native controls available")
        self._update_guidance()
        self._gate()

    def _update_guidance(self, message=None):
        if message is None:
            message = {
                "disconnected": "Click 'Connect camera & mount…'. Choose Simulation to practice without hardware.",
                "connecting": "Connecting. Nothing moves and no pictures are taken yet.",
                "idle": ("Star selected. Click '3 · Measure backlash'." if self.selected_position is not None else
                         "Connected. Click '1 · Take picture' to see the stars."),
                "preview": "Picture shown. Click '2 · Pick star automatically', or click a bright, isolated star in the picture. Stop Live view before measuring.",
                "acquiring": "Locating the star (reacquiring). Wait a moment.",
                "awaiting_star": "No usable star here. Click a bright isolated star in the picture, press 'Pick star automatically' to retry, or 'Skip this position'.",
                "calibrating": "Learning how the star moves with the mount (tracking is off for a few seconds). Wait, or press STOP.",
                "measuring": "Moving RA back and forth to find the slack in the gears. Wait, or press STOP.",
                "analyzing": "Calculating the result from the recorded star positions.",
                "completed": "Done. Read the coloured result box above; the 'Result details' tab explains it.",
                "fault": "Something went wrong. Check the log below, use the mount's own controls if needed, then disconnect and reconnect.",
                "adjustment": "Adjustment mode: RA stopped and tracking is off. Clear hands and tools, then press 'Done adjusting' when finished; measure again afterwards.",
                "stopping": "Stopping. Waiting for the software to finish cleanly (a cooperative stop, not a hardware stop). Use the mount's own controls if a driver hangs.",
                "slewing": "Moving the mount to the next test angle. Wait, or press STOP. Keep the mount's own controls within reach.",
                "settling": "Waiting for the mount to settle before choosing a star.",
            }.get(self.state, "Wait for the current operation to finish, or press STOP.")
        if self._map_progress:
            message = f"{self._map_progress} — {message}"
        timing = " Simulation runs in fast virtual time, so phases may flash by." if self.simulation else ""
        self.guidance_label.setText(
            "<b>Next:</b> " + html.escape(message + timing) +
            "<br><b>Steps:</b> 1 Take picture → 2 Pick a star (automatic, or click one) → 3 Measure backlash. "
            "Step 3 tests one position and auto-picks a star if you skip step 2. "
            "'Measure at several angles' (left panel) repeats it at each angle.")

    def _phase_step(self, phase):
        cycles = self.fields["cycles"].value()
        total = 5 + 2 * cycles
        fixed = ["drift", "sign", "sign_settle", "latency_fast", "latency_slow"]
        if phase in fixed:
            return fixed.index(phase) + 1, total
        kind, _, number = phase.partition("_")
        if kind in {"reverse", "forward"} and number.isdigit() and 1 <= int(number) <= cycles:
            return 5 + 2 * (int(number) - 1) + (1 if kind == "reverse" else 2), total
        return None

    def _estimated_minutes(self):
        """Real-mount time for one angle, excluding slews and star acquisition."""
        cfg = self._config()
        forward = cfg.phase * (1 + cfg.rate) / (cfg.rate - 1)
        seconds = 2 * cfg.cal + 13 + cfg.cycles * (cfg.phase + forward)
        return max(1, round(seconds / 60))

    def _save_limit(self, *_):
        self.settings.setValue("limit/enabled", self.target_check.isChecked())
        self.settings.setValue("limit/arcsec", self.target_spin.value())

    def _set_verdict(self, verdict, headline, detail):
        self.verdict_headline.setText(headline)
        self.verdict_detail.setText(detail)
        if self.verdict_card.property("verdict") != verdict:
            self.verdict_card.setProperty("verdict", verdict)
            self.verdict_card.style().unpolish(self.verdict_card)
            self.verdict_card.style().polish(self.verdict_card)

    def _reset_measurement_display(self):
        """Display-only reset at actual drift start, including map/confirmation points."""
        self.samples.clear()
        self.trail.clear()
        self.calibration = None
        self.last_result = None
        self._assessment_result = None
        self._reassess()
        self._pending_phase = False
        self._pending_markers.clear()
        self._trajectory_dirty = False
        self._has_commands = self._has_fits = False
        for item in self.fit_items:
            self.time_plot.removeItem(item)
        self.fit_items.clear()
        for item in (self.trail_item, self.star_item, self.direction_item, self.bracket_item,
                     self.x_curve, self.y_curve):
            item.setData([], [])
        self.result_summary.setText("No completed fit yet — B and quality reasons will appear here.")
        self._update_trajectory()

    def _trajectory_calibration(self):
        calibration = self.calibration or {}
        try:
            u = np.asarray(calibration.get("u", []), dtype=float)
        except (TypeError, ValueError):
            return None
        v0 = finite(calibration.get("v0"))
        if (u.shape == (2,) and np.isfinite(u).all() and
                abs(np.linalg.norm(u) - 1) < .001 and v0 > 0):
            return u, v0
        return None

    def _apply_trajectory_style(self, calibrated):
        self.time_plot.setLabel("left", "RA / orthogonal displacement (RA-axis-equivalent)" if calibrated else
                                "Sensor x / y displacement", units="arcsec" if calibrated else "px")
        self.time_plot.setTitle("Calibrated RA-axis trajectory · relative to first valid sample" if calibrated else
                               "Raw sensor displacement · awaiting drift calibration")
        self.trajectory_caption.setText(
            ("Calibrated: RA + orthogonal displacement in RA-axis-equivalent arcsec, both relative to the first valid sample. " if calibrated else
             "Before calibration: sensor x + y displacement in pixels, relative to the first valid sample. ") +
            "Observed star displacement includes intentional drives — it is NOT backlash alone or seeing removal. "
            "Gray dotted = command / phase time (live: first sample); green = fit before, orange = fit after command. "
            "Orange trajectory fit is NOT the image's inferred-backlash bracket. Missing samples leave gaps.")

    def _update_trajectory(self):
        """One origin/unit system for live samples and completed raw frames; retain gaps."""
        calibrated = self._trajectory_calibration()
        names = (["RA displacement (cyan)", "Orthogonal displacement (purple)"] if calibrated else
                 ["Sensor x displacement (cyan)", "Sensor y displacement (purple)"])
        entries = [(self.x_curve, names[0]), (self.y_curve, names[1])]
        if self._has_commands:
            entries.append((self._command_sample, "Command / phase time (gray dotted)"))
        if self._has_fits:
            entries.extend([(self._before_sample, "Fit before command (green)"),
                            (self._after_sample, "Fit after command (orange)")])
        labels = [name for _, name in entries]
        if labels != self._trajectory_legend_names:
            legend = self.time_plot.plotItem.legend
            legend.clear()
            for item, name in entries:
                legend.addItem(item, name)
            self._trajectory_legend_names = labels
        if self._trajectory_style != bool(calibrated):
            self._trajectory_style = bool(calibrated)
            self._apply_trajectory_style(bool(calibrated))
        data = np.asarray(self.samples, dtype=float).reshape(-1, 3)
        valid = np.isfinite(data).all(axis=1)
        if not valid.any():
            self.x_curve.setData([], [])
            self.y_curve.setData([], [])
            return None
        origin = data[np.flatnonzero(valid)[0]]
        xy = data[:, 1:] - origin[1:]
        if calibrated:
            u, v0 = calibrated
            xy = xy @ np.column_stack((u, [-u[1], u[0]])) * core.S_ARCSEC / v0
        xy[~valid] = np.nan
        times = data[:, 0] - origin[0]
        self.x_curve.setData(times, xy[:, 0], connect="finite")
        self.y_curve.setData(times, xy[:, 1], connect="finite")
        return origin

    def _request_trajectory(self):
        """Redraw at most ~10 Hz regardless of sample arrival rate."""
        self._trajectory_dirty = True
        wait = .1 - (time.monotonic() - self._trajectory_drawn)
        if wait <= 0:
            self._flush_trajectory()
        elif not self._trajectory_timer.isActive():
            self._trajectory_timer.start(max(1, int(wait * 1000)))

    def _flush_trajectory(self):
        self._trajectory_timer.stop()
        if not self._trajectory_dirty:
            return
        self._trajectory_dirty = False
        self._trajectory_drawn = time.monotonic()
        origin = self._update_trajectory()
        markers, self._pending_markers = self._pending_markers, []
        if origin is not None:
            for marker in markers:
                self._command_marker(marker - origin[0])
            if markers:
                self._update_trajectory()

    def _command_marker(self, t):
        if math.isfinite(finite(t)):
            item = pg.InfiniteLine(t, pen=pg.mkPen("#92a7bc", style=QtCore.Qt.PenStyle.DotLine))
            self.time_plot.addItem(item)
            self.fit_items.append(item)
            self._has_commands = True

    def _update_map_caption(self):
        empty = "Run 'Measure at several angles' to fill this chart. 'Measure backlash' gives one position, not a full map. "
        self.map_caption.setText(
            (empty if not self.rows else "Measured points only; unsampled angles are unknown. ") +
            "Current = cyan; saved baseline = purple. Circle = median; triangle = forward → reverse; "
            "square = reverse → forward. Whiskers = diagnostic allowance, NOT a confidence interval. "
            "Scatter only: no connections across unknown samples or pier sides.")

    @QtCore.Slot(str)
    def _on_ready(self, state):
        self.pending = False
        self._map_progress = ""
        self._on_state(state)
        if state == "completed" and self._display_workflow == "measure" and self.last_result is not None:
            self.tabs.setCurrentWidget(self.assessment_panel)
        self._display_workflow = None

    def _assessment_source(self, result):
        """Use known connection context only for live data, never for offline imports."""
        source = dict(result)
        if ("simulation" not in source and not source.get("imported") and self.connected
                and (source.get("connection") is None or source.get("connection") == self.current_connection)):
            source["simulation"] = self.simulation
        return source

    def _reassess(self, *_):
        self.target_spin.setEnabled(self.target_check.isChecked())
        if self._assessment_result is None:
            self._set_verdict("none", "No measurement yet",
                              "Follow the numbered buttons: 1 Take picture, 2 Pick star, 3 Measure backlash. "
                              "The result appears here as a number plus a verdict (large / acceptable / none detected).")
            self.assessment_title.setText("No completed result selected")
            self.assessment_meaning.setText("Measure backlash for one position, or select a saved/measured table row to interpret its evidence.")
            self.assessment_action.setText("Next action: repeatable measurements first; measure at several angles before mesh tuning.")
            self.assessment_warning.setText(assess_result(None)["binding_warning"])
            self.assessment_details.clear()
            return
        target = self.target_spin.value() if self.target_check.isChecked() else None
        assessment = assess_result(self._assessment_result, target)
        detail = assessment["summary"]
        if assessment["simulation"]:
            detail = "SIMULATION: practice data that says nothing about your real mount. " + detail
        self._set_verdict(assessment["verdict"], assessment["headline"], detail)
        self.assessment_title.setText(assessment["title"])
        self.assessment_title.setProperty("tone", assessment["tone"])
        self.assessment_meaning.setText(assessment["meaning"])
        self.assessment_action.setText("Next action: " + assessment["action"])
        self.assessment_warning.setText(assessment["binding_warning"])
        self.assessment_details.setPlainText("\n".join(assessment["details"]))

    def _assess_selection(self):
        index = self.table.currentRow()
        if 0 <= index < len(self.rows):
            self._assessment_result = self._assessment_source(self.rows[index])
            self._reassess()

    def _gate(self):
        idle = self.connected and self.state in IDLE_STATES and not self.pending and not self.closing
        free = not self.pending and not self.closing
        selecting = idle or (self.state in {"preview", "awaiting_star"} and free)
        self.connect_button.setEnabled(self.state == "disconnected" and free)
        self.disconnect_button.setEnabled((self.state == "fault" or self.connected and self.state in IDLE_STATES | {"adjustment"}) and free)
        self.settings_box.setEnabled((idle or self.state == "disconnected") and free)
        self.advanced_box.setEnabled((idle or self.state == "disconnected") and free)
        # Binning is a connection-time adapter setting, not a live mutation.
        self.fields["binning"].setEnabled(self.state == "disconnected" and free)
        for widget in (self.side, self.angle_list, self.geometry_check, self.fields["count"], self.fields["first"], self.fields["spacing"]):
            widget.setEnabled(idle)
        self.plan_button.setEnabled(idle)
        self.map_button.setEnabled(idle and self.can_slew)
        self.preview_button.setEnabled(idle)
        self.live_button.setEnabled(idle)
        self.auto_button.setEnabled(selecting)
        self.measure_button.setEnabled(idle)
        self.skip_button.setEnabled(self.state == "awaiting_star" and free)
        self.stop_button.setEnabled(self.connected and (self.state in ACTIVE_STATES or self.pending) and not self.closing)
        row = self._selected_candidate()
        live = bool(row and not row.get("imported") and row.get("geometry_verified") and row.get("pose")
                and row.get("connection") == self.current_connection)
        self.candidate_button.setEnabled(idle and self.can_slew and live)
        self.refine_button.setEnabled(idle and self.can_slew and bool(row))
        self.finish_button.setEnabled(self.state == "adjustment" and free)
        self.pose_button.setEnabled(idle)
        self.import_button.setEnabled(self.state not in ACTIVE_STATES and free and self.state != "adjustment")
        self.baseline_button.setEnabled(self.import_button.isEnabled())

    @QtCore.Slot(object)
    def _on_event(self, event):
        kind = event["type"]
        if kind == "connected":
            self.connected = True
            self.simulation = event["simulation"]
            self.camera_id = event["camera"]
            self.can_slew = event["can_slew"]
            self.connected_binning = event["settings"]["binning"]
            self.current_connection = event["connection"]
            for key, value in event["settings"].items():
                if key in self.fields:
                    self.fields[key].setValue(value)
            self._save_profile()
            self.geometry_check.setChecked(self.simulation)
            self.device_label.setText(f"{event['telescope']}\n{event['camera']}\n{event['width']} × {event['height']} binned pixels · bin {self.connected_binning}")
            self._auto_range = True
            self.selected_position = None
            self._update_guidance("Connected — nothing has moved yet. Click '1 · Take picture' to see the stars.")
            self._log("Connected. No movement at connection. Driver capabilities validated by adapters.")
        elif kind == "disconnected":
            self.connected = False
            self.current_connection = None
            self.can_slew = False
            self.pose = None
            self.selected_position = None
            self.device_label.setText("No devices connected")
            self.banner.setText("SIMULATION DEFAULT · No hardware connected · Imported measurements are offline only")
            self._update_guidance("Disconnected — connect devices to begin. No automatic exposures or mount motion.")
        elif kind == "pose":
            self.pose = event
            self.pose_label.setText(f"RA {event['ra']:.4f} h · Dec {event['dec']:+.2f}° · HA {event['ha']:+.3f} h · "
                                    f"angle {event['angle']:+.2f}° · alt {event['alt']:.1f}° · pier {event['pier']} · tracking {'ON' if event['tracking'] else 'OFF'}")
        elif kind == "selected":
            self.selected_position = event.get("position")
            self.trail.clear()
            self.trail_item.setData([], [])
            if self.selected_position is not None:
                self.star_item.setData([self.selected_position[0]], [self.selected_position[1]])
            else:
                self.star_item.setData([], [])
            self._update_guidance("Star selected — click '3 · Measure backlash' (stop Live view first if it is running)."
                                  if self.selected_position is not None else
                                  "No suitable star — click '1 · Take picture', then click an isolated star or retry 'Pick star automatically'.")
            self._log(f"Selected absolute binned coordinates: {event['position']}")
        elif kind == "awaiting_star":
            self.pending = False
            self._log(f"No automatic star at {event['angle']:+g}°. Click a star in the picture to retry this angle, press 'Pick star automatically' to retry, or 'Skip this position'. The pose is rechecked before measuring.")
        elif kind == "phase":
            phase = event["phase"]
            if phase == "drift":
                self._reset_measurement_display()
            self._pending_phase = True
            if phase == "drift":
                name, description = "Drift calibration", "Tracking is off for a few seconds; the star's natural drift shows the camera's orientation and scale."
            elif phase.startswith("sign"):
                name, description = "Drive direction check", "A short test move checks which way the drive moves the star."
            elif phase.startswith("latency"):
                name, description = "Drive response delay", "Same-direction speed changes measure how long the drive takes to react (this is not backlash)."
            elif phase.startswith("reverse"):
                name, description = "Reverse drive", "Reversing RA direction: while the gears take up the slack the star lags. That lag is the backlash."
            elif phase.startswith("forward"):
                name, description = "Forward drive", "Driving forward again to measure the slack in the other direction."
            else:
                name, description = phase.replace("_", " "), "Recording star motion."
            step = self._phase_step(phase)
            prefix = f"Step {step[0]} of {step[1]} · " if step else ""
            self._phase_description = f"{prefix}{name} — {description}"
            self.state_label.setText(f"{name.upper()}  ·  {event['duration']:.1f} s")
            self._update_guidance(self._phase_description)
            self._log(f"Phase {event['phase']}: {event['duration']:.1f} s · raw rate {event['raw_rate']:+g}×")
        elif kind == "map_point":
            self._map_progress = f"Angle {event['index'] + 1} of {event['total']} ({event['angle']:+g}°)"
            self._update_guidance()
        elif kind == "calibration":
            self.calibration = event
            self._update_trajectory()
            self._log(f"Drift {event['v0']:.3f} px/s · RA unit vector {event['u']} · driver sign {event['sign']:+g}")
        elif kind == "sample":
            # Cheap append only; the simulator can emit hundreds per second.
            if self.last_result is None:
                self.samples.append(tuple(finite(event.get(key)) for key in ("t", "x", "y")))
                if self._pending_phase and math.isfinite(finite(event.get("t"))):
                    self._pending_markers.append(event["t"])
                    self._pending_phase = False
                self._request_trajectory()
            return
        self._gate()

    @QtCore.Slot(object)
    def _on_frame(self, frame):
        try:
            self._show_frame(frame)
        finally:
            self.worker._frame_sent_at = 0.

    def _show_frame(self, frame):
        image = frame.image
        self.image_item.setImage(image, autoLevels=False)
        # Robust display stretch is display-only, never applied to raw centroids.
        sample = image[::max(1, image.shape[0] // 150), ::max(1, image.shape[1] // 150)]
        lo, hi = np.percentile(sample, [5, 99.8])
        self.image_item.setLevels((float(lo), float(max(lo + 50, hi))))
        self.image_item.setRect(image_rect(frame.origin, image.shape, frame.step))
        if self._auto_range:
            self._fit_image()
            self._auto_range = False
        x, y = finite(frame.x), finite(frame.y)
        if math.isfinite(x) and math.isfinite(y):
            if self.last_result is None:
                self.trail.append((x, y))
            xy = np.asarray(self.trail)
            if len(xy):
                self.trail_item.setData(xy[:, 0], xy[:, 1], connect="finite")
            self.star_item.setData([x], [y])
            self._zoom(frame)
            calibrated = self._trajectory_calibration()
            if calibrated and self.last_result is None:
                u, _ = calibrated
                start = np.array([x, y])
                end = start + 35 * u
                self.direction_item.setData([start[0], end[0]], [start[1], end[1]])
        elif self.last_result is None:
            self.trail.append((float("nan"), float("nan")))
        q = frame.quality or {}
        self.quality_label.setText(f"Absolute binned sensor coordinates · SNR {finite(q.get('snr')):.1f} · FWHM {finite(q.get('fwhm')):.2f} px · "
                                   f"saturated {q.get('saturated')}")

    def _zoom(self, frame):
        ix = int(round((frame.x - frame.origin[0]) / frame.step))
        iy = int(round((frame.y - frame.origin[1]) / frame.step))
        radius = max(4, int(32 / frame.step))
        x0, y0 = max(0, ix - radius), max(0, iy - radius)
        crop = frame.image[y0:iy + radius + 1, x0:ix + radius + 1]
        if crop.size:
            self.zoom_item.setImage(crop, autoLevels=True)
            self.zoom_item.setRect(image_rect((frame.origin[0] + x0 * frame.step, frame.origin[1] + y0 * frame.step), crop.shape, frame.step))
            self.zoom_plot.autoRange()

    def _bracket(self, anchor, result):
        length = backlash_pixels(result.get("backlash"), result.get("drift_px_s"))
        u = np.asarray(result.get("u", [float("nan"), float("nan")]))
        if not math.isfinite(length) or u.shape != (2,) or not np.isfinite(u).all():
            self.bracket_item.setData([], [])
            return
        normal = np.array([-u[1], u[0]])
        a = np.asarray(anchor) + normal * 24
        b = a + u * length
        points = np.array([a - normal * 4, a + normal * 4, a, b, b - normal * 4, b + normal * 4])
        self.bracket_item.setData(points[:, 0], points[:, 1])

    def _image_clicked(self, event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton or not self.image_plot.sceneBoundingRect().contains(event.scenePos()):
            return
        if not (self.connected and (self.state in IDLE_STATES or self.state in {"preview", "awaiting_star"}) and not self.pending):
            return
        point = self.image_plot.getViewBox().mapSceneToView(event.scenePos())
        self._launch("select", position=(point.x(), point.y()))

    def _fit_image(self):
        self.image_plot.autoRange()

    @QtCore.Slot(object)
    def _on_result(self, result):
        self.last_result = result
        self._assessment_result = self._assessment_source(result)
        self._reassess()
        summary = (f"B = {finite(result.get('backlash')):.3f} ± {finite(result.get('uncertainty_arcsec')):.3f} "
                   f"RA-axis arcsec (diagnostic allowance, NOT a calibrated confidence interval) · "
                   f"Quality: {result.get('quality', 'unknown')}")
        notes = [str(note) for note in result.get("notes", [])]
        reasons = "; ".join(notes) or str(result.get("reason") or "No quality reasons supplied.")
        self.result_summary.setText(summary + "\n" + reasons)
        self._log("Result: " + summary)
        raw = result.get("_raw") or {}
        self.samples.clear()
        # Partial exports may have None/missing centroid components. Keep the
        # row as a gap rather than dropping it and drawing across missing data.
        source = raw.get("frames")
        if source is not None:
            for row in source:
                values = list(row) if isinstance(row, (tuple, list, np.ndarray)) else []
                self.samples.append(tuple(finite(values[i]) if i < len(values) else float("nan") for i in range(3)))
        self.calibration = dict(u=result.get("u"), v0=result.get("drift_px_s"))
        for item in self.fit_items:
            self.time_plot.removeItem(item)
        self.fit_items.clear()
        self._has_commands = self._has_fits = False
        self._pending_phase = False
        self._pending_markers.clear()
        self._trajectory_dirty = False
        self.bracket_item.setData([], [])
        origin = self._update_trajectory()
        calibrated = self._trajectory_calibration()
        if origin is not None:
            for command in raw.get("cmds", []):
                self._command_marker(finite(command[0]) - origin[0])
        if origin is not None and calibrated:
            u, v0 = calibrated
            offset = origin[1:] @ u * core.S_ARCSEC / v0
            for transition in raw.get("trans", []):
                tc = finite(transition.get("t"))
                if not math.isfinite(tc):
                    continue
                if not raw.get("cmds"):
                    self._command_marker(tc - origin[0])
                for key, color, start, end in (("fit_before", "#77d6a5", -1., 0.), ("fit_after", "#ffac59", 0., 1.)):
                    fit = transition.get(key)
                    if fit is None or len(fit) < 2:
                        continue
                    slope, intercept = map(finite, fit[:2])
                    if not math.isfinite(slope) or not math.isfinite(intercept):
                        continue
                    x = np.array([start, end])
                    item = self.time_plot.plot(x + tc - origin[0], slope * x + intercept - offset, pen=pg.mkPen(color, width=2))
                    self.fit_items.append(item)
                    self._has_fits = True
            frames = np.asarray(self.samples)
            valid = np.isfinite(frames).all(axis=1)
            anchor = frames[valid][-1, 1:]
            end = anchor + 35 * u
            self.direction_item.setData([anchor[0], end[0]], [anchor[1], end[1]])
            if self._has_fits and result.get("status", "completed") == "completed" and result.get("quality") in {"valid", "uncertain"}:
                self._bracket(anchor, result)
        if origin is not None:
            # Show the complete recorded path, not only UI-throttled frames.
            # Refit after completion so a wide preview cannot leave the
            # inferred bracket and measured motion too small to inspect.
            recorded = np.asarray(self.samples, dtype=float)
            positions = recorded[:, 1:].copy()
            positions[~np.isfinite(recorded).all(axis=1)] = np.nan
            self.trail_item.setData(positions[:, 0], positions[:, 1], connect="finite")
            self._fit_image()
        self._update_trajectory()
        self._update_guidance("Result ready — read the coloured box at the top; 'Result details' explains it and 'Trajectory graph' shows the fits. "
                              "'Measure at several angles' repeats the test across the axis.")
        for note in notes:
            self._log(note)

    @QtCore.Slot(object)
    def _on_rows(self, rows):
        self.rows = [dict(row) for row in rows]
        selection_blocker = QtCore.QSignalBlocker(self.table)
        self.table.setRowCount(len(rows))
        mismatches = 0
        for index, row in enumerate(rows):
            pose = row.get("pose", {})
            reason = row.get("reason", "")
            if row.get("role") == "candidate_confirmation":
                reason = f"Candidate confirmation: {row.get('confirmation_status', 'measured')} · " + reason
            if self.baseline and math.isfinite(finite(row.get("backlash"))):
                matched = [b for b in self.baseline if abs(finite(b.get("angle")) - finite(row.get("angle"))) < .05
                           and abs(finite(b.get("dec")) - finite(row.get("dec"))) < .05]
                if matched:
                    reason += f"  Δ baseline {finite(row.get('backlash')) - finite(matched[0].get('backlash')):+.3f} arcsec (load/settings not verified)"
                else:
                    reason += "  BASELINE POSE MISMATCH — no comparison"
                    mismatches += 1
            values = [row.get("angle"), row.get("ha", pose.get("ha")), row.get("dec"), row.get("alt", pose.get("alt")),
                      f"{row.get('status', 'completed')} / {row.get('quality', '—')}", row.get("backlash"), row.get("uncertainty_arcsec"),
                      row.get("fwd_rev"), row.get("rev_fwd"), row.get("n_good", "—"), reason]
            for column, value in enumerate(values):
                text = f"{value:.3f}" if isinstance(value, float) and math.isfinite(value) else "—" if value is None else str(value)
                self.table.setItem(index, column, QtWidgets.QTableWidgetItem(text))
        del selection_blocker
        map_assessment = assess_map([self._assessment_source(row) for row in self.rows])
        self.map_assessment_label.setText("Map summary (separate from selected point)\n" +
            "\n".join([map_assessment["title"], map_assessment["meaning"],
                       *map_assessment["details"], map_assessment["action"]]))
        self.map_plot.clear()
        # clear() does not consistently clear LegendItem across pg releases.
        if self.map_plot.plotItem.legend:
            self.map_plot.plotItem.legend.clear()
        for source, color, name in ((rows, "#4bdce8", "Current (cyan)"), (self.baseline, "#a4a3ec", "Saved baseline (purple)")):
            usable = [r for r in source if math.isfinite(finite(r.get("angle"))) and math.isfinite(finite(r.get("backlash")))]
            if usable:
                x = np.array([float(r["angle"]) for r in usable])
                y = np.array([float(r["backlash"]) for r in usable])
                self.map_plot.plot(x, y, pen=None, symbol="o", symbolBrush=color, name=name + " · median (circle)")
                for key, symbol in (("fwd_rev", "t"), ("rev_fwd", "s")):
                    directional = np.array([finite(r.get(key)) for r in usable])
                    present = np.isfinite(directional)
                    # Scatter bounds use nanmin/nanmax, even for an entirely
                    # missing series. Never send absent/nonfinite points.
                    if present.any():
                        direction = "forward → reverse (triangle)" if key == "fwd_rev" else "reverse → forward (square)"
                        self.map_plot.plot(x[present], directional[present], pen=None, symbol=symbol, symbolSize=6,
                                           symbolBrush=color, name=name + " · " + direction)
                err = np.array([finite(r.get("uncertainty_arcsec"), 0.) for r in usable])
                self.map_plot.addItem(pg.ErrorBarItem(x=x, y=y, height=2 * err, beam=1.2, pen=color))
        candidate = core.candidate(rows)
        if candidate:
            self._log(f"Qualified measured candidate {candidate['angle']:+g}° · {candidate['candidate_note']} · select table row to inspect/return")
        if mismatches:
            self._log(f"Warning: {mismatches} row(s) do not match saved baseline angle/Dec; comparison withheld.")
        self._update_map_caption()
        self._gate()

    @staticmethod
    def read_saved(path):
        """Read legacy summary/JSON without inventing image or pose evidence."""
        path = Path(path)
        if path.suffix.lower() == ".csv":
            with path.open(newline="", encoding="utf-8-sig") as stream:
                data = list(csv.DictReader(stream))
            rows = []
            for entry in data:
                row = dict(entry)
                for key in ("angle", "dec", "backlash", "spread", "fwd_rev", "rev_fwd", "uncertainty_arcsec"):
                    row[key] = finite(row.get(key))
                row.setdefault("quality", "uncertain")
                rows.append(row)
        else:
            data = core.load_result(path)
            rows = data.get("rows", [data])
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("Expected result object or manifest rows")
        result = []
        for source in rows:
            row = dict(source)
            metadata = row.get("metadata", {})
            for key in ("angle", "dec", "pose"):
                if key not in row and key in metadata:
                    row[key] = metadata[key]
            # Imports NEVER acquire this connection's motion authorization.
            row.update(imported=True, geometry_verified=False, connection=None)
            result.append(row)
        return result

    def _import(self, baseline):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select saved baseline" if baseline else "Import offline results", "", "Saved results (*.json *.csv)")
        if not path:
            return
        try:
            rows = self.read_saved(path)
            if baseline:
                self.baseline = rows
                self._on_rows(self.rows)
            else:
                self._reset_measurement_display()
                self.image_item.clear()
                self.zoom_item.clear()
                self._on_rows(rows)
                if rows:
                    self.table.selectRow(0)
                    self._assess_selection()
                    self.tabs.setCurrentWidget(self.assessment_panel)
            self._log(f"Loaded {len(rows)} saved row(s). Offline data: no camera images reconstructed; imported results cannot authorize candidate return.")
        except Exception as exc:
            self._warn(f"Cannot load saved results: {exc}")

    def _log(self, message):
        self.log_view.appendPlainText(time.strftime("%H:%M:%S  ") + message)

    def _warn(self, message):
        self._log("Warning: " + message)
        QtWidgets.QMessageBox.warning(self, "RA Backlash", message)

    def closeEvent(self, event):
        if not self.worker.isRunning():
            event.accept()
            return
        event.ignore()
        if not self.closing:
            self.closing = True
            self._log("Closing: requesting cancellation and owner-thread disconnect. Waiting for driver; never terminating QThread.")
            self.worker.shutdown()
            self._gate()

    def _worker_finished(self):
        if self.closing:
            self.close()
        else:
            self.pending = False
            self.connected = False
            self._on_state("fault")


def main(argv=None):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("RA Backlash")
    app.setOrganizationName("RA Backlash")
    pg.setConfigOptions(antialias=True, background="#111e29", foreground="#adc3d6", imageAxisOrder="row-major")
    window = MainWindow()
    window.show()
    return app.exec()