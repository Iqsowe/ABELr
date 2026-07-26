"""`catalog.preview_size_advice` — the Standard Preview Size diagnostic.

`requestJpegThumbnail` never renders above the catalog's Standard Preview Size:
it serves the largest tier it already holds (SDK `LrPhoto.html`: "request sizes
are treated as minimums… the smallest preview that satisfies either one is
returned"). On "Automatic" that cap lands below the 2048 measurement grid on any
sub-4K display — every render comes back sub-grid, forever, and no SDK call can
change the setting. So the App has to read it out of the `.lrcat` and name it in
the failure. These tests build a minimal `Adobe_variablesTable`, no real catalog.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.core import catalog as catalogmod
from app.core.render_metrics import MEASURE_LONG_EDGE


def _lrcat(tmp_path, value: str | None, *, table: bool = True):
    path = tmp_path / "Fake.lrcat"
    conn = sqlite3.connect(path)
    if table:
        conn.execute("CREATE TABLE Adobe_variablesTable (name TEXT, value TEXT)")
        if value is not None:
            conn.execute(
                "INSERT INTO Adobe_variablesTable VALUES (?, ?)",
                ("AgPreviewBuilder_standardSize", value),
            )
    conn.commit()
    conn.close()
    return str(path)


def test_automatic_is_reported_with_the_setting_to_change(tmp_path):
    advice = catalogmod.preview_size_advice(_lrcat(tmp_path, "automatic"), MEASURE_LONG_EDGE)
    assert advice is not None
    assert "Automatic" in advice
    assert str(MEASURE_LONG_EDGE) in advice
    assert "Standard Preview Size" in advice


@pytest.mark.parametrize("value", ["1024", "1440.0"])
def test_explicit_size_below_the_grid_is_reported(tmp_path, value):
    advice = catalogmod.preview_size_advice(_lrcat(tmp_path, value), MEASURE_LONG_EDGE)
    assert advice is not None
    assert value in advice


@pytest.mark.parametrize("value", ["2048", "2880.0"])
def test_size_at_or_above_the_grid_says_nothing(tmp_path, value):
    """No false alarm once the user has fixed it: a sub-grid render then has
    another cause and must not be blamed on the catalog setting."""
    assert catalogmod.preview_size_advice(_lrcat(tmp_path, value), MEASURE_LONG_EDGE) is None


def test_unreadable_catalog_is_silent(tmp_path):
    """Diagnostic only — never breaks a run, whatever the catalog looks like."""
    assert catalogmod.preview_size_advice(None, MEASURE_LONG_EDGE) is None
    assert catalogmod.preview_size_advice(tmp_path / "missing.lrcat", MEASURE_LONG_EDGE) is None
    assert catalogmod.preview_size_advice(_lrcat(tmp_path, None), MEASURE_LONG_EDGE) is None
    assert catalogmod.preview_size_advice(
        _lrcat(tmp_path, None, table=False), MEASURE_LONG_EDGE
    ) is None


def test_standard_preview_size_returns_the_raw_value(tmp_path):
    assert catalogmod.standard_preview_size(_lrcat(tmp_path, "automatic")) == "automatic"


def test_no_write_function_is_exposed(tmp_path):
    """Editing the `.lrcat` directly was tried and reverted (a live run wrote
    the value in the wrong format and Lightroom's Library came back up empty —
    catalog integrity was fine, but the risk isn't worth it). This module reads
    and advises only; the fix is manual, in Lightroom's own UI."""
    assert not hasattr(catalogmod, "set_standard_preview_size")
    assert not hasattr(catalogmod, "CatalogInUse")
