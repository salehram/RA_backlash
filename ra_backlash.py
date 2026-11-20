#!/usr/bin/env python3
"""
ra_backlash.py  v0.1  -  Measure RA backlash on a GEM with a guide camera.

Built for: EQ6-R Pro controlled by GSS (ASCOM), any ASCOM guide camera.
Windows: pip install numpy matplotlib pywin32

HOW IT WORKS
------------
rho = RA axis speed in sidereal units, + = tracking direction.
Tracking is turned OFF and the axis is driven with ASCOM MoveAxis.
Star speed on the sensor (along RA) = |v0| * (1 - rho), where v0 = star drift
when the axis is stopped. So:
    rho = +1  -> star still        rho = +2 -> star moves back at 1x
    rho = -2  -> star moves at 3x  axis stopped (inside backlash) -> 1x

When the motor reverses, the axis stands still while the worm crosses the gap.
A line is fitted before and after each command. The jump between the two lines
at the command time is:
    jump = S * (D * rho_new + L * (rho_new - rho_old))
    D = dead time (backlash zone), L = command latency + timestamp bias,
    S = 15.041 arcsec/s.
Non-reversing steps (+1 -> +2 -> +1) have D = 0, so they measure L.
Backlash (RA-axis arcsec) = S * |rho_new| * D.

The pixel scale and camera angle are NOT needed: both come from the drift
calibration. Results are in RA-AXIS arcseconds (same at any Dec).

WHEEL ANGLE
-----------
Angle = RA axis angle from home (counterweight down), for normal
counterweight-down poses:  angle = sign(HA) * (90 - 15*|HA|) deg.
0 = home, +/-90 = counterweight horizontal (target on the meridian).

COMMANDS
--------
  python ra_backlash.py measure            # measure at the current pointing
  python ra_backlash.py map                # slew to several angles, measure each
  python ra_backlash.py simulate           # dry run with a fake mount + camera
Use --help on each command for options.

IMPORTANT: balance RA as close to perfect as you can. A heavy side keeps the
wheel pressed on the worm and hides backlash.
"""

import argparse
import csv
import json
import math
import os
import sys
import time

import numpy as np

S_ARCSEC = 15.041067                 # sidereal rate, arcsec per second
S_DEG = S_ARCSEC / 3600.0            # sidereal rate, deg per second


# ----------------------------------------------------------------------------
# Clock
# ----------------------------------------------------------------------------
class RealClock:
    def now(self):
        return time.perf_counter()

    def sleep(self, dt):
        if dt > 0:
            time.sleep(dt)


# ----------------------------------------------------------------------------
# ASCOM devices (Windows)
# ----------------------------------------------------------------------------
def ascom_choose(device_type):
    import win32com.client
    ch = win32com.client.Dispatch("ASCOM.Utilities.Chooser")
    ch.DeviceType = device_type
    progid = ch.Choose("")
    if not progid:
        sys.exit(f"No {device_type} selected.")
    return progid


class AscomMount:
    def __init__(self, progid):
        import win32com.client
        self.t = win32com.client.Dispatch(progid)
        self.t.Connected = True
        if not self.t.CanMoveAxis(0):
            raise RuntimeError("Driver says it cannot MoveAxis on the RA axis.")
        print(f"Mount: {self.t.Description}")

    def lst(self):
        return float(self.t.SiderealTime)

    def latitude(self):
        return float(self.t.SiteLatitude)

    def ra_dec(self):
        return float(self.t.RightAscension), float(self.t.Declination)

    def slew(self, ra_h, dec_deg):
        self.t.Tracking = True
        self.t.SlewToCoordinates(ra_h, dec_deg)
        while self.t.Slewing:
            time.sleep(0.2)

    def tracking(self, on):
        self.t.Tracking = bool(on)

    def move_axis(self, rate_deg_s):
        self.t.MoveAxis(0, rate_deg_s)

    def pier_side(self):
        try:
            return {0: "East", 1: "West"}.get(int(self.t.SideOfPier), "Unknown")
        except Exception:
            return "Unknown"

    def max_rate(self):
        try:
            rates = self.t.AxisRates(0)
            return max(float(rates.Item(i).Maximum) for i in range(1, rates.Count + 1))
        except Exception:
            return None


