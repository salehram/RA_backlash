"""Simulation and fail-closed ASCOM adapters; no Qt and no COM initialization.

Construct, use AND disconnect on a single COM-initialized worker thread. A Stop
cannot interrupt a hung driver/COM call: native mount controls remain necessary.
Connection performs no motion/tracking commands. Camera coordinates are binned
full-frame pixels; multiply by binning only when converting to native pixels.
"""
from __future__ import annotations

import math
import threading
import time

import numpy as np

import ra_backlash as legacy
from .core import Cancelled, S_DEG


def _choose(kind):
    import win32com.client
    chooser = win32com.client.Dispatch("ASCOM.Utilities.Chooser")
    chooser.DeviceType = kind
    progid = chooser.Choose("")
    if not progid:
        raise Cancelled(f"No {kind} selected")
    return progid


def _dispatch(progid):
    import win32com.client
    return win32com.client.Dispatch(progid)


class _Owned:
    def _own(self):
        self._owner = threading.get_ident()

    def _assert_thread(self):
        if self._owner != threading.get_ident():
            raise RuntimeError("Device used outside its owning thread")


class AscomMount(_Owned):
    def __init__(self, progid, cfg, cancel, driver=None):
        self._own()
        self.cfg, self.cancel = cfg, cancel
        self.t = _dispatch(progid) if driver is None else driver
        self._axis_active = self._slew_active = False
        try:
            self.t.Connected = True
            if not self.t.CanMoveAxis(0):
                raise RuntimeError("Mount cannot MoveAxis on RA")
            if not self.t.CanSetTracking:
                raise RuntimeError("Mount cannot set tracking")
            collection = self.t.AxisRates(0)
            self.rate_intervals = [(float(collection.Item(i).Minimum), float(collection.Item(i).Maximum))
                                   for i in range(1, int(collection.Count) + 1)]
            if not self.rate_intervals or any(not (0 <= lo <= hi and math.isfinite(hi)) for lo, hi in self.rate_intervals):
                raise RuntimeError("Missing/invalid RA rate intervals")
            self.can_slew = bool(self.t.CanSlewAsync)
        except Exception:
            self.t.Connected = False
            raise

    def _safe(self):
        self._assert_thread()
        if not self.t.Connected:
            raise RuntimeError("Mount disconnected")
        if self.t.AtPark:
            raise RuntimeError("Mount is parked; unpark explicitly in native controls")
        # IsPulseGuiding is mandatory only on drivers which support guiding.
        if bool(self.t.CanPulseGuide) and bool(self.t.IsPulseGuiding):
            raise RuntimeError("Stop guiding before measurement/slew")
        if bool(self.t.Slewing) and not (self._axis_active or self._slew_active):
            raise RuntimeError("Mount is already slewing/moving under another controller")

    def validate_rate(self, rate):
        self._safe()
        if not math.isfinite(rate):
            raise ValueError("Rate must be finite")
        magnitude = abs(rate)
        if magnitude and not any(lo <= magnitude <= hi for lo, hi in self.rate_intervals):
            raise ValueError(f"RA rate {rate:.6g} deg/s outside allowed intervals {self.rate_intervals}")

    def lst(self):
        self._assert_thread()
        return float(self.t.SiderealTime)

    def latitude(self):
        self._assert_thread()
        return float(self.t.SiteLatitude)

    def ra_dec(self):
        self._assert_thread()
        return float(self.t.RightAscension), float(self.t.Declination)

    def pier_side(self):
        self._assert_thread()
        try:
            return {0: "East", 1: "West"}.get(int(self.t.SideOfPier), "Unknown")
        except Exception:
            return "Unknown"

    def get_tracking(self):
        self._assert_thread()
        return bool(self.t.Tracking)

    def tracking(self, on):
        self._assert_thread()
        if on:
            self._safe()
        self.t.Tracking = bool(on)

    def move_axis(self, rate):
        self._assert_thread()
        if rate:
            self.cancel.check()
            self.validate_rate(rate)
        elif not math.isfinite(rate):
            raise ValueError("Rate must be finite")
        self.t.MoveAxis(0, float(rate))
        self._axis_active = bool(rate)

    def stop(self):
        self._assert_thread()
        errors = []
        try:
            self.t.MoveAxis(0, 0.)
        except Exception as exc:
            errors.append(f"MoveAxis(0): {exc}")
        try:
            if self._slew_active or bool(self.t.Slewing):
                self.t.AbortSlew()
                deadline = time.perf_counter() + self.cfg.stop_timeout
                while bool(self.t.Slewing):
                    if time.perf_counter() >= deadline:
                        raise TimeoutError("Driver still reports motion after AbortSlew")
                    threading.Event().wait(.02)
        except Exception as exc:
            errors.append(f"AbortSlew: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
        self._axis_active = self._slew_active = False

    def slew(self, ra, dec, cancel):
        self._safe()
        cancel.check()
        if not self.can_slew:
            raise RuntimeError("Async slew unsupported; synchronous slews are deliberately disabled")
        if self._axis_active or self._slew_active:
            raise RuntimeError("Stop current motion before slewing")
        if not math.isfinite(ra) or not math.isfinite(dec) or not 0 <= ra < 24 or not -90 <= dec <= 90:
            raise ValueError("Invalid target coordinates")
        altitude = legacy.altitude(self.latitude(), dec, self.lst() - ra)
        if altitude < self.cfg.min_alt:
            raise ValueError("Slew destination below minimum altitude")
        # Endpoint checks are NOT proof of a collision-free path or GEM pose.
        clock = legacy.RealClock()
        end = clock.now() + self.cfg.slew_timeout
        try:
            self.tracking(True)
            self._slew_active = True
            self.t.SlewToCoordinatesAsync(ra, dec)
            while bool(self.t.Slewing):
                cancel.check()
                if clock.now() >= end:
                    raise TimeoutError("Mount slew timed out")
                cancel.wait(.05)
            cancel.check()
        except Exception:
            try:
                self.stop()
            finally:
                self.tracking(False)
            raise
        finally:
            self._slew_active = False

    def disconnect(self):
        self._assert_thread()
        # Stop is explicit: merely connecting/disconnecting must not send motion.
        # Refuse to disconnect while *our* motion remains active.
        if self._axis_active or self._slew_active:
            self.stop()
        self.t.Connected = False


class AscomCamera(_Owned):
    def __init__(self, progid, cfg, cancel, driver=None):
        self._own()
        self.cfg, self.cancel = cfg, cancel
        self.c = _dispatch(progid) if driver is None else driver
        self._exposing = self._fault = False
        try:
            self.c.Connected = True
            if int(self.c.CameraState) != 0:
                raise RuntimeError("Camera is not idle; stop other acquisition before connecting")
            self.can_abort = bool(self.c.CanAbortExposure)
            self.can_stop = bool(self.c.CanStopExposure)
            if not (self.can_abort or self.can_stop):
                raise RuntimeError("Camera has no cancellable exposure (abort/stop unsupported)")
            b = cfg.binning
            if isinstance(b, bool) or int(b) != b or not 1 <= b <= min(int(self.c.MaxBinX), int(self.c.MaxBinY)):
                raise ValueError("Unsupported symmetric camera binning")
            self.c.NumX, self.c.NumY = 1, 1
            self.c.StartX, self.c.StartY = 0, 0
            self.c.BinX, self.c.BinY = int(b), int(b)
            self.binning = int(b)
            self.W = int(self.c.CameraXSize) // self.binning
            self.H = int(self.c.CameraYSize) // self.binning
            self.exposure_min, self.exposure_max = float(self.c.ExposureMin), float(self.c.ExposureMax)
            try:
                self.max_adu = float(self.c.MaxADU)
            except Exception:
                self.max_adu = float("inf")
            self.set_roi(0, 0, self.W, self.H)
        except Exception:
            self.c.Connected = False
            raise

    def set_roi(self, x0, y0, w, h):
        self._assert_thread()
        if self._exposing or self._fault:
            raise RuntimeError("Cannot change ROI while exposing/faulted")
        values = (x0, y0, w, h)
        if any(isinstance(v, bool) or not math.isfinite(v) or int(v) != v for v in values):
            raise ValueError("ROI coordinates must be integers")
        x0, y0, w, h = map(int, values)
        if min(x0, y0) < 0 or min(w, h) <= 0 or x0 + w > self.W or y0 + h > self.H:
            raise ValueError("ROI outside binned sensor")
        # Shrink before moving origin; prevents transient out-of-bounds ROIs.
        self.c.NumX, self.c.NumY = 1, 1
        self.c.StartX, self.c.StartY = x0, y0
        self.c.NumX, self.c.NumY = w, h
        self.roi = (x0, y0, w, h)

    def _abort(self):
        try:
            if self.can_abort:
                self.c.AbortExposure()
            else:
                self.c.StopExposure()
        except Exception:
            self._fault = True
            raise
        finally:
            self._exposing = False

    def expose(self, clock, exposure):
        self._assert_thread()
        self.cancel.check()
        if self._exposing or self._fault:
            raise RuntimeError("Camera already exposing or faulted; reconnect required")
        if int(self.c.CameraState) != 0:
            raise RuntimeError("Camera busy under another acquisition/controller")
        if not math.isfinite(exposure) or not self.exposure_min <= exposure <= self.exposure_max:
            raise ValueError("Exposure outside driver limits")
        start = clock.now()
        self._exposing = True
        try:
            self.c.StartExposure(float(exposure), True)
            while not bool(self.c.ImageReady):
                self.cancel.check()
                if clock.now() > start + exposure + self.cfg.exposure_timeout:
                    raise TimeoutError("Camera exposure/readout timed out")
                self.cancel.wait(.01, clock)
            self.cancel.check()
            # ASCOM ImageArray is [x][y], numpy image is [y][x].
            image = np.asarray(self.c.ImageArray, dtype=np.float32).T.copy()
            if image.shape != (self.roi[3], self.roi[2]) or not np.isfinite(image).all():
                raise RuntimeError("Invalid camera image shape/pixels")
            self.cancel.check()
            return start + exposure / 2, image, self.roi[:2]
        except Exception as exc:
            try:
                self._abort()
            except Exception as abort_error:
                raise RuntimeError(f"Exposure failed: {exc}; abort also failed: {abort_error}") from exc
            raise
        finally:
            self._exposing = False

    def disconnect(self):
        self._assert_thread()
        try:
            if self._exposing:
                self._abort()
        finally:
            self.c.Connected = False


class SimWorld(legacy.SimWorld):
    def __init__(self, cfg, seeing):
        if cfg.sim_sign not in (-1, 1) or not 0 <= cfg.sim_correlation < 1:
            raise ValueError("Invalid simulator sign/correlation")
        if not math.isfinite(seeing) or seeing < 0 or cfg.sim_scale <= 0:
            raise ValueError("Invalid seeing/scale")
        if cfg.sim_backlash is not None and (not math.isfinite(cfg.sim_backlash) or cfg.sim_backlash < 0):
            raise ValueError("Invalid simulated backlash")
        def backlash(angle):
            if cfg.sim_backlash is not None:
                return cfg.sim_backlash
            return 12 + 33 * (1 - math.cos(math.radians(angle - 10))) / (1 - math.cos(math.radians(100)))
        super().__init__(backlash, latency=cfg.sim_latency, move_sign=cfg.sim_sign,
                         seeing=seeing, scale=cfg.sim_scale, rot_deg=cfg.sim_rotation,
                         seed=cfg.sim_seed)
        self.correlation = cfg.sim_correlation
        self.jitter = np.zeros(2)

    def star_px(self):
        p = legacy.S_ARCSEC * self.t - self.th - self.p0
        distance = p * math.cos(math.radians(self.dec)) / self.scale
        a = self.correlation
        self.jitter = a * self.jitter + math.sqrt(1 - a * a) * self.rng.normal(0, self.seeing / self.scale, 2)
        return np.array([512., 512.]) + distance * self.u + self.jitter


class SimMount(legacy.SimMount):
    can_slew = True

    def __init__(self, world, cfg, cancel):
        super().__init__(world)
        self.cfg, self.cancel = cfg, cancel
        self.connected = True

    def get_tracking(self):
        # Include the most recently requested state (commands have latency).
        return next((v for _, k, v in reversed(self.w.pending) if k == "track"), self.w.track)

    def validate_rate(self, rate):
        if not self.connected or not math.isfinite(rate) or abs(rate) > 4:
            raise ValueError("Invalid simulator axis rate/state")

    def move_axis(self, rate):
        self.validate_rate(rate)
        if rate:
            self.cancel.check()
        super().move_axis(rate)

    def stop(self):
        # Emergency software stop takes priority over all queued commands.
        self.w.pending = [entry for entry in self.w.pending if entry[1] != "move"]
        self.w.move = 0.

    def slew(self, ra, dec, cancel):
        cancel.check()
        if not (math.isfinite(ra) and 0 <= ra < 24 and math.isfinite(dec) and -90 <= dec <= 90):
            raise ValueError("Invalid target coordinates")
        if legacy.altitude(self.latitude(), dec, self.lst() - ra) < self.cfg.min_alt:
            raise ValueError("Slew destination below minimum altitude")
        self.w.slew(legacy.angle_from_ha(self.lst() - ra), dec)

    def disconnect(self):
        self.stop()
        self.connected = False


class SimCamera:
    max_adu = 65535.

    def __init__(self, world, cfg, cancel):
        self.w, self.cfg, self.cancel = world, cfg, cancel
        self.binning = cfg.binning
        self.W = self.H = 1024 // self.binning
        self.roi = (0, 0, self.W, self.H)
        self.connected = True

    def set_roi(self, x0, y0, w, h):
        if any(int(v) != v for v in (x0, y0, w, h)) or min(x0, y0) < 0 or min(w, h) < 1 or x0 + w > self.W or y0 + h > self.H:
            raise ValueError("ROI outside binned sensor")
        self.roi = tuple(map(int, (x0, y0, w, h)))

    def expose(self, clock, exposure):
        self.cancel.check()
        if not self.connected or not math.isfinite(exposure) or exposure <= 0:
            raise ValueError("Invalid simulated exposure/state")
        self.cancel.wait(exposure / 2, clock)
        sx, sy = self.w.star_px() / self.binning
        t = clock.now()
        self.cancel.wait(exposure / 2 + .04, clock)
        x0, y0, w, h = self.roi
        yy, xx = np.mgrid[y0:y0 + h, x0:x0 + w]
        image = 100 + 3000 * np.exp(-((xx - sx) ** 2 + (yy - sy) ** 2) / 8)
        if self.cfg.sim_multistar:
            image += 4500 * np.exp(-((xx - sx - 65 / self.binning) ** 2 + (yy - sy + 38 / self.binning) ** 2) / 8)
        image += self.w.rng.normal(0, 12, image.shape)
        self.cancel.check()
        return t + self.w.ts_bias, image.astype(np.float32), (x0, y0)

    def disconnect(self):
        self.connected = False


def open_devices(simulation: bool, cfg, cancel, telescope="", camera="", seeing=.8):
    """No motion on connection. Caller initializes/uninitializes COM on worker.

    Optional explicit ProgIDs avoid the ASCOM chooser. No SystemExit is used.
    Partially connected devices are disconnected if the second connection fails.
    """
    cfg.validate()
    cancel.check()
    if simulation:
        world = SimWorld(cfg, seeing)
        return SimMount(world, cfg, cancel), SimCamera(world, cfg, cancel), legacy.SimClock(world)
    telescope = telescope or _choose("Telescope")
    cancel.check()
    camera = camera or _choose("Camera")
    cancel.check()
    mount = AscomMount(telescope, cfg, cancel)
    cam = None
    try:
        cam = AscomCamera(camera, cfg, cancel)
        cancel.check()
        return mount, cam, legacy.RealClock()
    except Exception:
        try:
            if cam is not None:
                cam.disconnect()
        finally:
            mount.disconnect()
        raise