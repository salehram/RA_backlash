"""Mock driver contract tests; no COM/ASCOM installation or hardware needed."""
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from ra_backlash_app.core import Cancellation, Cancelled, Config, S_DEG
from ra_backlash_app.devices import AscomCamera, AscomMount, open_devices


class MountDriver:
    Connected = False
    CanSetTracking = CanSlewAsync = CanPulseGuide = True
    AtPark = IsPulseGuiding = Slewing = False
    Tracking = True
    SiderealTime = 0.
    SiteLatitude = 25.
    RightAscension = Declination = 0.
    SideOfPier = 0
    def __init__(self):
        self.calls = []
        self.intervals = [(0., .025), (.1, 4.)]
    def CanMoveAxis(self, axis):
        return True
    def AxisRates(self, axis):
        return SimpleNamespace(Count=len(self.intervals), Item=lambda i: SimpleNamespace(
            Minimum=self.intervals[i - 1][0], Maximum=self.intervals[i - 1][1]))
    def MoveAxis(self, axis, rate):
        self.calls.append(("move", rate))
    def SlewToCoordinatesAsync(self, ra, dec):
        self.calls.append(("slew", ra, dec))
        self.Slewing = True
    def AbortSlew(self):
        self.calls.append(("abort",))
        self.Slewing = False


def mount_with(driver=None, cfg=None, cancel=None):
    driver = driver or MountDriver()
    cancel = cancel or Cancellation()
    return AscomMount("mock", cfg or Config(), cancel, driver), driver, cancel


def test_connection_and_disconnect_do_not_command_motion_or_tracking():
    mount, driver, token = mount_with()
    assert driver.calls == []
    assert driver.Tracking is True
    assert driver.Connected
    mount.disconnect()
    assert not driver.Connected
    assert driver.calls == []


@pytest.mark.parametrize("sign", [-1, 1])
def test_absolute_rate_intervals_not_just_maximum(sign):
    mount, driver, token = mount_with()
    mount.validate_rate(sign * .02)
    mount.validate_rate(sign * .2)
    with pytest.raises(ValueError, match="intervals"):
        mount.move_axis(sign * .05)
    assert driver.calls == []
    with pytest.raises(ValueError):
        mount.validate_rate(float("nan"))


@pytest.mark.parametrize("state", ["AtPark", "IsPulseGuiding", "Slewing"])
def test_park_guiding_and_other_slew_rejected(state):
    mount, driver, token = mount_with()
    setattr(driver, state, True)
    with pytest.raises(RuntimeError):
        mount.move_axis(S_DEG)
    assert not driver.calls


def test_unsupported_async_slew_is_never_replaced_with_blocking_slew():
    driver = MountDriver()
    driver.CanSlewAsync = False
    mount, driver, token = mount_with(driver)
    assert not mount.can_slew
    with pytest.raises(RuntimeError, match="Async"):
        mount.slew(0, 0, token)
    assert not driver.calls


def test_async_slew_cancellation_aborts_and_does_not_restore_tracking():
    mount, driver, token = mount_with()
    original = driver.SlewToCoordinatesAsync
    def slew(ra, dec):
        original(ra, dec)
        token.cancel()
    driver.SlewToCoordinatesAsync = slew
    with pytest.raises(Cancelled):
        mount.slew(0, 0, token)
    assert ("abort",) in driver.calls
    assert driver.Tracking is False
    assert not driver.Slewing


def test_slew_timeout_aborts():
    mount, driver, token = mount_with(cfg=Config(slew_timeout=.001))
    with pytest.raises(TimeoutError):
        mount.slew(0, 0, token)
    assert ("abort",) in driver.calls
    assert not driver.Tracking


def test_failed_abort_is_not_reported_as_stopped():
    mount, driver, token = mount_with(cfg=Config(stop_timeout=.001))
    driver.Slewing = True
    driver.AbortSlew = lambda: None
    with pytest.raises(RuntimeError, match="still reports motion"):
        mount.stop()


def test_slew_target_altitude_and_coordinates_validated():
    mount, driver, token = mount_with()
    for ra, dec in ((12, 0), (24, 0), (0, 91), (float("nan"), 0)):
        with pytest.raises(ValueError):
            mount.slew(ra, dec, token)
    assert driver.calls == []


def test_thread_ownership():
    mount, driver, token = mount_with()
    errors = []
    def wrong_thread():
        try:
            mount.move_axis(S_DEG)
        except RuntimeError as error:
            errors.append(str(error))
    thread = threading.Thread(target=wrong_thread)
    thread.start()
    thread.join()
    assert "owning thread" in errors[0]
    assert not driver.calls


class Clock:
    def __init__(self):
        self.t = 0
    def now(self):
        return self.t
    def sleep(self, duration):
        self.t += duration


