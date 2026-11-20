"""Read-only interpretation of recorded measurements; never a motion authorization."""
import math
from collections.abc import Mapping


BINDING_WARNING = (
    "There is no universal safe mesh gap or backlash target. This measurement cannot detect "
    "binding or certify binding-free operation. Low backlash is not proof of binding. "
    "Do not tighten blindly; follow the mount manufacturer's mechanical procedure."
)
REPEAT_MAP = (
    "Repeat at the same pose to check repeatability, then use 'Measure at several angles' before considering "
    "mesh tuning. Inspect the sampled range and follow the manufacturer's procedure."
)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def assess_result(result, target_arcsec=None):
    """Return display text and classifications, not a tolerance or safety decision.

    B ± U is a diagnostic envelope, NOT a calibrated confidence interval.
    Missing legacy uncertainty is not inferred from reversal spread.
    """
    r = result if isinstance(result, Mapping) else {}
    b, u = _number(r.get("backlash")), _number(r.get("uncertainty_arcsec"))
    target = _number(target_arcsec)
    target_valid = target is not None and target >= 0
    details = [
        "B is inferred angular lost motion in RA-axis arcsec, not the observed star path or on-sky arcsec.",
        "U is a diagnostic allowance; B ± U is not a calibrated confidence interval.",
        "Command latency is excluded from B. This is not a tracking/guiding performance prediction.",
    ]
    if r.get("simulation") is True:
        details.insert(0, "SIMULATION — educational synthetic data, not a hardware measurement.")
    elif r.get("simulation") is False:
        details.insert(0, "Recorded hardware measurement; hardware accuracy and mechanical safety are not certified.")
    else:
        details.insert(0, "Measurement source not recorded; do not assume hardware or simulation.")
    if b is not None:
        details.append(f"B: {b:.6g} RA-axis arcsec.")
    if u is not None:
        details.append(f"U: {u:.6g} RA-axis arcsec (diagnostic allowance).")
    for key, label, unit in (
        ("fwd_rev", "Forward → reverse", "RA-axis arcsec"),
        ("rev_fwd", "Reverse → forward", "RA-axis arcsec"),
        ("noise_arcsec", "Fit residual noise", "RA-axis arcsec"),
        ("orthogonal_noise_arcsec", "Orthogonal residual noise", "RA-axis-equivalent arcsec"),
        ("spread", "Reversal spread (not U)", "RA-axis arcsec"),
        ("baseline_lag1", "Baseline lag-1 correlation", ""),
        ("cadence_s", "Measured cadence", "s"),
        ("n_good", "Usable reversals", ""),
        ("n_total", "Attempted reversals", ""),
    ):
        value = _number(r.get(key))
        if value is not None:
            details.append(f"{label}: {value:.6g} {unit}.".replace(" .", "."))
    settings = r.get("settings")
    rate = _number(settings.get("rate")) if isinstance(settings, Mapping) else None
    if b is not None and b >= 0 and rate is not None and rate > 0:
        seconds = b / 15.041067 / rate
        if math.isfinite(seconds):
            details.append(
                f"Equivalent reversal take-up model at measurement rate {rate:.6g}× sidereal: "
                f"{seconds:.6g} s = B / (15.041067 × rate). Latency excluded; "
                "not a tracking/guiding prediction.")
    notes = r.get("notes")
    if isinstance(notes, str):
        notes = [notes]
    if isinstance(notes, (list, tuple)):
        details.extend(str(note) for note in notes if note)
    if r.get("reason"):
        details.append(str(r["reason"]))
    if target_arcsec is None:
        details.append("Size target not set; no universal mount tolerance is assumed.")
    elif target_valid:
        details.append(f"Selected comparison goal: {target:.6g} RA-axis arcsec; interpretation only, not a safe mesh gap.")
    else:
        details.append("Invalid comparison goal; use a finite nonnegative target. No target comparison made.")

    status, title, tone = "unreliable", "Measurement unreliable — no mesh conclusion", "warning"
    meaning = "Missing, invalid or failed measurement evidence cannot establish the backlash size or mesh condition."
    action = "Check quality reasons, reacquire a suitable star and repeat with fresh calibration. Make no mesh conclusion from this result."
    quality = str(r.get("quality", "")).lower()
    usable = (
        str(r.get("status", "completed")).lower() == "completed"
        and quality in {"valid", "uncertain", "noisy", "correlated"}
        and b is not None and b >= 0 and u is not None and u >= 0
        and not r.get("cleanup_errors")
    )
    for key in ("fwd_rev", "rev_fwd"):
        value = _number(r.get(key))
        if value is not None and value < 0:
            usable = False
    for key in ("n_good", "drift_px_s"):
        if key in r and (_number(r[key]) is None or _number(r[key]) <= 0):
            usable = False
    if "sign" in r and _number(r["sign"]) not in {-1., 1.}:
        usable = False
    if (_number(r.get("lost_frac")) or 0) > 0:
        usable = False
    correlation = _number(r.get("baseline_lag1"))
    if usable:
        if b <= u:
            status, title = "unresolved", "Not resolved above measurement uncertainty"
            meaning = "The inferred lost motion is not resolved above the diagnostic allowance. This is NOT a zero-backlash measurement and NOT proof of binding."
            action = "Repeat at the same pose; improve star quality, seeing stability and cadence before comparing size or considering mesh tuning."
        elif quality != "valid" or (correlation is not None and correlation > .5):
            status, title = "inconclusive", "Inconclusive — repeat under improved conditions"
            meaning = "Positive lost motion exceeds U, but uncertain/noisy/correlated evidence does not support a reliable size or target decision."
            action = "Repeat at the same pose with a better isolated star, steadier seeing and adequate cadence. Do not adjust mesh from this result."
        else:
            status, title, tone = "measurable", "Measurable backlash — size target not set", "neutral"
            meaning = "Angular lost motion is measurable above U at this pose. Its size alone does not say whether the mesh is too wide or too narrow."
            action = REPEAT_MAP
            if target_valid:
                if b - u > target:
                    status, title, tone = "above_target", "Above selected target — relatively wide versus target", "warning"
                    meaning = "The whole diagnostic envelope is above your selected comparison goal, not a universal mount tolerance."
                elif b + u <= target:
                    status, title = "within_target", "Within selected target — relatively narrow versus target"
                    meaning = "The whole diagnostic envelope is within your selected comparison goal. This does not certify a safe mesh or absence of binding."
                else:
                    status, title = "overlaps_target", "Diagnostic envelope overlaps target — cannot decide"
                    meaning = "B ± U crosses the selected comparison goal; the diagnostic envelope cannot decide above versus within target."
            elif target_arcsec is not None:
                title = "Measurable backlash — comparison goal invalid"
    verdict, headline, summary = _verdict(status, b, u, target if target_valid else None)
    return dict(status=status, title=title, meaning=meaning, action=action, details=details,
                binding_warning=BINDING_WARNING, tone=tone, backlash_arcsec=b,
                uncertainty_arcsec=u, target_arcsec=target if target_valid else None,
                verdict=verdict, headline=headline, summary=summary,
                simulation=r.get("simulation") is True, qualified=usable and status in {
                    "measurable", "above_target", "within_target", "overlaps_target"})


