"""Cancellable measurement engine using the legacy drift/line-jump estimator.

Coordinates are full-frame *binned* pixel coordinates (not ROI-relative).
Uncertainty is a conservative diagnostic budget, NOT a calibrated confidence
interval. Geometry is a planning assumption, never authorization to move.
"""
from __future__ import annotations

import csv
import json
import math
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import ra_backlash as legacy

S_ARCSEC = legacy.S_ARCSEC
S_DEG = legacy.S_DEG


@dataclass
class Config:
    exposure: float = .1
    acq_exposure: float = 1.
    roi: int = 160
    rate: float = 2.
    phase: float = 5.
    cycles: int = 3
    cal: float = 4.
    settle: float = 3.
    min_alt: float = 25.
    dec: float = 0.
    out: str = "ra_backlash_runs"
    binning: int = 1
    min_snr: float = 6.
    search_radius: float = 24.
    exposure_timeout: float = 30.
    slew_timeout: float = 180.
    stop_timeout: float = 5.
    partial_on_error: bool = True
    sim_sign: int = -1
    sim_backlash: float | None = None
    sim_latency: float = .08
    sim_seed: int = 1
    sim_correlation: float = 0.
    sim_multistar: bool = False
    sim_scale: float = .6
    sim_rotation: float = 33.

    def validate(self):
        for key in ("exposure", "acq_exposure", "phase", "cal", "min_snr",
                    "search_radius", "exposure_timeout", "slew_timeout", "stop_timeout"):
            value = getattr(self, key)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be finite and positive")
        if not math.isfinite(self.rate) or self.rate <= 1:
            raise ValueError("rate must exceed 1 sidereal")
        for key, minimum in (("cycles", 1), ("binning", 1), ("roi", 24)):
            value = getattr(self, key)
            if isinstance(value, bool) or int(value) != value or value < minimum:
                raise ValueError(f"{key} must be an integer >= {minimum}")
        if not math.isfinite(self.settle) or self.settle < 0:
            raise ValueError("settle must be finite and nonnegative")
        if not -90 <= self.dec <= 90 or not 0 <= self.min_alt <= 90:
            raise ValueError("invalid declination/altitude")
        # Five points per fit is the absolute numerical minimum; require more
        # time than this even before camera readout overhead is known.
        if min(self.cal * .5, self.phase * .5, 1.5) < 6 * self.exposure:
            raise ValueError("exposure too long for calibration/phase fit windows")
        return self


class Cancelled(RuntimeError):
    """Workflow stopped; partial_result/partial_path are attached by measure."""


class Cancellation:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    def reset(self):
        """Only reset after the previous owning workflow has exited."""
        self._event.clear()

    def check(self):
        if self._event.is_set():
            raise Cancelled("Operation cancelled")

    def wait(self, seconds, clock=None):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("wait duration must be finite and nonnegative")
        self.check()
        if clock is None or isinstance(clock, legacy.RealClock):
            if self._event.wait(seconds):
                self.check()
        else:
            end = clock.now() + seconds
            while clock.now() < end:
                self.check()
                clock.sleep(min(.05, end - clock.now()))
        self.check()


def generate_angles(count=3, spacing=25, first=25, side="both"):
    if isinstance(count, bool) or int(count) != count or count < 1:
        raise ValueError("count must be a positive integer")
    if not all(math.isfinite(v) and v > 0 for v in (spacing, first)):
        raise ValueError("spacing and first must be finite and positive")
    groups = {"both": (-1, 1), "negative": (-1,), "positive": (1,),
              "-": (-1,), "+": (1,)}
    if side not in groups:
        raise ValueError("side must be both, negative or positive")
    values = [first + i * spacing for i in range(int(count))]
    if values[-1] > 82:
        raise ValueError("angles must not exceed the conservative 82 degree limit")
    return sorted(s * v for s in groups[side] for v in values)


