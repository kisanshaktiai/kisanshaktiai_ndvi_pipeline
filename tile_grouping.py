"""
tile_grouping.py — fetch each satellite scene ONCE, clip it to every field.

THIS MODULE IS THE SYNTHESIS OF THE TWO REPOSITORIES, AND IT IS THE ONE PLACE
WHERE BOTH WERE HALF-RIGHT.

    kisanshaktiai_ndvi_pipeline (v1)
        Clipped correctly, per field, from the field polygon.       CORRECT
        Opened the same scene once per land.                        WASTEFUL
        At 100,000 farms: ~100,000 STAC searches per cycle.

    kisanshakti-ndvi-engine (retired)
        Fetched once per MGRS tile.                                 CORRECT
        Then AGGREGATED NDVI over the WHOLE 12,060 km2 tile and
        weighted that histogram to fields by overlap_ratio.         INVALID

Why the engine repo's aggregation is agronomically void, not merely coarse:

    field 5 acres = 0.02 km2
    MGRS tile      = 12,060 km2
    ratio          = 0.00017%

A single histogram over 43QCU contains the Western Ghats, the Krishna river,
Kolhapur city, sugarcane, paddy, fallow and quarries. Weighting it by overlap
does not extract the field's NDVI; it produces a regional average wearing a
field-shaped label. The field signal was never captured, so no downstream
weighting can recover it. Its own schema knew better: get_land_ndvi_grids
returns overlap_ratio and the table is ndvi_spatial_analytics with a bbox —
the design intended many small GRID CELLS per tile. The ingester wrote one
row per whole tile.

v2 does the only correct thing:

    GROUP lands by MGRS tile
      -> search STAC ONCE per tile
        -> open each scene's COG ONCE (HTTP range reads, lazy)
          -> WINDOWED READ per field, clipped to the buffered boundary
            -> one row per (field, scene)

Cloud-Optimised GeoTIFFs are internally tiled, so rasterio fetches only the
bytes overlapping each field window. A 2-hectare field costs a few hundred KB,
not the 4.3 GB a full-tile read would need — which is why the engine repo would
have OOM'd on a GitHub Actions runner had its two silent bugs been fixed.
"""

from collections import defaultdict
from typing import Dict, List, Iterable
from shapely.geometry import shape

from sentinel_search import search_s2
from logger import logger


def group_lands_by_tile(lands: Iterable[dict]) -> Dict[str, List[dict]]:
    """
    Group lands by MGRS tile so each tile's imagery is searched once.

    Lands without a stored mgrs_tile_id fall into '__untiled__' and are
    processed individually. That is a coverage gap, not an error: only 5 of
    41 lands currently carry mgrs_tile_id.

    NOTE: this deliberately does NOT call find_mgrs_tile_for_land(). That
    function has an unbounded nearest-neighbour fallback (audit finding C-09):
    a land outside every loaded tile is silently assigned the geometrically
    nearest one, with no distance ceiling. A field 400 km from its assigned
    tile would be harvested from imagery that does not contain it. Better to
    process untiled lands individually than to guess their tile.
    """
    groups: Dict[str, List[dict]] = defaultdict(list)
    for land in lands:
        tile = land.get("tile_id") or land.get("mgrs_tile_id")
        groups[str(tile) if tile else "__untiled__"].append(land)

    tiled = {k: v for k, v in groups.items() if k != "__untiled__"}
    untiled = len(groups.get("__untiled__", []))

    total = sum(len(v) for v in groups.values())
    logger.info(
        f"Tile grouping: {total} lands -> {len(tiled)} tiles "
        f"({untiled} untiled, processed individually)"
    )
    if tiled:
        saved = total - len(tiled) - untiled
        logger.info(f"STAC searches avoided by grouping: {max(saved, 0)}")

    return groups


def scenes_for_group(lands: List[dict], lookback_days: int = None) -> List:
    """
    One STAC search covering every land in the group.

    Searches the UNION of the group's geometries so no field is missed at a
    tile edge. Scene selection is then per field: a scene returned here may
    still be rejected for an individual field by its SCL mask, which is
    correct — cloud is local.

    NO scene-level cloud filter. That filter (eo:cloud_cover < 30 in v1
    sentinel2_pc.py:28) is the single defect that produced a 100% monsoon
    skip rate: it judged a 2-hectare field by the cloudiness of a
    290 x 110 km granule. Credit where due — the retired engine repo had no
    such filter and got this right.
    """
    from shapely.ops import unary_union

    geoms = []
    for land in lands:
        raw = (land.get("boundary_geom") or land.get("boundary_geojson")
               or land.get("boundary_polygon_old"))
        if not raw:
            continue
        try:
            g = shape(raw)
            if not g.is_valid:
                g = g.buffer(0)
            if not g.is_empty:
                geoms.append(g)
        except Exception as e:
            logger.debug(f"Land {land.get('id')} geometry unusable for grouping: {e}")

    if not geoms:
        # None (not []) so process_land falls back to a per-land search.
        return None

    union = unary_union(geoms)
    # convex_hull keeps the STAC intersects payload small for scattered fields
    return search_s2(union.convex_hull, days=lookback_days)


def log_group_plan(groups: Dict[str, List[dict]]) -> None:
    for tile, lands in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        label = "untiled (individual)" if tile == "__untiled__" else f"tile {tile}"
        logger.info(f"  {label}: {len(lands)} land(s)")
