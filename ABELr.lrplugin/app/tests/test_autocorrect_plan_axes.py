"""PLAN.md COV4 — `core.autocorrect` branches not reached by the existing
suite: `_plan_seeds`'s wb/hsl tail (only "expo"/"calib" axes were exercised
through `plan()` before this), `_plan_embedded`'s divergence diagnostic and
hsl axis, `_pair_for`'s variant-fallback, and the embedded-mode
no-calibration-match calib branch.
"""

from __future__ import annotations

from app.core import autocorrect as ac
from app.core.response import ResponseModel, WBResponse
from app.core.seed_match import SeedTarget
from app.tests.conftest import make_analysis, make_band, make_measure, make_neutral, make_seed, make_tone


# --------------------------------------------------------------------------- #
# _pair_for — variant fallback when the requested scope is incomplete
# --------------------------------------------------------------------------- #
def test_pair_for_sharp_requested_falls_back_to_global_when_sharp_missing():
    m = make_measure(
        "p1",
        embedded_global=make_analysis(), neutral_global=make_analysis(),
        embedded_sharp=None, neutral_sharp=None,
    )
    t, n, variant = ac._pair_for(m, "sharp")
    assert t is not None and n is not None
    assert variant == "global"


def test_pair_for_global_requested_falls_back_to_sharp_when_global_missing():
    m = make_measure(
        "p1",
        embedded_sharp=make_analysis(), neutral_sharp=make_analysis(),
        embedded_global=None, neutral_global=None,
    )
    t, n, variant = ac._pair_for(m, "global")
    assert t is not None and n is not None
    assert variant == "sharp"


def test_pair_for_neither_scope_available_returns_none():
    m = make_measure("p1")
    t, n, variant = ac._pair_for(m, "global")
    assert t is None and n is None
    assert variant == "global"  # unchanged, caller treats t/n None as "no anchor"


# --------------------------------------------------------------------------- #
# _embedded_band_targets / _band_targets_from_seed_match — remaining branches
# --------------------------------------------------------------------------- #
def test_embedded_band_targets_skips_unreliable_band():
    from app.core.pipeline import RenderAnalysis

    t = RenderAnalysis(tone=None, neutral=None, bands=[make_band("Red", frac=0.0)])  # below _BAND_MIN_FRAC
    tgs = ac._embedded_band_targets(t, ac.ProfileBias(n=8), ignore_bias=True)
    assert tgs == {}


def test_embedded_band_targets_historical_mode_skips_band_without_bias_norm():
    from app.core.pipeline import RenderAnalysis

    t = RenderAnalysis(tone=None, neutral=None, bands=[make_band("Red")])
    bias = ac.ProfileBias(n=8)  # no "Red" entry in bias.bands
    tgs = ac._embedded_band_targets(t, bias, ignore_bias=False)
    assert tgs == {}  # no norm for this band -> no target


def test_band_targets_from_seed_match_none_target_returns_empty():
    assert ac._band_targets_from_seed_match(None) == {}


def test_band_targets_from_seed_match_no_bands_returns_empty():
    t = SeedTarget(
        temperature=None, tint=None, tone=None, bands=None,
        shadow_tint=None, red_hue=None, red_saturation=None,
        green_hue=None, green_saturation=None, blue_hue=None, blue_saturation=None,
        n_matched=1, seed_ids=["s"],
    )
    assert ac._band_targets_from_seed_match(t) == {}


# --------------------------------------------------------------------------- #
# _plan_embedded — divergence diagnostic (global vs sharp-zone ΔL*)
# --------------------------------------------------------------------------- #
def _embedded_measure(pid, *, embedded_l, neutral_l, embedded_sharp_l=None, neutral_sharp_l=None):
    """A photo with both global and sharp T/N pairs -> eligible for the
    global<->sharp divergence check (needs all 4 populated)."""
    embedded_sharp_l = embedded_l if embedded_sharp_l is None else embedded_sharp_l
    neutral_sharp_l = neutral_l if neutral_sharp_l is None else neutral_sharp_l
    return make_measure(
        pid,
        embedded_global=make_analysis(tone=make_tone(embedded_l)),
        neutral_global=make_analysis(tone=make_tone(neutral_l)),
        embedded_sharp=make_analysis(tone=make_tone(embedded_sharp_l)),
        neutral_sharp=make_analysis(tone=make_tone(neutral_sharp_l)),
        neutral_asshot_temp=5500.0, neutral_asshot_tint=0.0,
    )