def plan_points(angles, latitude, min_alt=25, dec=0):
    if not all(math.isfinite(v) for v in (latitude, min_alt, dec)):
        raise ValueError("nonfinite planning parameters")
    if not -90 <= latitude <= 90 or not -90 <= dec <= 90 or not 0 <= min_alt <= 90:
        raise ValueError("planning parameters out of range")
    rows, seen = [], set()
    for angle in angles:
        a = float(angle)
        reason = ""
        target_dec = dec
        ha = legacy.ha_from_angle(a) if math.isfinite(a) else float("nan")
        alt = legacy.altitude(latitude, dec, ha) if math.isfinite(ha) else float("nan")
        if not math.isfinite(a) or abs(a) > 82:
            reason = "outside supported +/-82 degree geometry"
        elif a == 0:
            reason = "zero angle requires an explicit HA branch"
        elif a in seen:
            reason = "duplicate angle"
        elif latitude <= 0:
            reason = "southern/equatorial mechanical geometry not verified"
        elif alt < min_alt:
            # Preferred Dec is not silently treated as an achieved target.
            # Search both ways, show the selected Dec explicitly in the table,
            # and never claim that endpoint feasibility validates a slew path.
            options = [dec + direction * offset for offset in range(5, 181, 5)
                       for direction in (1, -1) if -75 <= dec + direction * offset <= 75]
            feasible = [d for d in options if legacy.altitude(latitude, d, ha) >= min_alt]
            if feasible:
                target_dec = feasible[0]
                alt = legacy.altitude(latitude, target_dec, ha)
            else:
                reason = "no supported declination reaches minimum altitude"
        seen.add(a)
        rows.append(dict(angle=a, ha=ha, dec=target_dec, alt=alt,
                         status="skipped" if reason else "ready", reason=reason,
                         geometry_verified=False))
    return rows


