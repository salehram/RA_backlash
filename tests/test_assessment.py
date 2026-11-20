"""Interpretation contracts: diagnostic evidence is not a mesh safety decision."""
from copy import deepcopy
import math

import pytest

from ra_backlash_app.assessment import assess_map, assess_result


def result(**changes):
    return dict(dict(backlash=12., uncertainty_arcsec=2., quality="valid",
                     status="completed", n_good=6, n_total=6), **changes)


def text(assessment):
    return " ".join([assessment["title"], assessment["meaning"], assessment["action"],
                     assessment["binding_warning"], *assessment["details"]])


def test_default_detection_and_next_action_not_arbitrary_mount_tolerance():
    a = assess_result(result())
    assert a["status"] == "measurable"
    assert a["title"] == "Measurable backlash — size target not set"
    assert a["target_arcsec"] is None
    assert "same pose" in a["action"] and "Start map" in a["action"]
    assert "before" in a["action"]
    assert "no universal safe mesh gap" in a["binding_warning"]
    assert "cannot detect binding" in a["binding_warning"]
    assert "not the observed star path" in text(a)
    assert "not a calibrated confidence interval" in text(a)


@pytest.mark.parametrize("b,u", [(0., 0.), (0., 2.), (1., 2.), (2., 2.)])
@pytest.mark.parametrize("quality", ["valid", "uncertain"])
def test_unresolved_is_not_zero_or_binding(b, u, quality):
    a = assess_result(result(backlash=b, uncertainty_arcsec=u, quality=quality), 100.)
    assert a["status"] == "unresolved"
    assert a["title"] == "Not resolved above measurement uncertainty"
    assert "NOT a zero-backlash measurement" in a["meaning"]
    assert "NOT proof of binding" in a["meaning"]
    assert not a["qualified"]


@pytest.mark.parametrize("patch", [
    {"backlash": None}, {"backlash": -1}, {"backlash": math.nan}, {"backlash": math.inf},
    {"backlash": True}, {"uncertainty_arcsec": None}, {"uncertainty_arcsec": -1},
    {"uncertainty_arcsec": math.nan}, {"uncertainty_arcsec": math.inf},
    {"quality": "invalid"}, {"quality": None}, {"quality": "unknown"},
    {"status": "failed"}, {"status": "cancelled"}, {"status": "aborted"},
    {"status": "skipped"}, {"status": "planned"}, {"status": None},
    {"n_good": 0}, {"n_good": None}, {"fwd_rev": -1}, {"rev_fwd": -1},
    {"sign": 0}, {"sign": None}, {"drift_px_s": -2}, {"lost_frac": .01},
    {"cleanup_errors": ["tracking restore failed"]},
])
def test_invalid_measurements_never_make_mesh_or_target_conclusion(patch):
    a = assess_result(result(**patch), 100.)
    assert a["status"] == "unreliable"
    assert "no mesh conclusion" in a["title"]
    assert not a["qualified"]


@pytest.mark.parametrize("source", [None, {}, [], "bad", {"backlash": 1., "spread": .1},
    {"quality": "valid", "uncertainty_arcsec": 1.}])
def test_missing_and_legacy_uncertainty_are_not_guessed(source):
    assert assess_result(source)["status"] == "unreliable"


@pytest.mark.parametrize("patch", [{"quality": "uncertain"}, {"quality": "correlated"},
    {"quality": "noisy"}, {"baseline_lag1": .51}])
def test_uncertain_positive_signal_is_inconclusive_even_with_goal(patch):
    a = assess_result(result(**patch), 100.)
    assert a["status"] == "inconclusive"
    assert not a["qualified"]
    assert all(word in a["action"] for word in ("star", "seeing", "cadence", "Do not adjust"))


@pytest.mark.parametrize("target,status", [
    (0., "above_target"), (9.999, "above_target"),
    (10., "overlaps_target"), (12., "overlaps_target"), (13.999, "overlaps_target"),
    (14., "within_target"), (20., "within_target"),
])
def test_goal_uses_entire_diagnostic_envelope_and_inclusive_upper_boundary(target, status):
    a = assess_result(result(), target)
    assert a["status"] == status
    assert a["target_arcsec"] == target
    assert "not a calibrated confidence interval" in text(a)
    assert "cannot detect binding" in a["binding_warning"]
    if status == "above_target":
        assert "relatively wide versus target" in a["title"]
    if status == "within_target":
        assert "relatively narrow versus target" in a["title"]