def test_plan_embedded_flags_global_sharp_divergence():
    # global ΔL* = 60-50 = 10, sharp ΔL* = 50-50 = 0 -> |10-0| = 10 > _DIVERGENCE_L (4.0)
    m = _embedded_measure("p1", embedded_l=60.0, neutral_l=50.0, embedded_sharp_l=50.0, neutral_sharp_l=50.0)
    _adj, diag = ac.plan([m], axes=frozenset({"expo"}), forced_embedded=True, model=None, seed_pool=[])
    assert any("diverge" in note for note in diag.notes)


def test_plan_embedded_no_divergence_note_when_global_and_sharp_agree():
    m = _embedded_measure("p1", embedded_l=55.0, neutral_l=50.0)  # same delta both scopes
    _adj, diag = ac.plan([m], axes=frozenset({"expo"}), forced_embedded=True, model=None, seed_pool=[])
    assert not any("diverge" in note for note in diag.notes)


def test_plan_embedded_no_anchor_note_when_measure_has_neither_pair():
    m = make_measure("p1")  # no embedded_*/neutral_* at all
    adjustments, diag = ac.plan([m], axes=frozenset({"expo"}), forced_embedded=True, model=None, seed_pool=[])
    assert adjustments == []
    assert any("no neutral anchor" in note for note in diag.notes)


# --------------------------------------------------------------------------- #
# _plan_embedded — wb axis: deviant+calibrated writes, deviant+uncalibrated notes
# --------------------------------------------------------------------------- #
def _wb_measure(pid, *, t_a, t_b, n_a, n_b, frac=0.1):
    return make_measure(
        pid,
        embedded_global=make_analysis(neutral=make_neutral(a=t_a, b=t_b, frac=frac)),
        neutral_global=make_analysis(neutral=make_neutral(a=n_a, b=n_b, frac=frac)),
        neutral_asshot_temp=5500.0, neutral_asshot_tint=0.0,
    )


def test_plan_embedded_wb_deviant_and_calibrated_writes_temperature_tint():
    m = _wb_measure("p1", t_a=0.0, t_b=0.0, n_a=10.0, n_b=10.0)  # big excess cast -> deviant
    model = ResponseModel(camera="ILCE-7M4", profile="Neutral", wb=WBResponse(
        da_dtemp=0.5, db_dtemp=0.9, da_dtint=0.2, db_dtint=-0.2,
    ))
    adjustments, diag = ac.plan([m], axes=frozenset({"wb"}), forced_embedded=True, model=model, seed_pool=[])
    assert len(adjustments) == 1
    dev = adjustments[0].develop
    assert dev["WhiteBalance"] == "Custom"
    assert "Temperature" in dev and "Tint" in dev
    assert any(note.startswith("wb: 1 corrected") for note in diag.notes)


def test_plan_embedded_wb_deviant_but_uncalibrated_writes_nothing_and_notes_it():
    m = _wb_measure("p1", t_a=0.0, t_b=0.0, n_a=10.0, n_b=10.0)
    adjustments, diag = ac.plan([m], axes=frozenset({"wb"}), forced_embedded=True, model=None, seed_pool=[])
    assert adjustments == []
    assert any("WB response not calibrated" in note for note in diag.notes)


def test_plan_embedded_wb_conforming_writes_nothing():
    m = _wb_measure("p1", t_a=0.0, t_b=0.0, n_a=0.5, n_b=0.5)  # within the WB cast deadband
    adjustments, diag = ac.plan([m], axes=frozenset({"wb"}), forced_embedded=True, model=None, seed_pool=[])
    assert adjustments == []
    assert any(note.startswith("wb: 0 corrected, 1 matching") for note in diag.notes)


def test_plan_embedded_wb_low_neutral_frac_treated_as_conforming():
    m = _wb_measure("p1", t_a=0.0, t_b=0.0, n_a=10.0, n_b=10.0, frac=0.001)  # below _MIN_NEUTRAL_FRAC
    adjustments, diag = ac.plan([m], axes=frozenset({"wb"}), forced_embedded=True, model=None, seed_pool=[])
    assert adjustments == []
    assert any(note.startswith("wb: 0 corrected, 1 matching") for note in diag.notes)