class Tracker:
    """One selected identity; loss latches until explicit acquire().

    A local prediction gate never falls back to the full-frame brightest star.
    Unresolvable close companions are rejected, not silently interchanged.
    Frames passed to on_frame must be treated as read-only by consumers.
    """
    def __init__(self, cam, clock, cfg, cancel, on_frame=None):
        self.cam, self.clock, self.cfg, self.cancel = cam, clock, cfg, cancel
        self.on_frame = on_frame
        self.last_quality = {}
        self.selected = None
        self._last = None
        self._velocity = np.zeros(2)
        self._locked = False
        self._transition_frames = 3
        self._profile_flux = None

    def phase_changed(self):
        """Allow a bounded motion-change gate, never a new star selection."""
        self._transition_frames = 3

    def _roi(self, x, y):
        w, h = min(self.cfg.roi, self.cam.W), min(self.cfg.roi, self.cam.H)
        self.cam.set_roi(int(np.clip(round(x - w / 2), 0, self.cam.W - w)),
                         int(np.clip(round(y - h / 2), 0, self.cam.H - h)), w, h)

    def _peaks(self, sub, ox, oy, bg, noise, limit):
        """Bounded peak proposal; never use a truncated list to prove isolation."""
        self.cancel.check()
        # Convert only the tile/crop, never the main-camera frame. Invalid
        # samples cannot become peaks or poison argmax/median filtering.
        finite = np.isfinite(sub)
        work = legacy.median3(np.where(finite, sub, bg).astype(float))
        self.cancel.check()
        work[~finite] = -np.inf
        peaks = []
        for _ in range(limit):
            self.cancel.check()
            iy, ix = np.unravel_index(np.argmax(work), work.shape)
            snr = float((work[iy, ix] - bg) / noise)
            if not math.isfinite(snr) or snr < self.cfg.min_snr:
                return peaks, False
            peaks.append((int(ix + ox), int(iy + oy), snr))
            work[max(0, iy - 7):iy + 8, max(0, ix - 7):ix + 8] = -np.inf
        return peaks, bool(np.max(work) >= bg + self.cfg.min_snr * noise)

    def _full_frame_candidates(self, image):
        """Scan every pixel with overlapping 512px cores and an 8px halo.

        Only background estimation is sampled; narrow (2px FWHM) stars are
        never stride-decimated. The nine-plane median temporary is at most
        528x528, and the global shortlist is bounded at 128 proposals.
        """
        h, w = image.shape
        sample = image[::max(1, math.ceil(h / 256)), ::max(1, math.ceil(w / 256))]
        sample = sample[np.isfinite(sample)].astype(float)
        self.cancel.check()
        if not sample.size:
            return []
        bg = float(np.median(sample))
        noise = max(1e-6, float(1.4826 * np.median(np.abs(sample - bg))))
        best = []
        for y in range(0, h, 512):
            for x in range(0, w, 512):
                self.cancel.check()
                ox, oy = max(0, x - 8), max(0, y - 8)
                tile = image[oy:min(h, y + 520), ox:min(w, x + 520)]
                peaks, _ = self._peaks(tile, ox, oy, bg, noise, 24)
                # Deduplicate halo proposals, not genuine neighboring stars.
                # The shortlist is ONLY a proposal list: isolation is checked
                # again in the original image, including discarded neighbors.
                merged = sorted(best + peaks, key=lambda p: -p[2])
                best = []
                for peak in merged:
                    if not 8 <= peak[0] < w - 8 or not 8 <= peak[1] < h - 8:
                        continue
                    if any(abs(peak[0] - p[0]) <= 7 and abs(peak[1] - p[1]) <= 7
                           for p in best):
                        continue
                    best.append(peak)
                del best[128:]
        self.cancel.check()
        return best

    def _detect(self, image, target=None, gate=None):
        self.cancel.check()
        h, w = image.shape
        if h < 17 or w < 17:
            return None, dict(reason="no suitable star", snr=0.)
        if target is None:
            for ix, iy, _ in self._full_frame_candidates(image):
                self.cancel.check()
                # Revalidate against ALL nearby pixels, not just the capped
                # shortlist or this proposal's tile. This sees companions
                # across tile seams and outside the top-128 proposals.
                point, quality = self._detect(image, (ix, iy), gate=4.)
                if point is not None:
                    return point, quality
            return None, dict(reason="no quality-qualified selected star", snr=0.)
        radius = self.cfg.search_radius if gate is None else gate
        # Reject overlapping possible-identity envelopes, including after a
        # phase change restores the largest gate. Photometry cannot resolve
        # equal-brightness companions if the selected star disappears.
        isolation = max(15., 2 * self.cfg.search_radius)
        if not np.isfinite(target).all():
            return None, dict(reason="nonfinite predicted identity")
        # Candidate acceptance stays inside radius; detection extends far
        # enough to see neighbors of any accepted candidate, not merely
        # neighbors that happen to fall inside the association gate.
        detection_radius = radius + isolation + 8
        ox, oy = max(0, int(target[0] - detection_radius)), max(0, int(target[1] - detection_radius))
        ex, ey = min(w, int(target[0] + detection_radius + 1)), min(h, int(target[1] + detection_radius + 1))
        if ex - ox < 5 or ey - oy < 5:
            return None, dict(reason="predicted identity outside ROI")
        sub = image[oy:ey, ox:ex]
        finite = sub[np.isfinite(sub)].astype(float)
        if not finite.size:
            return None, dict(reason="no finite star pixels", snr=0.)
        bg = float(np.median(finite))
        noise = max(1e-6, float(1.4826 * np.median(np.abs(finite - bg))))
        self.cancel.check()
        peaks, truncated = self._peaks(sub, ox, oy, bg, noise, 128)
        if truncated:
            return None, dict(reason="ambiguous selected identity: too many nearby peaks", snr=0.)
        peaks = [(px, py, snr) for px, py, snr in peaks if 8 <= px < w - 8 and 8 <= py < h - 8]
        if not peaks:
            return None, dict(reason="no suitable star", snr=0.)
        peaks.sort(key=lambda p: math.hypot(p[0] - target[0], p[1] - target[1]))
        for ix, iy, snr in peaks:
            self.cancel.check()
            if not np.isfinite(image[iy - 8:iy + 9, ix - 8:ix + 9]).all():
                continue
            cx, cy = legacy.centroid(image, ix, iy)
            self.cancel.check()
            if not math.isfinite(cx) or not math.isfinite(cy) or math.hypot(cx - target[0], cy - target[1]) > radius:
                continue
            cut = image[iy - 7:iy + 8, ix - 7:ix + 8].astype(float)
            weight = np.maximum(0, cut - bg)
            yy, xx = np.mgrid[iy - 7:iy + 8, ix - 7:ix + 8]
            total = weight.sum()
            variance = float((weight * ((xx - cx) ** 2 + (yy - cy) ** 2)).sum() / max(total, 1))
            fwhm = 2.355 * math.sqrt(variance / 2)
            saturated = bool(np.max(cut) >= getattr(self.cam, "max_adu", float("inf")) * .98)
            crowded = any(0 < math.hypot(px - ix, py - iy) <= isolation for px, py, _ in peaks)
            q = dict(snr=snr, fwhm=fwhm, saturated=saturated, flux=float(total), reason="")
            if saturated or not 1.5 <= fwhm <= 12 or crowded:
                q["reason"] = "ambiguous selected identity: overlapping search envelopes" if crowded else "saturated or unsuitable shape"
                return None, q
            return (float(cx), float(cy)), q
        return None, dict(reason="no quality-qualified selected star", snr=0.)

    def _publish(self, t, image, origin, point, quality):
        self.last_quality = dict(quality, origin=list(origin))
        x, y = point if point is not None else (float("nan"), float("nan"))
        if self.on_frame:
            self.on_frame(dict(image=image, origin=origin, t=t, x=x, y=y,
                               snr=quality.get("snr", 0.), fwhm=quality.get("fwhm", float("nan")),
                               saturated=quality.get("saturated", False)))

    def acquire(self, position=None):
        self.cancel.check()
        self._locked, self.selected, self._last = False, None, None
        self._velocity = np.zeros(2)
        self._transition_frames, self._profile_flux = 3, None
        self.cam.set_roi(0, 0, self.cam.W, self.cam.H)
        t, image, origin = self.cam.expose(self.clock, self.cfg.acq_exposure)
        self.cancel.check()
        target = None if position is None else (position[0] - origin[0], position[1] - origin[1])
        point, q = self._detect(image, target)
        self.cancel.check()
        absolute = None if point is None else (point[0] + origin[0], point[1] + origin[1])
        self._publish(t, image, origin, absolute, q)
        if absolute is None:
            return False
        self.selected, self._last, self._locked = absolute, (t, *absolute), True
        self._roi(*absolute)
        return True

    def grab(self):
        self.cancel.check()
        if not self._locked:
            raise RuntimeError("Selected star lost/not acquired; explicit reacquisition required")
        t, image, origin = self.cam.expose(self.clock, self.cfg.exposure)
        self.cancel.check()
        dt = max(0., t - self._last[0])
        # Bound extrapolation so a bad previous centroid cannot run away.
        prediction = np.array(self._last[1:]) + self._velocity * min(dt, .5)
        target = prediction - np.array(origin)
        # A radial gate must allow two-dimensional seeing AND post-gap
        # acceleration; the 12px trial rejected a measured 12.6px innovation.
        gate = self.cfg.search_radius if self._transition_frames else min(16., self.cfg.search_radius)
        point, q = self._detect(image, target, gate=gate)
        self._transition_frames = max(0, self._transition_frames - 1)
        if point is not None and self._profile_flux is not None:
            ratio = q["flux"] / self._profile_flux
            if not .4 <= ratio <= 2.5:
                point = None
                q["reason"] = "selected identity photometry changed; reacquire explicitly"
        if point is not None:
            self._profile_flux = q["flux"] if self._profile_flux is None else .95 * self._profile_flux + .05 * q["flux"]
        absolute = None if point is None else (point[0] + origin[0], point[1] + origin[1])
        self._publish(t, image, origin, absolute, q)
        if absolute is None:
            self._locked = False
            return t, float("nan"), float("nan")
        if dt > 0:
            observed_velocity = (np.array(absolute) - np.array(self._last[1:])) / dt
            # Stabilize the SEARCH prediction only. Returned/raw centroids are
            # never smoothed or interpolated, especially across reversals.
            self._velocity = .5 * self._velocity + .5 * observed_velocity
        self._last = (t, *absolute)
        self._roi(*absolute)
        return self._last