class AscomCamera:
    def __init__(self, progid, binning):
        import win32com.client
        self.c = win32com.client.Dispatch(progid)
        self.c.Connected = True
        self.c.BinX = binning
        self.c.BinY = binning
        self.W = int(self.c.CameraXSize) // binning
        self.H = int(self.c.CameraYSize) // binning
        self.roi = (0, 0, self.W, self.H)
        print(f"Camera: {self.c.Description}  {self.W}x{self.H} (bin {binning})")

    def set_roi(self, x0, y0, w, h):
        self.c.StartX, self.c.StartY = int(x0), int(y0)
        self.c.NumX, self.c.NumY = int(w), int(h)
        self.roi = (int(x0), int(y0), int(w), int(h))

    def expose(self, clock, exposure):
        t0 = clock.now()
        self.c.StartExposure(exposure, True)
        while not self.c.ImageReady:
            time.sleep(0.003)
        img = np.array(self.c.ImageArray, dtype=np.float32).T   # ASCOM is [x][y]
        # Mid-exposure time. Any constant bias here is absorbed by the
        # latency calibration, so it does not affect the backlash value.
        return t0 + exposure / 2.0, img, self.roi[:2]


# ----------------------------------------------------------------------------
# Star finding
# ----------------------------------------------------------------------------
def median3(img):
    p = np.pad(img, 1, mode="edge")
    h, w = img.shape
    stack = [p[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3)]
    return np.median(np.stack(stack), axis=0)


def find_brightest(img):
    sm = median3(img)                         # kills single hot pixels
    bg = np.median(sm)
    noise = 1.4826 * np.median(np.abs(sm - bg)) + 1e-6
    iy, ix = np.unravel_index(np.argmax(sm), sm.shape)
    return int(ix), int(iy), float((sm[iy, ix] - bg) / noise)


def centroid(img, ix, iy, r=8):
    h, w = img.shape
    for _ in range(2):
        y0, y1 = max(0, iy - r), min(h, iy + r + 1)
        x0, x1 = max(0, ix - r), min(w, ix + r + 1)
        sub = img[y0:y1, x0:x1].astype(np.float64)
        edge = np.concatenate([sub[0], sub[-1], sub[:, 0], sub[:, -1]])
        sub = np.clip(sub - np.median(edge), 0, None)
        tot = sub.sum()
        if tot <= 0:
            return float(ix), float(iy)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        cx, cy = (sub * xx).sum() / tot, (sub * yy).sum() / tot
        ix, iy = int(round(cx)), int(round(cy))
    return cx, cy


class StarTracker:
    """Keeps a small ROI around the star and returns absolute sensor coords."""

    def __init__(self, cam, clock, roi_size, exposure, min_snr=6.0):
        self.cam, self.clock = cam, clock
        self.roi_size, self.exposure, self.min_snr = roi_size, exposure, min_snr

    def _roi_around(self, x, y):
        w = min(self.roi_size, self.cam.W)
        h = min(self.roi_size, self.cam.H)
        x0 = int(np.clip(round(x - w / 2), 0, self.cam.W - w))
        y0 = int(np.clip(round(y - h / 2), 0, self.cam.H - h))
        self.cam.set_roi(x0, y0, w, h)

    def acquire(self, exposure=1.0):
        self.cam.set_roi(0, 0, self.cam.W, self.cam.H)
        _, img, (x0, y0) = self.cam.expose(self.clock, exposure)
        ix, iy, snr = find_brightest(img)
        if snr < 3 * self.min_snr:
            return False, snr
        cx, cy = centroid(img, ix, iy)
        self._roi_around(cx + x0, cy + y0)
        return True, snr

    def grab(self):
        t, img, (x0, y0) = self.cam.expose(self.clock, self.exposure)
        ix, iy, snr = find_brightest(img)
        if snr < self.min_snr:
            return t, np.nan, np.nan
        cx, cy = centroid(img, ix, iy)
        X, Y = cx + x0, cy + y0
        _, _, w, h = self.cam.roi
        if abs(cx - w / 2) > w / 4 or abs(cy - h / 2) > h / 4:
            self._roi_around(X, Y)          # follow the star
        return t, X, Y