# --------------------------------------------------------------------------- #
# _plan_embedded — hsl axis
# --------------------------------------------------------------------------- #
def test_plan_embedded_hsl_deviant_band_writes_delta():
    # Target (in-camera JPEG) chroma far from the neutral render's -> saturation
    # reduction beyond the dead zone (nominal gain, no calibrated response needed).
    m = make_measure(
        "p1",
        embedded_global=make_analysis(bands=[make_band("Red", median_chroma=80.0, frac=0.2)]),
        neutral_global=make_analysis(bands=[make_band("Red", median_chroma=80.0, frac=0.2)]),
        neutral_asshot_temp=5500.0, neutral_asshot_tint=0.0,
        raw_bands=[make_band("Red", sat_clip_frac=0.10)],  # RAW confirms -> reduction allowed
    )
    # embedded (T) target chroma is LOWER than the neutral render's current chroma
    # -> stats.median_chroma (80, from neutral_global) exceeds target.chroma (T=embedded_global=80 too,
    # so instead make T's chroma low to create excess on the neutral render's own band stats).
    m.embedded_global.bands[0] = make_band("Red", median_chroma=20.0, frac=0.2)
    adjustments, diag = ac.plan([m], axes=frozenset({"hsl"}), forced_embedded=True, model=None, seed_pool=[])
    assert len(adjustments) == 1
    dev = adjustments[0].develop
    assert "SaturationAdjustmentRed" in dev
    assert dev["SaturationAdjustmentRed"] < 0  # reduction only
    assert any(note.startswith("hsl: 1/1") for note in diag.notes)


def test_plan_embedded_hsl_matching_band_writes_nothing():
    m = make_measure(
        "p1",
        embedded_global=make_analysis(bands=[make_band("Red", median_chroma=40.0, frac=0.2)]),
        neutral_global=make_analysis(bands=[make_band("Red", median_chroma=40.0, frac=0.2)]),
        neutral_asshot_temp=5500.0, neutral_asshot_tint=0.0,
    )
    adjustments, diag = ac.plan([m], axes=frozenset({"hsl"}), forced_embedded=True, model=None, seed_pool=[])
    assert adjustments == []
    assert any(note.startswith("hsl: 0/1") for note in diag.notes)


# --------------------------------------------------------------------------- #
# _plan_embedded — calib axis with a seed pool present but no usable match
# --------------------------------------------------------------------------- #
def test_plan_embedded_calib_no_calibration_on_matched_seed_writes_nothing():
    seed_pool = [make_seed("seed1")]  # no calib fields set -> has_calibration() False
    m = make_measure("p1", raw_tone=make_tone(), asshot_rg=0.5, asshot_bg=0.5)
    adjustments, diag = ac.plan(
        [m], axes=frozenset({"calib"}), forced_embedded=True, model=None, seed_pool=seed_pool,
    )
    assert adjustments == []
    assert any(note.startswith("calib: 0/1") for note in diag.notes)


# --------------------------------------------------------------------------- #
# _plan_seeds — usable-filter note
# --------------------------------------------------------------------------- #
def test_plan_seeds_notes_photos_with_no_current_render():
    seed_pool = [make_seed("s1")]
    no_render = make_measure("p1", analysis=None, raw_tone=make_tone(), asshot_rg=0.5, asshot_bg=0.5)
    has_render = make_measure("p2", analysis=make_analysis(), raw_tone=make_tone(), asshot_rg=0.5, asshot_bg=0.5)
    _adj, diag = ac.plan(
        [no_render, has_render], axes=frozenset({"expo"}), model=None, seed_pool=seed_pool,
    )
    assert any("1 photo(s) with no current render" in note for note in diag.notes)