def _verdict(status, b, u, target):
    """Plain-language headline for the always-visible result card."""
    value = f"{b:.1f} ± {u:.1f} arcsec" if b is not None and u is not None else "unknown"
    limit = f"{target:g} arcsec" if target is not None else ""
    if status == "above_target":
        return ("large", f"LARGE backlash: {value}, above your {limit} limit",
                "If a repeat measurement agrees, the gear mesh may be too loose. Follow the mount manual's "
                "adjustment procedure, then measure again. Do not adjust from one run.")
    if status == "within_target":
        return ("acceptable", f"ACCEPTABLE backlash: {value}, within your {limit} limit",
                "No adjustment is needed for this limit. Optionally measure several angles to check the whole axis.")
    if status == "overlaps_target":
        return ("borderline", f"BORDERLINE: {value} straddles your {limit} limit",
                "The measurement cannot say which side of your limit this is on. Repeat it to decide.")
    if status == "measurable":
        return ("measured", f"BACKLASH MEASURED: {value}",
                "Turn on 'Compare with my acceptable limit' to see whether this is large or acceptable.")
    if status == "unresolved":
        return ("none_detected", f"NO BACKLASH DETECTED: smaller than the measurement noise (±{u:.1f} arcsec)",
                "Either the backlash is very small, or the gears are tight/binding. This test cannot tell which. "
                "Repeat once; if the mount also feels rough or stalls, inspect it mechanically.")
    if status == "inconclusive":
        return ("inconclusive", f"INCONCLUSIVE: about {value}, but not trustworthy",
                "Repeat with a better isolated star, steadier seeing and no guiding. Do not adjust from this run.")
    return ("unreliable", "MEASUREMENT UNRELIABLE: repeat it",
            "This run failed or its quality was too low to give a backlash value. See the reasons below and try again.")


def assess_map(rows):
    """Summarize qualified distinct sampled angles; never locate binding or authorize return."""
    rows = list(rows) if isinstance(rows, (list, tuple)) else []
    groups = {}
    qualified_count = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        angle = _number(row.get("angle"))
        assessment = assess_result(row)
        if angle is None or not assessment["qualified"]:
            continue
        qualified_count += 1
        b, u = assessment["backlash_arcsec"], assessment["uncertainty_arcsec"]
        lower, upper = b - u, b + u
        if angle in groups:
            lower, upper = min(lower, groups[angle][0]), max(upper, groups[angle][1])
        groups[angle] = (lower, upper)
    count = len(groups)
    excluded = len(rows) - qualified_count
    details = [f"{count} distinct qualified angle(s); {qualified_count} qualified row(s); "
               f"{excluded} invalid/unqualified or missing-angle row(s) excluded.",
               "Diagnostic envelopes are not confidence intervals. Unsampled angles remain unknown."]
    if any(isinstance(row, Mapping) and row.get("simulation") is True for row in rows):
        details.append("SIMULATION present — educational synthetic points are not hardware evidence.")
    low, high = [], []
    if count < 2:
        status, title = "insufficient", "No tight spot across the axis established"
        meaning = "One point is not a map; at least two distinct qualified angles are needed even to compare sampled gaps."
    else:
        low_edge = min(value[1] for value in groups.values())
        high_edge = max(value[0] for value in groups.values())
        low = sorted(angle for angle, value in groups.items() if value[0] <= low_edge)
        high = sorted(angle for angle, value in groups.items() if value[1] >= high_edge)
        status = "tied" if len(low) > 1 or len(high) > 1 else "ranked"
        title = "Sampled gap comparison — " + ("overlapping/tied diagnostic envelopes" if status == "tied" else "measured extremes only")
        angles = lambda values: ", ".join(f"{a:+g}°" for a in values)
        meaning = f"Lowest measured gap candidate(s): {angles(low)}. Highest measured gap candidate(s): {angles(high)}. Neither is binding proof or a safe mesh setting."
    return dict(status=status, title=title, meaning=meaning, details=details,
                action="Select a row → Result details to inspect its evidence. Repeat the same pose, then extend 'Measure at several angles' before mesh tuning.",
                binding_warning=BINDING_WARNING, tone="neutral", qualified_angles=count,
                excluded_count=excluded, lowest_angles=low, highest_angles=high)