# ----------------------------------------------------------------------------
# Measurement
# ----------------------------------------------------------------------------
def fit_line(t, p):
    """Robust line fit. Returns (slope, intercept_at_0, rms, n) or None."""
    if len(t) < 5:
        return None
    A = np.vstack([t, np.ones_like(t)]).T
    coef = np.linalg.lstsq(A, p, rcond=None)[0]
    res = p - A @ coef
    sig = 1.4826 * np.median(np.abs(res)) + 1e-9
    keep = np.abs(res) < 3.5 * sig
    if 5 <= keep.sum() < len(t):
        coef = np.linalg.lstsq(A[keep], p[keep], rcond=None)[0]
        res = p[keep] - A[keep] @ coef
    return float(coef[0]), float(coef[1]), float(np.std(res)), int(len(res))


def run_steps(mount, tracker, clock, steps, frames, cmds):
    """steps: list of (raw_rate_in_sidereal_units or None for 'stop', seconds)."""
    for raw, dur in steps:
        t_c = clock.now()
        if raw is None:
            mount.move_axis(0.0)
            mount.tracking(False)
            raw = 0.0
        else:
            mount.move_axis(raw * S_DEG)
        cmds.append((t_c, raw))
        t_end = t_c + dur
        while clock.now() < t_end:
            frames.append(tracker.grab())


def measure_here(mount, tracker, clock, cfg, log=print):
    r, tb, cycles = cfg.rate, cfg.phase, cfg.cycles
    if r <= 1.0:
        raise ValueError("--rate must be > 1")
    tf = tb * (1 + r) / (r - 1)               # keeps the star near the start point
    frames, cmds = [], []
    sign = None
    try:
        # 1) drift with axis stopped -> v0 (direction and px/s)
        # 2) MoveAxis(+1x) -> learn which sign is the tracking direction
        run_steps(mount, tracker, clock, [(None, cfg.cal), (+1.0, cfg.cal)], frames, cmds)
        F = np.array(frames, dtype=float)
        ok = np.isfinite(F[:, 1])
        t, x, y = F[ok, 0], F[ok, 1], F[ok, 2]
        (t0, _), (t1, _) = cmds[0], cmds[1]
        w = (t > t0 + 0.4 * (t1 - t0)) & (t < t1)
        fx, fy = fit_line(t[w] - t1, x[w]), fit_line(t[w] - t1, y[w])
        if fx is None or fy is None:
            raise RuntimeError("Lost the star during drift calibration.")
        v = np.array([fx[0], fy[0]])
        v0 = float(np.hypot(*v))
        if v0 * cfg.cal < 3.0:
            raise RuntimeError(
                f"Star barely moved with tracking off ({v0:.2f} px/s). "
                "Is the driver still tracking during MoveAxis(0)?")
        u = v / v0
        w = (t > t1 + 0.5 * cfg.cal)
        fp = fit_line(t[w] - t1, x[w] * u[0] + y[w] * u[1])
        ratio = fp[0] / v0
        if ratio < 0.5:
            sign = +1.0
        elif ratio > 1.5:
            sign = -1.0
        else:
            raise RuntimeError(f"MoveAxis(+1x) gave star speed {ratio:.2f}x drift "
                               "(expected ~0 or ~2). Is MoveAxis being ignored?")
        log(f"  drift {v0:.2f} px/s, +MoveAxis = {'tracking' if sign > 0 else 'reverse'} direction")

        # 3) latency steps, then reversal cycles
        # Latency steps use the same rate jump as a reversal (2r), so any
        # latency error cancels instead of leaking into the backlash value.
        steps = [(sign * 1, 6.0), (sign * (1 + 2 * r), 3.0), (sign * 1, 4.0)]
        steps += [(-sign * r, tb), (sign * r, tf)] * cycles
        run_steps(mount, tracker, clock, steps, frames, cmds)
        t_stop = clock.now()
    finally:
        try:
            mount.move_axis(0.0)
        finally:
            mount.tracking(True)

    return analyze(frames, cmds, t_stop, sign, u, v0, cfg)