def _fit(frames, start, end, reference, vector=None):
    f = np.asarray(frames, dtype=float).reshape(-1, 3)
    mask = np.isfinite(f).all(axis=1) & (f[:, 0] >= start) & (f[:, 0] < end)
    f = f[mask]
    if len(f) < 6 or np.ptp(f[:, 0]) < (end - start) * .55:
        return None
    values = f[:, 1:] @ vector if vector is not None else f[:, 1]
    return legacy.fit_line(f[:, 0] - reference, values)


def analyze(frames, cmds, t_stop, sign, u, v0, cfg, measurement_start=5):
    """Legacy line-jump mathematics, with explicit transition roles and gates.

    cmds are (request_time, raw_sidereal_rate). Indices before measurement_start
    are NEVER measurement reversals, including the negative-sign sign test.
    """
    f = np.asarray(frames, dtype=float).reshape(-1, 3)
    if not np.isfinite(v0) or v0 <= 0:
        raise ValueError("invalid drift calibration")
    u = np.asarray(u, dtype=float)
    if u.shape != (2,) or not np.isfinite(u).all() or abs(np.linalg.norm(u) - 1) > .001:
        raise ValueError("invalid RA unit vector")
    vector = u * S_ARCSEC / v0
    rhos = [raw * sign for _, raw in cmds]
    times = [c[0] for c in cmds] + [t_stop]
    transitions, reversals, latencies, missing = [], [], [], 0
    for k in range(3, len(cmds)):
        old, new = rhos[k - 1:k + 1]
        if abs(new - old) < 1e-9:
            continue
        prev, tc, nxt = times[k - 1:k + 2]
        before = _fit(f, (prev + tc) / 2, tc, tc, vector)
        after = _fit(f, (tc + nxt) / 2, nxt, tc, vector)
        role = "measurement" if k >= measurement_start and old * new < 0 else "latency"
        if before is None or after is None:
            missing += 1
            if role == "measurement":
                reversals.append(dict(dir="fwd->rev" if new < 0 else "rev->fwd",
                                      backlash=float("nan"), dead_s=float("nan"),
                                      ok=False, note="insufficient fit data", t=tc))
            continue
        good = all(abs(fit[0] - S_ARCSEC * (1 - rho)) < .25 * S_ARCSEC
                   for fit, rho in ((before, old), (after, new)))
        d = dict(k=k, t=tc, old=old, new=new, jump=after[1] - before[1],
                 fit_before=before, fit_after=after, slope_ok=good,
                 window=nxt - tc, role=role)
        transitions.append(d)
        if role == "latency" and old * new > 0 and good:
            latencies.append(d["jump"] / (S_ARCSEC * (new - old)))
    latency = float(np.mean(latencies)) if latencies else float("nan")
    for d in transitions:
        if d["role"] != "measurement":
            continue
        B = math.copysign(1, d["new"]) * (d["jump"] - S_ARCSEC * latency * (d["new"] - d["old"]))
        dead = B / (S_ARCSEC * abs(d["new"]))
        note = ""
        if not math.isfinite(B):
            note = "missing latency calibration"
        elif not d["slope_ok"]:
            note = "slope off"
        elif dead > .45 * d["window"]:
            note = "phase too short"
        elif B < 0:
            note = "negative signed estimate (unresolved/systematic)"
        d["backlash"] = B
        reversals.append(dict(dir="fwd->rev" if d["new"] < 0 else "rev->fwd",
                              backlash=B, dead_s=dead, ok=not note, note=note, t=d["t"]))
    reversals.sort(key=lambda r: r["t"])
    values = [r["backlash"] for r in reversals if r["ok"]]
    median = lambda xs: float(np.median(xs)) if len(xs) else float("nan")
    noise = median([d["fit_after"][2] for d in transitions])
    backlash, spread = median(values), float(np.std(values)) if values else float("nan")
    fwd = median([r["backlash"] for r in reversals if r["ok"] and r["dir"] == "fwd->rev"])
    rev = median([r["backlash"] for r in reversals if r["ok"] and r["dir"] == "rev->fwd"])
    # Do NOT divide by sqrt(n): temporally correlated points and shared
    # calibration can dominate. Include line extrapolation and latency spread.
    latency_error = S_ARCSEC * 2 * cfg.rate * (float(np.ptp(latencies)) if len(latencies) > 1 else float("inf"))
    uncertainty = max(3 * noise, 2 * spread, abs(fwd - rev) / 2, latency_error, .02 * abs(backlash)) if values else float("inf")
    notes = ["Uncertainty is a conservative diagnostic allowance, not a calibrated confidence interval.",
             "Sign-calibration transitions excluded; RA-axis arcseconds, not on-sky arcseconds."]
    quality = "valid"
    if not values or len(latencies) < 2 or not math.isfinite(uncertainty):
        quality = "invalid"
        notes.append("Insufficient reversal or latency calibration data.")
    elif len(values) < max(4, 2 * cfg.cycles) or missing or backlash <= uncertainty:
        quality = "uncertain"
        notes.append("Incomplete reversals or backlash unresolved relative to uncertainty.")
    finite = np.isfinite(f).all(axis=1)
    lost = float(1 - finite.mean()) if len(f) else 1.
    if lost:
        quality = "invalid"
        notes.append("Selected-star identity lost; restart acquisition/calibration.")
    projected = f[finite, 1:] @ vector
    return dict(backlash=backlash, spread=spread, fwd_rev=fwd, rev_fwd=rev,
                n_good=len(values), n_total=len(reversals), latency_s=latency,
                latency_samples=latencies, drift_px_s=v0, sign=sign, u=u.tolist(),
                noise_arcsec=noise, lost_frac=lost, reversals=reversals,
                quality=quality, uncertainty_arcsec=uncertainty, notes=notes,
                schema_version=2, algorithm="drift-line-jump/2-role-filtered",
                _raw=dict(frames=f, t=f[finite, 0], p=projected,
                          cmds=list(zip(times[:-1], rhos)), trans=transitions, t_stop=t_stop))


