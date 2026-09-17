from ndvi_temporal_predictor import forecast


def history(values):
    return [
        {"acquisition_date": d, "observed_ndvi": y, "quality_score": 0.9}
        for d, y in values
    ]


def test_forecast_requires_three_observations():
    assert forecast(history([
        ("2026-09-01", 0.40),
        ("2026-09-06", 0.45),
    ])) == []


def test_forecast_is_bounded_and_future_only():
    out = forecast(history([
        ("2026-08-20", 0.30),
        ("2026-08-25", 0.36),
        ("2026-08-30", 0.42),
        ("2026-09-04", 0.47),
        ("2026-09-09", 0.52),
    ]), forecast_days=5)
    assert len(out) == 5
    assert all(-1 <= x["estimated_ndvi"] <= 1 for x in out)
    assert all(x["estimated_ndvi_low"] <= x["estimated_ndvi"] <= x["estimated_ndvi_high"] for x in out)
    assert out[0]["acquisition_date"] == "2026-09-10"
    assert out[-1]["acquisition_date"] == "2026-09-14"


def test_large_latest_gap_fails_closed():
    assert forecast(history([
        ("2026-08-01", 0.30),
        ("2026-08-06", 0.35),
        ("2026-08-11", 0.40),
        ("2026-08-25", 0.45),
    ])) == []