def analyze(frames, cmds, t_stop, sign, u, v0, cfg):
    F = np.array(frames, dtype=float)
    ok = np.isfinite(F[:, 1])
    lost = 1 - ok.mean()
    t = F[ok, 0]
    p = (F[ok, 1] * u[0] + F[ok, 2] * u[1]) * S_ARCSEC / v0     # RA-axis arcsec
    rhos = [raw * sign for _, raw in cmds]
    tc = [c[0] for c in cmds] + [t_stop]

    trans = []
    for k in range(1, len(cmds)):
        old, new = rhos[k - 1], rhos[k]
        if old == 0 or abs(new - old) < 1e-9:
            continue
        t_prev, t_c, t_next = tc[k - 1], tc[k], tc[k + 1]
        bw = (t >= t_prev + 0.5 * (t_c - t_prev)) & (t < t_c)
        aw = (t >= t_c + 0.5 * (t_next - t_c)) & (t <= t_next)
        fb, fa = fit_line(t[bw] - t_c, p[bw]), fit_line(t[aw] - t_c, p[aw])
        if fb is None or fa is None:
            continue
        exp_b, exp_a = S_ARCSEC * (1 - old), S_ARCSEC * (1 - new)
        good = abs(fb[0] - exp_b) < 0.25 * S_ARCSEC and abs(fa[0] - exp_a) < 0.25 * S_ARCSEC
        trans.append(dict(k=k, t=t_c, old=old, new=new, jump=fa[1] - fb[1],
                          fit_before=fb, fit_after=fa, slope_ok=bool(good),
                          window=t_next - t_c))

    lat = [d["jump"] / (S_ARCSEC * (d["new"] - d["old"]))
           for d in trans if d["old"] * d["new"] > 0 and d["slope_ok"]]
    L = float(np.mean(lat)) if lat else 0.0

    revs = []
    for d in trans:
        if d["old"] * d["new"] >= 0:
            continue
        B = math.copysign(1, d["new"]) * (d["jump"] - S_ARCSEC * L * (d["new"] - d["old"]))
        D = B / (S_ARCSEC * abs(d["new"]))
        note = ""
        if not d["slope_ok"]:
            note = "slope off"
        elif D > 0.45 * d["window"]:
            note = "phase too short"
        revs.append(dict(dir="fwd->rev" if d["new"] < 0 else "rev->fwd",
                         backlash=B, dead_s=D, ok=(note == ""), note=note, t=d["t"]))
        d["backlash"] = B

    good = [r["backlash"] for r in revs if r["ok"]]
    noise = float(np.median([d["fit_after"][2] for d in trans])) if trans else float("nan")
    res = dict(
        backlash=float(np.median(good)) if good else float("nan"),
        spread=float(np.std(good)) if len(good) > 1 else float("nan"),
        fwd_rev=float(np.median([r["backlash"] for r in revs if r["ok"] and r["dir"] == "fwd->rev"] or [np.nan])),
        rev_fwd=float(np.median([r["backlash"] for r in revs if r["ok"] and r["dir"] == "rev->fwd"] or [np.nan])),
        n_good=len(good), n_total=len(revs), latency_s=L, latency_samples=lat,
        drift_px_s=v0, sign=sign, noise_arcsec=noise, lost_frac=float(lost),
        reversals=revs,
    )
    res["_raw"] = dict(t=t, p=p, cmds=[(c[0], rho) for c, rho in zip(cmds, rhos)],
                       trans=trans, t_stop=t_stop, frames=F)
    return res