def measure(mount, tracker, clock, cfg, cancel, on_event=lambda event: None):
    cfg.validate()
    cancel.check()
    # Validate ALL rates before the first motion (including the latency steps).
    validator = getattr(mount, "validate_rate", None)
    if validator is None:
        raise RuntimeError("Mount adapter must provide validate_rate(rate_deg_s)")
    for raw in (0, 1, -1, cfg.rate, -cfg.rate, 1 + 2 * cfg.rate, -1 - 2 * cfg.rate):
        validator(raw * S_DEG)
    initial_tracking = mount.get_tracking()
    frames, cmds, command_log, frame_quality = [], [], [], []
    sign, u, v0 = None, None, None
    failure, result, cleanup = None, None, []

    def step(raw, duration, name):
        cancel.check()
        on_event(dict(type="phase", phase=name, duration=duration, raw_rate=raw))
        cancel.check()
        tc = clock.now()
        phase_changed = getattr(tracker, "phase_changed", None)
        if phase_changed:
            phase_changed()
        mount.move_axis(raw * S_DEG)
        cmds.append((tc, raw))
        command_log.append(dict(t=tc, returned=clock.now(), raw_rate=raw, phase=name))
        end = tc + duration
        while clock.now() < end:
            cancel.check()
            sample = tracker.grab()
            frames.append(sample)
            frame_quality.append(dict(t=sample[0], **tracker.last_quality))
            on_event(dict(type="sample", t=sample[0], x=sample[1], y=sample[2]))
            if not np.isfinite(sample).all():
                raise RuntimeError("Selected star lost; measurement stopped, reacquire explicitly")

    try:
        mount.stop()
        mount.tracking(False)
        step(0, cfg.cal, "drift")
        step(1, cfg.cal, "sign")
        tc = cmds[1][0]
        fx = _fit(frames, cmds[0][0] + .4 * (tc - cmds[0][0]), tc, tc, np.array([1., 0.]))
        fy = _fit(frames, cmds[0][0] + .4 * (tc - cmds[0][0]), tc, tc, np.array([0., 1.]))
        if fx is None or fy is None:
            raise RuntimeError("Insufficient drift calibration samples")
        velocity = np.array([fx[0], fy[0]])
        v0 = float(np.linalg.norm(velocity))
        if v0 * cfg.cal < 3:
            raise RuntimeError("Insufficient tracking-off drift")
        u = velocity / v0
        fp = _fit(frames, tc + .5 * cfg.cal, clock.now(), tc, u)
        if fp is None:
            raise RuntimeError("Insufficient sign-calibration samples")
        ratio = fp[0] / v0
        if abs(ratio) < .3:
            sign = 1.
        elif abs(ratio - 2) < .3:
            sign = -1.
        else:
            raise RuntimeError(f"Unreliable MoveAxis sign calibration ({ratio:.2f}x)")
        on_event(dict(type="calibration", u=u.tolist(), v0=v0, sign=sign))
        step(sign, 6., "sign_settle")
        step(sign * (1 + 2 * cfg.rate), 3., "latency_fast")
        step(sign, 4., "latency_slow")
        forward = cfg.phase * (1 + cfg.rate) / (cfg.rate - 1)
        for i in range(cfg.cycles):
            step(-sign * cfg.rate, cfg.phase, f"reverse_{i + 1}")
            step(sign * cfg.rate, forward, f"forward_{i + 1}")
        cancel.check()
        result = analyze(frames, cmds, clock.now(), sign, u, v0, cfg)
        # Conservative drift-calibration slope allowance, separate from sample
        # residual noise and reversal spread. No claimed coverage probability.
        fractional = max(fx[2], fy[2]) * 3 / max(v0 * cfg.cal * .6, 1e-12)
        result["calibration_fractional_allowance"] = fractional
        result["uncertainty_arcsec"] += abs(result["backlash"]) * fractional
        if result["quality"] != "invalid" and (fractional > .1 or result["backlash"] <= result["uncertainty_arcsec"]):
            result["quality"] = "uncertain"
            result["notes"].append("Drift calibration has a large residual allowance.")
        # Report temporal correlation, without pretending orthogonal seeing is
        # an exact removable component of the RA measurement.
        f = np.asarray(frames)
        mask = (f[:, 0] > cmds[0][0] + .4 * (tc - cmds[0][0])) & (f[:, 0] < tc)
        baseline = f[mask]
        delta = baseline[:, 0] - tc
        residual = baseline[:, 1:] - np.column_stack((fx[0] * delta + fx[1], fy[0] * delta + fy[1]))
        along = residual @ u
        orthogonal = residual @ np.array([-u[1], u[0]])
        correlation = float(np.corrcoef(along[:-1], along[1:])[0, 1]) if len(along) > 3 and np.std(along) > 1e-8 else 0.
        result.update(baseline_lag1=correlation,
                      orthogonal_noise_arcsec=float(np.std(orthogonal) * S_ARCSEC / v0),
                      cadence_s=float(np.median(np.diff(f[:, 0]))))
        if correlation > .5:
            if result["quality"] == "valid":
                result["quality"] = "uncertain"
            result["notes"].append(
                "Strong measured baseline correlation (lag-1 > 0.5); candidate ineligible "
                "until correlation-aware confidence is validated. The diagnostic allowance "
                "is not a calibrated confidence interval.")
    except Exception as exc:
        failure = exc
    finally:
        try:
            mount.stop()
        except Exception as exc:
            cleanup.append(f"stop failed: {exc}")
        # Faults never restart tracking. Cancellation restores only after a
        # successful stop; adjustment mode must be handled separately by GUI.
        if not cleanup and (failure is None or isinstance(failure, Cancelled)):
            try:
                mount.tracking(initial_tracking)
            except Exception as exc:
                cleanup.append(f"tracking restore failed: {exc}")
    if result is None:
        result = dict(backlash=float("nan"), spread=float("nan"), noise_arcsec=float("nan"),
                      quality="invalid", uncertainty_arcsec=float("inf"),
                      u=None if u is None else u.tolist(), drift_px_s=v0,
                      notes=[str(failure)], schema_version=2,
                      _raw=dict(frames=np.asarray(frames).reshape(-1, 3), cmds=cmds,
                                trans=[], t_stop=clock.now()))
    result["_raw"].update(command_log=command_log, frame_quality=frame_quality)
    result["command_rate_convention"] = "tracking-positive sidereal" if failure is None else "raw driver sidereal (see command_log)"
    result.update(settings=asdict(cfg), initial_tracking=initial_tracking,
                  cleanup_errors=cleanup, status="cancelled" if isinstance(failure, Cancelled) else "failed" if failure or cleanup else "completed")
    if cleanup:
        result["quality"] = "invalid"
        result["notes"].extend(cleanup)
        if failure is None:
            failure = RuntimeError("; ".join(cleanup))
    if failure:
        failure.partial_result = result
        if cfg.partial_on_error:
            try:
                folder = create_run(cfg.out)
                failure.partial_path = save_partial(result, folder, "partial", {})
            except Exception as exc:
                result["notes"].append(f"Partial save failed: {exc}")
        raise failure
    return result


