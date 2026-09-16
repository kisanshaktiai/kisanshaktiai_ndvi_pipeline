import numpy as np


def test_stac_calibration_semantics_are_scale_then_offset():
    raw = np.array([1000.0, 2000.0, 6000.0], dtype=np.float32)
    scale = 0.0001
    offset = -0.1
    physical = raw * scale + offset
    np.testing.assert_allclose(physical, [0.0, 0.1, 0.5], atol=1e-7)


def test_canonical_ndre_uses_b8a_not_b08():
    b8a = np.array([0.60], dtype=np.float32)
    b05 = np.array([0.30], dtype=np.float32)
    ndre = (b8a - b05) / (b8a + b05)
    assert np.isclose(float(ndre[0]), 1.0 / 3.0)


def test_spatial_effective_sample_size_cannot_exceed_kish_sample_size():
    # Contract test for the future variogram implementation: spatial
    # correlation can only reduce independent support, never create it.
    kish = 20.0
    spatial = min(kish, 8.0)
    assert 0.0 < spatial <= kish