class CameraDriver:
    Connected = False
    CameraState = 0
    CanAbortExposure = True
    CanStopExposure = True
    MaxBinX = MaxBinY = 4
    CameraXSize = 100
    CameraYSize = 80
    BinX = BinY = 1
    ExposureMin = .001
    ExposureMax = 20
    MaxADU = 65535
    StartX = StartY = 0
    NumX = 100
    NumY = 80
    def __init__(self):
        self.ready = True
        self.calls = []
    @property
    def ImageReady(self):
        return self.ready
    @property
    def ImageArray(self):
        return np.ones((self.NumX, self.NumY)) * 100
    def StartExposure(self, exposure, light):
        self.calls.append(("expose", exposure))
    def AbortExposure(self):
        self.calls.append(("abort",))
    def StopExposure(self):
        self.calls.append(("stop",))


def camera_with(driver=None, cfg=None, cancel=None):
    driver = driver or CameraDriver()
    token = cancel or Cancellation()
    return AscomCamera("mock", cfg or Config(), token, driver), driver, token


def test_camera_roi_bin_orientation_and_exposure_bounds():
    cam, driver, token = camera_with(cfg=Config(binning=2))
    assert (cam.W, cam.H) == (50, 40)
    assert not driver.calls
    cam.set_roi(20, 10, 10, 12)
    t, image, origin = cam.expose(Clock(), .1)
    assert image.shape == (12, 10)
    assert origin == (20, 10)
    assert t == .05
    for roi in ((-1, 0, 10, 10), (40, 0, 11, 10), (0, 0, .5, 10)):
        with pytest.raises(ValueError):
            cam.set_roi(*roi)
    with pytest.raises(ValueError):
        cam.expose(Clock(), 30)


def test_camera_timeout_abort_and_cancellation_stop_fallback():
    cam, driver, token = camera_with(cfg=Config(exposure_timeout=.02))
    driver.ready = False
    with pytest.raises(TimeoutError):
        cam.expose(Clock(), .01)
    assert ("abort",) in driver.calls
    driver = CameraDriver()
    driver.CanAbortExposure = False
    cam, driver, token = camera_with(driver)
    def start(exposure, light):
        token.cancel()
    driver.StartExposure = start
    with pytest.raises(Cancelled):
        cam.expose(Clock(), .1)
    assert ("stop",) in driver.calls


def test_camera_cancel_does_not_read_partial_image():
    cam, driver, token = camera_with()
    driver.StartExposure = lambda exposure, light: token.cancel()
    driver.ready = False
    with pytest.raises(Cancelled):
        cam.expose(Clock(), .1)
    assert ("abort",) in driver.calls


def test_abort_failure_faults_camera_until_reconnect():
    cam, driver, token = camera_with()
    driver.StartExposure = lambda exposure, light: token.cancel()
    def fail():
        raise RuntimeError("broken abort")
    driver.AbortExposure = fail
    with pytest.raises(RuntimeError, match="abort also failed"):
        cam.expose(Clock(), .1)
    token.reset()
    with pytest.raises(RuntimeError, match="faulted"):
        cam.expose(Clock(), .1)


def test_camera_no_abort_or_bad_bin_disconnects_failed_connection():
    driver = CameraDriver()
    driver.CanAbortExposure = driver.CanStopExposure = False
    with pytest.raises(RuntimeError, match="cancellable"):
        camera_with(driver)
    assert not driver.Connected
    driver = CameraDriver()
    with pytest.raises(ValueError, match="binning"):
        camera_with(driver, Config(binning=5))
    assert not driver.Connected


def test_camera_busy_connection_and_exposure_rejected():
    driver = CameraDriver()
    driver.CameraState = 2
    with pytest.raises(RuntimeError, match="not idle"):
        camera_with(driver)
    assert not driver.Connected
    driver = CameraDriver()
    cam, driver, token = camera_with(driver)
    driver.CameraState = 3
    with pytest.raises(RuntimeError, match="busy"):
        cam.expose(Clock(), .1)
    assert not driver.calls


def test_camera_rejects_malformed_image_before_using_centroids():
    class BadImage(CameraDriver):
        @property
        def ImageArray(self):
            return [[float("nan")]]
    cam, driver, token = camera_with(BadImage())
    with pytest.raises(RuntimeError, match="image shape/pixels"):
        cam.expose(Clock(), .1)
    assert ("abort",) in driver.calls


def test_open_devices_failure_closes_mount(monkeypatch):
    import ra_backlash_app.devices as devices
    driver = MountDriver()
    monkeypatch.setattr(devices, "_dispatch", lambda progid: driver)
    def fail(*args, **kwargs):
        raise RuntimeError("camera missing")
    monkeypatch.setattr(devices, "AscomCamera", fail)
    with pytest.raises(RuntimeError, match="camera missing"):
        open_devices(False, Config(), Cancellation(), telescope="mount", camera="camera")
    assert not driver.Connected
    assert not driver.calls


def test_sim_connection_no_motion_and_cancellation():
    token = Cancellation()
    mount, camera, clock = open_devices(True, Config(), token)
    assert mount.w.pending == []
    assert mount.w.move == 0
    assert mount.get_tracking()
    token.cancel()
    with pytest.raises(Cancelled):
        camera.expose(clock, .1)
    with pytest.raises(Cancelled):
        mount.slew(0, 0, token)
    mount.stop()  # Stop bypasses cancellation.