def _jsonable(value):
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, np.generic):
        return _jsonable(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _write_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(_jsonable(data), stream, indent=2, allow_nan=False)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def create_run(out):
    folder = Path(out) / (datetime.now().strftime("run_%Y%m%d_%H%M%S_%f_") + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def save_result(result, outdir, tag, meta):
    """Write legacy-compatible CSVs plus strict JSON; JSON is commit marker.

    Caller must supply a unique point tag; existing tags are never overwritten.
    Additional diagnostics live in <tag>_diagnostics.json (no camera images).
    """
    folder = Path(outdir)
    folder.mkdir(parents=True, exist_ok=True)
    if not tag or Path(tag).name != tag or any(c in tag for c in '/\\:') or tag in (".", ".."):
        raise ValueError("tag must be a plain filename stem")
    path = folder / f"{tag}_result.json"
    if any((folder / f"{tag}_{suffix}").exists() for suffix in
           ("result.json", "frames.csv", "commands.csv", "diagnostics.json")):
        raise FileExistsError(f"Result tag already exists: {tag}")
    raw = result.get("_raw", {})
    for suffix, header, rows in (("frames", ["t", "x", "y"], raw.get("frames", [])),
                                 ("commands", ["t", "rho_sidereal"], raw.get("cmds", []))):
        with (folder / f"{tag}_{suffix}.csv").open("x", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
    _write_json(folder / f"{tag}_diagnostics.json", {k: v for k, v in raw.items() if k != "frames"})
    public = {k: v for k, v in result.items() if not k.startswith("_")}
    public.update({k: v for k, v in meta.items() if k not in public and not k.startswith("_")})
    public["metadata"] = meta
    public["saved_utc"] = datetime.now(timezone.utc).isoformat()
    _write_json(path, public)
    return path


def save_partial(result, outdir, tag="partial", meta=None):
    partial = dict(result)
    partial.setdefault("status", "cancelled")
    partial["quality"] = "invalid"
    return save_result(partial, outdir, tag, meta or {})


def load_result(path):
    with Path(path).open(encoding="utf-8") as stream:
        result = json.load(stream)
    if not isinstance(result, dict):
        raise ValueError("Result must be a JSON object")
    result.pop("_raw", None)
    if "quality" not in result:
        result.update(quality="uncertain", uncertainty_arcsec=None)
        result.setdefault("notes", []).append("Legacy result: quality/uncertainty not validated.")
    return result


def save_manifest(path, rows, status):
    path = Path(path)
    if path.suffix.lower() != ".json":
        path = path / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, dict(schema_version=2, status=status, rows=rows,
                          updated_utc=datetime.now(timezone.utc).isoformat()))
    return path


def candidate(rows):
    """Only qualified measurements; ties are returned explicitly, not hidden."""
    def qualified(row):
        try:
            return (row.get("quality") == "valid" and row.get("status", "completed") == "completed"
                    and math.isfinite(float(row["backlash"])) and float(row["backlash"]) >= 0
                    and math.isfinite(float(row["uncertainty_arcsec"])) and float(row["uncertainty_arcsec"]) >= 0)
        except (KeyError, TypeError, ValueError):
            return False
    good = [row for row in rows if qualified(row)]
    if not good:
        return None
    best = min(good, key=lambda row: float(row["backlash"]))
    upper = float(best["backlash"]) + float(best["uncertainty_arcsec"])
    tied = [row for row in good if float(row["backlash"]) - float(row["uncertainty_arcsec"]) <= upper]
    return dict(best, tied=len(tied) > 1, candidate_angles=[row.get("angle") for row in tied],
                candidate_note="Overlapping uncertainty allowances" if len(tied) > 1 else "Remeasure before adjustment; not a binding diagnosis")