# ----------------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------------
def angle_from_ha(ha_h):
    ha_h = (ha_h + 12) % 24 - 12
    return math.copysign(1, ha_h) * (90 - 15 * abs(ha_h))


def ha_from_angle(a):
    s = 1 if a >= 0 else -1
    return s * (6 - abs(a) / 15.0)


def altitude(lat, dec, ha_h):
    la, de, h = map(math.radians, (lat, dec, ha_h * 15))
    return math.degrees(math.asin(math.sin(la) * math.sin(de) + math.cos(la) * math.cos(de) * math.cos(h)))


def pick_dec(lat, ha_h, min_alt, dec_pref, dec_max=75):
    d = dec_pref
    while d <= dec_max:
        if altitude(lat, d, ha_h) >= min_alt:
            return d
        d += 5
    return None


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
def summary_line(res):
    if not np.isfinite(res["backlash"]):
        return f"no valid reversals ({res['n_total']} tried)"
    return (f"backlash {res['backlash']:.1f}\"  (+/-{res['spread']:.1f}, "
            f"fwd->rev {res['fwd_rev']:.1f}, rev->fwd {res['rev_fwd']:.1f}, "
            f"{res['n_good']}/{res['n_total']} ok)  latency {res['latency_s'] * 1000:.0f} ms  "
            f"noise {res['noise_arcsec']:.2f}\"")