# --------------------------------------------------------------------------- #
# _plan_seeds — wb axis (matched+refined, unmatched)
# --------------------------------------------------------------------------- #
def test_plan_seeds_wb_matched_writes_temperature_tint():
    seed_pool = [make_seed("s1", l=50.0, temp=6000.0, tint=5.0)]
    m = make_measure(
        "p1", analysis=make_analysis(tone=make_tone(50.0), neutral=make_neutral(a=1.0, b=1.0, frac=0.1)),
        raw_tone=make_tone(50.0), asshot_rg=0.5, asshot_bg=0.5,
    )
    adjustments, diag = ac.plan([m], axes=frozenset({"wb"}), model=None, seed_pool=seed_pool)
    assert len(adjustments) == 1
    dev = adjustments[0].develop
    assert dev["WhiteBalance"] == "Custom"
    assert dev["Temperature"] == 6000  # k-NN transplant, on-grid already
    assert dev["Tint"] == 5
    assert any(note.startswith("wb: 1/1") for note in diag.notes)


def test_plan_seeds_wb_refines_with_calibrated_response():
    seed_pool = [make_seed("s1", l=50.0, temp=6000.0, tint=0.0)]
    model = ResponseModel(camera="ILCE-7M4", profile="Neutral", wb=WBResponse(
        da_dtemp=0.5, db_dtemp=0.9, da_dtint=0.2, db_dtint=-0.2,
    ))
    m = make_measure(
        "p1", analysis=make_analysis(tone=make_tone(50.0), neutral=make_neutral(a=2.0, b=2.0, frac=0.1, n=500)),
        raw_tone=make_tone(50.0), asshot_rg=0.5, asshot_bg=0.5,
    )
    adjustments, _diag = ac.plan([m], axes=frozenset({"wb"}), model=model, seed_pool=seed_pool)
    dev = adjustments[0].develop
    # Refinement perturbs the raw k-NN transplant (6000K) away from the seed value.
    assert dev["Temperature"] != 6000


def test_plan_seeds_wb_no_match_skips_photo():
    # Seed has no Temperature at all -> matched but `t.temperature is None` -> skipped.
    seed_pool = [make_seed("s1", l=50.0, temp=None)]
    m = make_measure(
        "p1", analysis=make_analysis(), raw_tone=make_tone(), asshot_rg=0.5, asshot_bg=0.5,
    )
    adjustments, diag = ac.plan([m], axes=frozenset({"wb"}), model=None, seed_pool=seed_pool)
    assert adjustments == []
    assert any(note.startswith("wb: 0/1") for note in diag.notes)


# --------------------------------------------------------------------------- #
# _plan_seeds — hsl axis
# --------------------------------------------------------------------------- #
def test_plan_seeds_hsl_matched_writes_delta():
    from app.core.seed_match import SeedVector

    seed_pool = [
        SeedVector(
            photo_id="s1", asshot_rg=0.5, asshot_bg=0.5, raw_median_l=50.0,
            temperature=6000.0, tint=0.0, preview_tone=make_tone(50.0),
            preview_bands=[make_band("Red", median_chroma=10.0, frac=0.2)],
        )
    ]
    m = make_measure(
        "p1",
        analysis=make_analysis(bands=[make_band("Red", median_chroma=80.0, frac=0.2)]),
        raw_tone=make_tone(50.0), asshot_rg=0.5, asshot_bg=0.5,
        raw_bands=[make_band("Red", sat_clip_frac=0.10)],
    )
    adjustments, diag = ac.plan([m], axes=frozenset({"hsl"}), model=None, seed_pool=seed_pool)
    assert len(adjustments) == 1
    dev = adjustments[0].develop
    assert "SaturationAdjustmentRed" in dev
    assert dev["SaturationAdjustmentRed"] < 0
    assert any(note.startswith("hsl: 1/1") for note in diag.notes)


def test_plan_seeds_hsl_no_deviant_band_writes_nothing():
    from app.core.seed_match import SeedVector

    seed_pool = [
        SeedVector(
            photo_id="s1", asshot_rg=0.5, asshot_bg=0.5, raw_median_l=50.0,
            temperature=6000.0, tint=0.0, preview_tone=make_tone(50.0),
            preview_bands=[make_band("Red", median_chroma=40.0, frac=0.2)],
        )
    ]
    m = make_measure(
        "p1",
        analysis=make_analysis(bands=[make_band("Red", median_chroma=40.0, frac=0.2)]),
        raw_tone=make_tone(50.0), asshot_rg=0.5, asshot_bg=0.5,
    )
    adjustments, diag = ac.plan([m], axes=frozenset({"hsl"}), model=None, seed_pool=seed_pool)
    assert adjustments == []
    assert any(note.startswith("hsl: 0/1") for note in diag.notes)
