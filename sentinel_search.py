"""
sentinel_search.py - STAC search for Sentinel-2 and Sentinel-1.

v1 DEFECT FIXED (P-14) - THE SINGLE MOST IMPORTANT CHANGE IN v2:

    v1 sentinel2_pc.py:
        query={"eo:cloud_cover": {"lt": 30}}

    eo:cloud_cover is a property of the ENTIRE 290 x 110 km granule. During
    the Maharashtra monsoon essentially no granule falls below 30%, so the
    search returned ZERO items. The GitHub Actions log of 2026-08-06 shows
    "No Sentinel-2 data" for all 29 eligible lands, and the skip rate rose
    59.5% (May) -> 87.9% (Jun) -> 99.1% (Jul) -> 100% (Aug).

    Meanwhile Sentinel-2A/2B/2C were imaging every one of those fields
    roughly every 2.5-3 days. Approximately 5 acquisitions per field per
    15-day window were retrieved and discarded unopened.

    A granule at 70% cloud is routinely clear over a single 2-hectare farm,
    and per-pixel SCL masking already existed downstream to handle exactly
    that. The scene-level pre-filter was both redundant and the sole cause
    of total seasonal failure.

v2 pulls every acquisition and lets the per-field SCL mask decide.

v1 DEFECT FIXED (P-01): item.datetime was never read, so the true
acquisition instant was discarded and rows were stamped date.today().
v2 returns it as a first-class field.

v1 DEFECT FIXED (P-15): list(search.get_items()) with no sortby - STAC order
is implementation-defined, so "the first valid scene" (used for the
thumbnail, GeoTIFF and every spatial statistic) was arbitrary.
"""

from datetime import datetime, timedelta, timezone
from typing import List

from pystac_client import Client
import planetary_computer as pc

from config import (
    STAC_URL, S2_COLLECTION, S1_RTC_COLLECTION, S1_GRD_COLLECTION,
    LOOKBACK_DAYS, S1_LOOKBACK_DAYS, SCENE_CLOUD_REJECT_ABOVE,
)
from logger import logger

_client = None


def client() -> Client:
    global _client
    if _client is None:
        _client = Client.open(STAC_URL, modifier=pc.sign_inplace)
    return _client


def _window(days: int, end: datetime = None):
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return f"{start.isoformat()}/{end.isoformat()}"


def search_s2(geometry, days: int = None, end: datetime = None) -> List:
    """
    Every Sentinel-2 L2A acquisition intersecting `geometry` in the window,
    newest first. NO scene-level cloud filter beyond discarding fully
    opaque granules.
    """
    days = days or LOOKBACK_DAYS

    search = client().search(
        collections=[S2_COLLECTION],
        intersects=geometry,
        datetime=_window(days, end),
        sortby=[{"field": "properties.datetime", "direction": "desc"}],
    )

    items = list(search.items())

    kept, dropped = [], 0
    for it in items:
        cc = it.properties.get("eo:cloud_cover")
        if cc is not None and cc > SCENE_CLOUD_REJECT_ABOVE:
            dropped += 1
            continue
        kept.append(it)

    logger.info(
        f"S2 search: {len(items)} acquisitions in {days}d, "
        f"{dropped} fully opaque dropped, {len(kept)} to evaluate per-pixel"
    )
    return kept


def search_s1(geometry, days: int = None, end: datetime = None) -> List:
    """
    Sentinel-1 for the monsoon fallback. Prefers RTC (terrain-corrected
    gamma0, already calibrated) over GRD (uncalibrated DN).

    v1 read raw GRD DN and applied 10*log10() with no calibration LUT,
    no terrain correction and no speckle filter (P-16). It produced NULL
    in 100% of rows.
    """
    days = days or S1_LOOKBACK_DAYS

    for collection in (S1_RTC_COLLECTION, S1_GRD_COLLECTION):
        try:
            search = client().search(
                collections=[collection],
                intersects=geometry,
                datetime=_window(days, end),
                query={"sar:instrument_mode": {"eq": "IW"}},
                sortby=[{"field": "properties.datetime", "direction": "desc"}],
            )
            items = [i for i in search.items()
                     if "vv" in {k.lower() for k in i.assets}
                     and "vh" in {k.lower() for k in i.assets}]
            if items:
                logger.info(f"S1 search: {len(items)} {collection} acquisitions")
                return [(collection, i) for i in items]
        except Exception as e:
            logger.warning(f"S1 search failed for {collection}: {e}")

    return []


def acquisition_meta(item) -> dict:
    """Provenance that v1 discarded entirely (P-09)."""
    p = item.properties
    dt = item.datetime
    return {
        "scene_id": item.id,
        "acquisition_time": dt.isoformat() if dt else None,
        "acquisition_date": dt.date().isoformat() if dt else None,
        "scene_cloud_cover": p.get("eo:cloud_cover"),
        "platform": p.get("platform"),
        "mgrs_tile": p.get("s2:mgrs_tile") or p.get("grid:code"),
        "relative_orbit": p.get("sat:relative_orbit"),
        "processing_baseline": p.get("s2:processing_baseline"),
    }