def save_measurement(res, outdir, tag, meta):
    os.makedirs(outdir, exist_ok=True)
    raw = res["_raw"]
    with open(os.path.join(outdir, f"{tag}_frames.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["t", "x", "y"])
        wr.writerows(raw["frames"].tolist())
    with open(os.path.join(outdir, f"{tag}_commands.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["t", "rho_sidereal"])
        wr.writerows(raw["cmds"])
    out = {k: v for k, v in res.items() if k != "_raw"}
    out.update(meta)
    with open(os.path.join(outdir, f"{tag}_result.json"), "w") as f:
        json.dump(out, f, indent=2)
    plot_measurement(res, os.path.join(outdir, f"{tag}_plot.png"), meta)


def plot_measurement(res, path, meta):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    raw = res["_raw"]
    t0 = raw["cmds"][0][0]
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(raw["t"] - t0, raw["p"], ".", ms=3, color="0.35", label="star (RA-axis arcsec)")
    top = np.nanmax(raw["p"])
    for tc, rho in raw["cmds"]:
        ax.axvline(tc - t0, color="0.85", lw=0.8)
        ax.text(tc - t0, top, f"{rho + 0.0:+.0f}x", fontsize=7, va="bottom", color="0.4")
    for d in raw["trans"]:
        for fit, side in ((d["fit_before"], -1), (d["fit_after"], 1)):
            tt = np.array([min(0, side * 3), max(0, side * 3)])   # extend each fit 3 s to the command
            ax.plot(d["t"] - t0 + tt, fit[1] + fit[0] * tt,
                    color="#c0392b" if "backlash" in d else "#2471a3", lw=1)
        if "backlash" in d:
            ax.annotate(f"{d['backlash']:.1f}\"", (d["t"] - t0, d["fit_after"][1]),
                        fontsize=8, color="#c0392b")
    title = f"angle {meta.get('angle', float('nan')):+.0f}°  |  " + summary_line(res)
    ax.set_title(title, fontsize=9)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("RA-axis arcsec")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def plot_map(rows, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    rows = [r for r in rows if np.isfinite(r["backlash"])]
    if not rows:
        return
    a = np.array([r["angle"] for r in rows])
    b = np.array([r["backlash"] for r in rows])
    e = np.nan_to_num(np.array([r["spread"] for r in rows]))
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.errorbar(a, b, yerr=e, fmt="o-", capsize=3, color="#2471a3")
    i = int(np.argmin(b))
    ax.plot(a[i], b[i], "o", ms=12, mfc="none", mec="#c0392b", mew=2)
    ax.annotate(f"tight spot {a[i]:+.0f}°", (a[i], b[i]), xytext=(8, -14),
                textcoords="offset points", color="#c0392b")
    ax.axvline(0, color="0.8", lw=0.8)
    ax.set_xlabel("RA wheel angle from home (°)   0 = counterweight down")
    ax.set_ylabel("backlash (RA-axis arcsec)")
    ax.set_ylim(bottom=0)
    ax.set_title("RA backlash map")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


# ----------------------------------------------------------------------------
# Simulator (to test the math without a mount)
# ----------------------------------------------------------------------------
class SimWorld:
    def __init__(self, backlash_fn, latency=0.08, move_sign=-1, seeing=0.8,
                 scale=0.6, rot_deg=33, ts_bias=0.03, seed=1):
        self.rng = np.random.default_rng(seed)
        self.B_fn, self.L, self.msign = backlash_fn, latency, move_sign
        self.seeing, self.scale, self.ts_bias = seeing, scale, ts_bias
        self.u = np.array([math.cos(math.radians(rot_deg)), math.sin(math.radians(rot_deg))])
        self.t, self.m, self.th = 0.0, 0.0, 0.0
        self.move, self.track = 0.0, True
        self.pending = []
        self.slew(0.0, 0.0)

    def slew(self, angle, dec):
        self.angle, self.dec = angle, dec
        self.gap = self.B_fn(angle)
        self.m, self.th = 0.0, -self.gap / 2       # gear loaded in tracking direction
        self.p0 = S_ARCSEC * self.t - self.th
        self.move, self.track, self.pending = 0.0, True, []

    def command(self, kind, val):
        self.pending.append((self.t + self.L, kind, val))

    def advance(self, dt):
        h = 0.005
        n = max(1, int(round(dt / h)))
        h = dt / n
        for _ in range(n):
            while self.pending and self.pending[0][0] <= self.t:
                _, kind, val = self.pending.pop(0)
                setattr(self, kind, val)
            if self.move != 0:
                rate = self.msign * self.move * 3600.0
            else:
                rate = S_ARCSEC if self.track else 0.0
            self.m += rate * h
            half = self.gap / 2
            if self.m - self.th > half:
                self.th = self.m - half
            elif self.m - self.th < -half:
                self.th = self.m + half
            self.t += h

    def star_px(self):
        p = S_ARCSEC * self.t - self.th - self.p0               # axis arcsec
        d = p * math.cos(math.radians(self.dec)) / self.scale
        jit = self.rng.normal(0, self.seeing / self.scale, 2)
        return 512 + d * self.u[0] + jit[0], 512 + d * self.u[1] + jit[1]


class SimClock:
    def __init__(self, w):
        self.w = w

    def now(self):
        return self.w.t

    def sleep(self, dt):
        self.w.advance(dt)


class SimMount:
    def __init__(self, w):
        self.w = w

    def lst(self):
        return (self.w.t / 3600.0 * 1.0027379) % 24

    def latitude(self):
        return 24.7

    def ra_dec(self):
        return (self.lst() - ha_from_angle(self.w.angle)) % 24, self.w.dec

    def slew(self, ra, dec):
        self.w.slew(angle_from_ha(self.lst() - ra), dec)

    def tracking(self, on):
        self.w.command("track", bool(on))

    def move_axis(self, rate):
        self.w.command("move", float(rate))

    def pier_side(self):
        return "sim"

    def max_rate(self):
        return 4.0


class SimCamera:
    W = H = 1024

    def __init__(self, w):
        self.w = w
        self.roi = (0, 0, self.W, self.H)

    def set_roi(self, x0, y0, wd, ht):
        self.roi = (x0, y0, wd, ht)

    def expose(self, clock, exposure):
        self.w.advance(exposure / 2)
        sx, sy = self.w.star_px()
        t_mid = self.w.t
        self.w.advance(exposure / 2 + 0.04)
        x0, y0, wd, ht = self.roi
        yy, xx = np.mgrid[y0:y0 + ht, x0:x0 + wd]
        img = 100 + 3000 * np.exp(-((xx - sx) ** 2 + (yy - sy) ** 2) / (2 * 2.0 ** 2))
        img = img + self.w.rng.normal(0, 12, img.shape)
        return t_mid + self.w.ts_bias, img.astype(np.float32), (x0, y0)


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
def open_devices(args):
    if args.cmd == "simulate":
        tight, low, high = args.sim_tight, args.sim_min, args.sim_max

        def bfn(a):
            return low + (high - low) * (1 - math.cos(math.radians(a - tight))) / (1 - math.cos(math.radians(100)))
        w = SimWorld(bfn, latency=args.sim_latency, seeing=args.sim_seeing)
        args._bfn = bfn
        return SimMount(w), SimCamera(w), SimClock(w)
    tel = args.telescope or ascom_choose("Telescope")
    cam = args.camera or ascom_choose("Camera")
    return AscomMount(tel), AscomCamera(cam, args.bin), RealClock()


def check_rate(mount, args):
    mx = mount.max_rate()
    need = (1 + 2 * args.rate) * S_DEG
    if mx is not None and mx < need:
        sys.exit(f"Driver max RA MoveAxis rate {mx:.5f} deg/s < needed {need:.5f}. Lower --rate.")


def cmd_measure(args):
    mount, cam, clock = open_devices(args)
    check_rate(mount, args)
    tracker = StarTracker(cam, clock, args.roi, args.exposure)
    ra, dec = mount.ra_dec()
    ha = (mount.lst() - ra + 12) % 24 - 12
    angle = angle_from_ha(ha)
    ok, snr = tracker.acquire(args.acq_exposure)
    if not ok:
        sys.exit(f"No usable star in the frame (SNR {snr:.1f}).")
    print(f"Measuring at HA {ha:+.2f}h, Dec {dec:+.1f}, wheel angle {angle:+.0f}° (pier {mount.pier_side()})")
    res = measure_here(mount, tracker, clock, args)
    print("  " + summary_line(res))
    for r in res["reversals"]:
        print(f"    {r['dir']}: {r['backlash']:6.1f}\"  dead {r['dead_s']:.2f}s  {r['note']}")
    tag = time.strftime("measure_%Y%m%d_%H%M%S")
    save_measurement(res, args.out, tag, dict(angle=angle, ha=ha, dec=dec))
    print(f"Saved to {os.path.abspath(args.out)}")


def cmd_map(args):
    mount, cam, clock = open_devices(args)
    check_rate(mount, args)
    tracker = StarTracker(cam, clock, args.roi, args.exposure)
    lat = mount.latitude()
    angles = [float(a) for a in args.angles.split(",")]
    rows = []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(args.out, f"map_{stamp}")
    for a in angles:
        if abs(a) > 82:
            print(f"angle {a:+.0f}: skipped (too close to the meridian, pier side is ambiguous)")
            continue
        ha = ha_from_angle(a)
        dec = pick_dec(lat, ha, args.min_alt, args.dec)
        if dec is None:
            print(f"angle {a:+.0f}: skipped (no Dec gives altitude >= {args.min_alt}°)")
            continue
        found = False
        for dd in (0, 0.4, -0.4, 0.8, -0.8, 1.2):
            ra = (mount.lst() - ha) % 24
            mount.slew(ra, dec + dd)
            clock.sleep(args.settle)
            found, snr = tracker.acquire(args.acq_exposure)
            if found:
                break
        if not found:
            print(f"angle {a:+.0f}: skipped (no star found)")
            continue
        alt = altitude(lat, dec + dd, ha)
        print(f"angle {a:+.0f}°: HA {ha:+.2f}h Dec {dec + dd:+.1f} alt {alt:.0f}° pier {mount.pier_side()}")
        try:
            res = measure_here(mount, tracker, clock, args)
        except RuntimeError as e:
            print(f"  failed: {e}")
            continue
        print("  " + summary_line(res))
        if args.cmd == "simulate":
            print(f"  (simulator true value: {args._bfn(a):.1f}\")")
        meta = dict(angle=a, ha=ha, dec=dec + dd, alt=alt, pier=mount.pier_side())
        save_measurement(res, outdir, f"angle_{a:+04.0f}", meta)
        rows.append(dict(angle=a, backlash=res["backlash"], spread=res["spread"],
                         fwd_rev=res["fwd_rev"], rev_fwd=res["rev_fwd"],
                         n_good=res["n_good"], latency_ms=res["latency_s"] * 1000,
                         noise=res["noise_arcsec"], ha=ha, dec=dec + dd, alt=alt))

    if not rows:
        sys.exit("No angles measured.")
    with open(os.path.join(outdir, "map_summary.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)
    plot_map(rows, os.path.join(outdir, "map.png"))
    print("\n angle   backlash  spread")
    for r in rows:
        print(f" {r['angle']:+5.0f}°  {r['backlash']:7.1f}\"  {r['spread']:5.1f}")
    good = [r for r in rows if np.isfinite(r["backlash"])]
    if good:
        t = min(good, key=lambda r: r["backlash"])
        print(f"\nTightest spot: {t['angle']:+.0f}° ({t['backlash']:.1f}\"). Adjust the mesh there.")
    print(f"Saved to {os.path.abspath(outdir)}")


def main():
    ap = argparse.ArgumentParser(description="Measure RA backlash with a guide camera.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--telescope", help="ASCOM ProgID (default: show chooser)")
        p.add_argument("--camera", help="ASCOM ProgID (default: show chooser)")
        p.add_argument("--bin", type=int, default=1)
        p.add_argument("--exposure", type=float, default=0.1, help="tracking frame exposure, s")
        p.add_argument("--acq-exposure", type=float, default=1.0, help="full-frame star search exposure, s")
        p.add_argument("--roi", type=int, default=160, help="ROI size, px")
        p.add_argument("--rate", type=float, default=2.0, help="drive rate during reversals, x sidereal (>1)")
        p.add_argument("--phase", type=float, default=5.0, help="reverse phase length, s (forward is longer)")
        p.add_argument("--cycles", type=int, default=3, help="reversal cycles per measurement")
        p.add_argument("--cal", type=float, default=4.0, help="drift / sign calibration length, s")
        p.add_argument("--out", default="ra_backlash_runs")

    def mapping(p):
        p.add_argument("--angles", default="-75,-50,-25,0,25,50,75",
                       help="wheel angles from home, deg (0 = counterweight down)")
        p.add_argument("--min-alt", type=float, default=25.0)
        p.add_argument("--dec", type=float, default=0.0, help="preferred Dec; raised if too low")
        p.add_argument("--settle", type=float, default=3.0, help="wait after slew, s")

    common(sub.add_parser("measure", help="measure at the current pointing"))
    mapping_p = sub.add_parser("map", help="measure at several wheel angles")
    common(mapping_p)
    mapping(mapping_p)
    sim = sub.add_parser("simulate", help="run 'map' on a simulated mount")
    common(sim)
    mapping(sim)
    sim.add_argument("--sim-tight", type=float, default=10.0, help="angle of the tight spot")
    sim.add_argument("--sim-min", type=float, default=12.0, help="backlash at tight spot, arcsec")
    sim.add_argument("--sim-max", type=float, default=45.0, help="backlash at loose side, arcsec")
    sim.add_argument("--sim-latency", type=float, default=0.08)
    sim.add_argument("--sim-seeing", type=float, default=0.8, help="per-frame jitter, arcsec")

    args = ap.parse_args()
    if args.cmd == "measure":
        cmd_measure(args)
    else:
        cmd_map(args)


if __name__ == "__main__":
    main()