@pytest.mark.parametrize("target", [-1, math.nan, math.inf, "bad", True])
def test_invalid_goal_makes_no_numeric_comparison(target):
    a = assess_result(result(), target)
    assert a["status"] == "measurable"
    assert a["target_arcsec"] is None
    assert "comparison goal invalid" in a["title"]


@pytest.mark.parametrize("sign", [-1, 1])
def test_direction_calibration_sign_does_not_change_physical_positive_backlash(sign):
    assert assess_result(result(sign=sign))["status"] == "measurable"


def test_finite_diagnostics_and_rate_model_only_with_recorded_rate():
    r = result(fwd_rev=11., rev_fwd=13., noise_arcsec=.2, baseline_lag1=.1,
               cadence_s=.14, settings={"rate": 2.}, latency_s=10.)
    a = assess_result(r)
    description = text(a)
    assert all(s in description for s in ("Forward → reverse: 11", "Reverse → forward: 13",
        "Fit residual noise: 0.2", "Usable reversals: 6", "Measured cadence: 0.14"))
    assert "take-up model at measurement rate 2×" in description
    assert f"{12 / (15.041067 * 2):.6g} s" in description  # No latency added.
    assert "Latency excluded; not a tracking/guiding prediction" in description
    assert "take-up model" not in text(assess_result(result()))
    for rate in (None, 0, -1, math.nan):
        assert "take-up model" not in text(assess_result(result(settings={"rate": rate})))
    a = assess_result(result(noise_arcsec=math.nan, fwd_rev=None, rev_fwd=math.inf))
    assert all(s not in text(a) for s in ("nan", "inf RA", "Forward → reverse:"))


def test_pure_input_and_source_provenance():
    r = result(settings={"rate": 2}, notes=["Diagnostic note"], simulation=True)
    before = deepcopy(r)
    a = assess_result(r, 20.)
    assert r == before
    assert "educational synthetic" in text(a)
    assert "Recorded hardware measurement" in text(assess_result(result(simulation=False)))
    assert "source not recorded" in text(assess_result(result()))


def test_map_requires_distinct_qualified_angles_not_duplicate_repeats():
    rows = [result(angle=25.), result(angle=25., backlash=13.),
            result(angle=50., quality="uncertain"), result(angle=75., backlash=-1.)]
    a = assess_map(rows)
    assert a["qualified_angles"] == 1 and a["excluded_count"] == 2
    assert a["status"] == "insufficient"
    assert not a["lowest_angles"] and not a["highest_angles"]
    assert "One point is not a map" in a["meaning"]


def test_map_ties_use_overlapping_diagnostic_envelopes():
    rows = [result(angle=25.), result(angle=50., backlash=14.),
            result(angle=75., backlash=30.), result(angle=math.nan)]
    a = assess_map(rows)
    assert a["status"] == "tied"
    assert a["lowest_angles"] == [25., 50.]
    assert a["highest_angles"] == [75.]
    assert a["excluded_count"] == 1
    assert "binding proof" in a["meaning"]


def test_map_ranked_extremes_simulation_and_no_mutation():
    rows = [result(angle=-25., simulation=True), result(angle=25., backlash=20.)]
    before = deepcopy(rows)
    a = assess_map(rows)
    assert rows == before
    assert a["status"] == "ranked"
    assert a["lowest_angles"] == [-25.] and a["highest_angles"] == [25.]
    assert "educational synthetic" in text(a)
    assert "Select a row" in a["action"]


@pytest.mark.parametrize("rows", [None, [], [None, {}, {"angle": 25.}]])
def test_empty_or_invalid_map_has_no_extrema(rows):
    a = assess_map(rows)
    assert a["qualified_angles"] == 0
    assert a["status"] == "insufficient"