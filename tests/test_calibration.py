import datetime as dt
import types

import numpy as np

import raster_utils as ru


def _item(scale=None, offset=None, baseline="05.12"):
    asset = types.SimpleNamespace(extra_fields={})
    if scale is not None or offset is not None:
        asset.extra_fields["raster:bands"] = [{"scale": scale, "offset": offset}]
    return types.SimpleNamespace(
        datetime=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc),
        properties={"s2:processing_baseline": baseline},
        assets={"B04": asset, "B08": asset},
    )


def test_stac_scale_offset_order_is_physical_raw_times_scale_plus_offset():
    item = _item(0.0001, -0.1)
    red = ru.to_reflectance(np.array([3000], dtype="uint16"), item, "B04")
    nir = ru.to_reflectance(np.array([7000], dtype="uint16"), item, "B08")
    assert np.allclose(red, [0.20])
    assert np.allclose(nir, [0.60])
    assert np.allclose((nir - red) / (nir + red), [0.50])


def test_pb04_fallback_is_equivalent_to_dn_minus_1000_over_10000():
    item = _item(baseline="05.12")
    red = ru.to_reflectance(np.array([3000], dtype="uint16"), item, "B04")
    nir = ru.to_reflectance(np.array([7000], dtype="uint16"), item, "B08")
    assert np.allclose(red, [0.20])
    assert np.allclose(nir, [0.60])


def test_invalid_scale_is_rejected():
    item = _item(0.0, 0.0)
    try:
        ru.band_scale_offset(item, "B04")
    except ValueError:
        return
    raise AssertionError("invalid scale must raise ValueError")
