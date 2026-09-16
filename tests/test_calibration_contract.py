"""Regression tests for Sentinel-2 reflectance calibration semantics.

These tests are local-only and require no network or database.
"""
import datetime as dt
import os
import sys
import types

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import raster_utils as ru  # noqa: E402


def _item(*, scale=None, offset=None, baseline="05.12"):
    item = types.SimpleNamespace()
    item.properties = {"s2:processing_baseline": baseline}
    item.datetime = dt.datetime(2026, 8, 21, tzinfo=dt.timezone.utc)
    assets = {}
    for band in ("B04", "B08"):
        extra = {}
        if scale is not None or offset is not None:
            extra["raster:bands"] = [{"scale": scale, "offset": offset}]
        assets[band] = types.SimpleNamespace(extra_fields=extra)
    item.assets = assets
    return item


def test_stac_scale_offset_is_scale_then_physical_offset():
    item = _item(scale=0.0001, offset=-0.1)
    red = ru.to_reflectance(np.array([3000], dtype=np.uint16), item, "B04")
    nir = ru.to_reflectance(np.array([7000], dtype=np.uint16), item, "B08")
    assert np.allclose(red, [0.20])
    assert np.allclose(nir, [0.60])
    assert np.allclose((nir - red) / (nir + red), [0.50])


def test_baseline_fallback_applies_dn_offset_before_quantification():
    item = _item(baseline="05.12")
    red = ru.to_reflectance(np.array([3000], dtype=np.uint16), item, "B04")
    nir = ru.to_reflectance(np.array([7000], dtype=np.uint16), item, "B08")
    assert np.allclose(red, [0.20])
    assert np.allclose(nir, [0.60])


def test_zero_and_negative_reflectance_are_nan():
    item = _item(scale=0.0001, offset=-0.1)
    out = ru.to_reflectance(np.array([1000, 900], dtype=np.uint16), item, "B04")
    assert np.isnan(out[0])
    assert np.isnan(out[1])


def test_invalid_scale_fails_closed():
    item = _item(scale=0.0, offset=0.0)
    try:
        ru.to_reflectance(np.array([3000], dtype=np.uint16), item, "B04")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid scale must fail closed")


def test_invalid_offset_fails_closed():
    item = _item(scale=0.0001, offset=float("nan"))
    try:
        ru.to_reflectance(np.array([3000], dtype=np.uint16), item, "B04")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid offset must fail closed")
