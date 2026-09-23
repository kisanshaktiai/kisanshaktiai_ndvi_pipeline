"""
tile_reader.py - read each tile-scene ONCE, serve every parcel inside it.

WHY
---
`read_band` opens the remote COG and reads a window per parcel, per band, per
stage. Measured on 2026-09-22: 30 lands, 3 MGRS tiles, 7 scenes -> ~100
windowed HTTP reads per land to produce 1.5 observations. Per-land reads scale
linearly with farms, so 1,000,000 lands is ~100,000,000 reads per night: about
111 days per cycle at today's concurrency (see the runtime audit).

Satellite imagery is stored the other way round: one scene is a set of COGs
covering a 110 km tile that thousands of parcels share. This module reads a
BLOCK of that tile once per band and then slices each parcel's window out of
the array in memory.

ACCURACY IS THE BINDING CONSTRAINT
----------------------------------
`subset_band` reproduces `raster_utils.read_band` step for step on the slice:
the same rasterio window rounding, the same all_touched rasterisation, the same
`coverage_fractions`, the same nodata handling, the same `to_reflectance`, the
same SCL dilation, and the same reference-grid reprojection. It returns the
same 4-tuple. `tests/test_tile_reader.py` asserts array-for-array equality
against `read_band` on synthetic rasters, including the coverage fractions that
EPC and purity are derived from.

Two details that would silently corrupt values if got wrong, and how they are handled:

1. Window alignment. `rasterio.mask` with crop=True derives the window from the
   geometry bounds and rounds it outward to whole pixels. Slicing must use the
   identical rule or every parcel's grid shifts by a pixel and EPC changes.
   `_geom_window` mirrors `rasterio.windows.from_bounds` + floor/ceil rounding,
   and the block is read on the source grid so offsets stay integral.

2. SCL dilation. `read_band` pads the read by CLOUD_DILATION_PX before dilating
   so cloud just outside the parcel still grows into it. The block read applies
   the same pad, and dilation runs ONCE on the block before slicing, which is
   equivalent for a morphological dilation as long as the block extends at
   least the pad beyond every parcel in it - enforced by `pad_m` below.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import rasterio
from rasterio.mask import mask as rio_mask
from rasterio.warp import reproject, Resampling
from shapely.geometry import box, mapping
from shapely.ops import unary_union

from config import CLOUD_DILATION_PX
from logger import logger
from raster_utils import (coverage_fractions, dilate_scl, reproject_geometry,
                          to_reflectance)
from resource_budget import RASTER_CACHE, COUNTERS

# A block is read once per band. Two ceilings apply, because a pixel cap alone
# is not a memory bound (release audit, 2026-09-22): 4096x4096 float32 is 67 MB
# for ONE band, so eight bands across ten workers would be gigabytes.
#   BLOCK_MAX_PIXELS    - per band, per block
#   resource_budget.RASTER_CACHE - process-wide bytes across every worker
# A block that cannot be afforded is simply not cached: those parcels fall back
# to per-parcel reads, which is the pre-batching behaviour and loses no data.
BLOCK_MAX_PIXELS = 1024 * 1024        # 1 M px/band = 4 MB float32, 32 MB for 8 bands


class SceneBlock:
    """Bands of one scene read once over a block of a tile."""

    __slots__ = ("item", "bands", "transforms", "crs", "nodata", "res", "geom_wgs84", "nbytes")

    def __init__(self, item, geom_wgs84):
        self.item = item
        self.geom_wgs84 = geom_wgs84
        self.bands: Dict[str, np.ndarray] = {}      # masked arrays, native grid
        self.transforms: Dict[str, object] = {}
        self.crs: Dict[str, object] = {}
        self.nodata: Dict[str, float] = {}
        self.res: Dict[str, float] = {}
        self.nbytes: Dict[str, int] = {}

    def has(self, band_key: str) -> bool:
        return band_key in self.bands


# Parcel context reads a ring grown around the field until the total footprint
# is ~1 acre (parcel_intelligence.adaptive_context_geometry). The widest ring
# is for a point-sized parcel: sqrt(4046.86 / pi) = 35.9 m. Padding every block
# read by 60 m (ring + one 20 m pixel + rounding) lets the context stage be
# served from the same block. subset_band additionally REFUSES any window that
# would leave the block, so correctness never depends on this number.
CONTEXT_BLOCK_PAD_M = 60.0


def read_scene_block(item, band_keys: List[str], geom_wgs84,
                     categorical: Tuple[str, ...] = ("SCL",),
                     extra_pad_m: float = 0.0) -> SceneBlock:
    """Read every band of one scene once, over the block geometry's bounds
    (plus extra_pad_m on every side, e.g. CONTEXT_BLOCK_PAD_M)."""
    block = SceneBlock(item, geom_wgs84)
    for band_key in band_keys:
        asset = item.assets.get(band_key) if hasattr(item.assets, "get") else item.assets[band_key]
        if asset is None:
            continue
        try:
            with rasterio.open(asset.href) as src:
                geom_proj = reproject_geometry(geom_wgs84, src.crs)
                nodata = src.nodata if src.nodata is not None else 0
                # Same pad rule as read_band, so dilation on the block equals
                # dilation on a padded per-parcel read.
                pad_m = float(extra_pad_m or 0.0)
                if band_key in categorical and CLOUD_DILATION_PX > 0:
                    pad_m += CLOUD_DILATION_PX * 10.0 + max(abs(src.res[0]), 10.0)
                # square-cornered pad: a rounded buffer would mask block corners
                minx, miny, maxx, maxy = geom_proj.bounds
                clip = box(minx - pad_m, miny - pad_m, maxx + pad_m, maxy + pad_m)
                data, transform = rio_mask(src, [mapping(clip)], crop=True, filled=False,
                                           all_touched=True, nodata=nodata)
                data = data[0] if data.ndim == 3 else data
                if data.size > BLOCK_MAX_PIXELS:
                    logger.warning(f"block too large for {band_key} on {getattr(item,'id','?')}: "
                                   f"{data.size} px - parcels fall back to per-parcel reads")
                    COUNTERS.bump("block_skipped_pixel_cap")
                    continue
                # charge data AND mask: a masked array holds both
                nbytes = int(np.ma.getdata(data).nbytes) + int(np.ma.getmaskarray(data).nbytes)
                if not RASTER_CACHE.acquire(nbytes):
                    logger.info(f"block not cached for {band_key} on {getattr(item,'id','?')}: "
                                f"process raster budget full - per-parcel reads used")
                    COUNTERS.bump("block_skipped_budget")
                    continue
                block.nbytes[band_key] = nbytes
                COUNTERS.bump("block_band_reads")
                block.bands[band_key] = data
                block.transforms[band_key] = transform
                block.crs[band_key] = src.crs
                block.nodata[band_key] = nodata
                block.res[band_key] = abs(src.res[0])
        except Exception as exc:
            COUNTERS.classify_read_error(exc)
            logger.warning(f"block read failed band={band_key} scene={getattr(item,'id','?')}: "
                           f"{type(exc).__name__}: {exc}")
    return block


def release_block(block: SceneBlock) -> None:
    """Return a block's bytes to the process-wide budget and drop the arrays."""
    try:
        for key, n in list(block.nbytes.items()):
            RASTER_CACHE.release(n)
        block.nbytes.clear()
        block.bands.clear()
    except Exception:
        pass


def _geom_window(geom_proj, transform, shape) -> Optional[Tuple[int, int, int, int]]:
    """Pixel window of geom within an array, rounded outward exactly as rasterio crops.

    Returns None if the window is not ENTIRELY inside the array. Clipping it
    would silently give a smaller grid than read_band produces (different EPC,
    coverage, statistics); refusing sends the caller to the canonical read."""
    from rasterio.windows import from_bounds
    try:
        win = from_bounds(*geom_proj.bounds, transform=transform)
    except Exception:
        return None
    row_off = int(np.floor(win.row_off)); col_off = int(np.floor(win.col_off))
    row_end = int(np.ceil(win.row_off + win.height)); col_end = int(np.ceil(win.col_off + win.width))
    if row_off < 0 or col_off < 0 or row_end > shape[0] or col_end > shape[1]:
        COUNTERS.bump("block_window_outside")
        return None
    if row_end <= row_off or col_end <= col_off:
        return None
    return row_off, row_end, col_off, col_end


def subset_band(block: SceneBlock, band_key: str, geometry, reference=None,
                categorical: bool = False):
    """The read_band contract, served from the block. Returns None if unavailable."""
    if not block.has(band_key):
        return None
    COUNTERS.bump("block_subsets")
    from rasterio.transform import Affine
    from rasterio.features import geometry_mask

    data_big = block.bands[band_key]
    t_big = block.transforms[band_key]
    crs = block.crs[band_key]
    nodata = block.nodata[band_key]

    geom_proj = reproject_geometry(geometry, crs)
    pad_m = 0.0
    if categorical and CLOUD_DILATION_PX > 0:
        pad_m = CLOUD_DILATION_PX * 10.0 + max(block.res[band_key], 10.0)
    clip_geom = geom_proj.buffer(pad_m) if pad_m else geom_proj

    win = _geom_window(clip_geom, t_big, data_big.shape)
    if win is None:
        return None
    r0, r1, c0, c1 = win
    sub = data_big[r0:r1, c0:c1]
    transform = t_big * Affine.translation(c0, r0)

    # rio_mask sets everything outside the geometry to nodata; reproduce it.
    outside = geometry_mask([mapping(clip_geom)], out_shape=sub.shape,
                            transform=transform, all_touched=True, invert=False)
    sub = np.ma.masked_array(np.ma.getdata(sub).copy(),
                             mask=np.ma.getmaskarray(sub) | outside)

    # ---- from here on this is read_band verbatim ------------------------
    if reference is None:
        cov, cov_method = coverage_fractions(geom_proj, transform, sub.shape)
        if cov is None:
            cov = (~np.ma.getmaskarray(sub)).astype("float32")
            logger.warning(f"coverage fallback ({cov_method}) for {band_key}")
    else:
        cov = None

    inside = (cov > 0) if cov is not None else ~np.ma.getmaskarray(sub)
    inside &= (np.ma.getdata(sub) != nodata)
    if cov is not None:
        cov = np.where(inside, cov, 0.0).astype("float32")

    if categorical:
        arr = np.ma.filled(sub, 0).astype("int16")
        arr[~inside] = 0
        if CLOUD_DILATION_PX > 0:
            arr = dilate_scl(arr, native_res_m=block.res[band_key])
    else:
        arr = to_reflectance(sub, block.item, band_key)
        arr[~inside] = np.nan

    if reference is None:
        return arr, transform, crs, cov

    ref_shape, ref_transform, ref_crs, ref_coverage = reference
    ref_footprint = ref_coverage > 0
    if categorical:
        dst = np.zeros(ref_shape, dtype="int16")
        reproject(source=arr, destination=dst, src_transform=transform, src_crs=crs,
                  dst_transform=ref_transform, dst_crs=ref_crs,
                  src_nodata=0, dst_nodata=0, resampling=Resampling.nearest)
    else:
        dst = np.full(ref_shape, np.nan, dtype="float32")
        reproject(source=arr, destination=dst, src_transform=transform, src_crs=crs,
                  dst_transform=ref_transform, dst_crs=ref_crs,
                  src_nodata=np.nan, dst_nodata=np.nan, resampling=Resampling.bilinear)
        dst[~ref_footprint] = np.nan
    return dst, ref_transform, ref_crs, ref_coverage


def plan_blocks(lands: List[dict], geoms: Dict[str, object], max_span_m: float = 5000.0,
                max_lands_per_block: int = 200):
    """Assign lands to read-blocks in ONE pass - O(N), deterministic.

    Replaces a greedy seed-and-scan planner that was O(N^2) per tile (the
    release audit was right: ~2,000 lands per tile at 1M lands nationally
    means millions of pair checks per tile, in Python).

    Each land goes to the grid cell containing its centroid, on a local metric
    grid of side max_span_m. Two hard limits keep every block a bounded unit
    of work:
      * span  - a cell is max_span_m wide; a block's read window is the cell's
                lands' bounds, so it can exceed the cell only by the size of a
                parcel straddling the edge. A parcel LARGER than max_span_m
                gets a block of its own rather than inflating its neighbours.
      * work  - at most max_lands_per_block lands per block, so a dense village
                is split into several blocks instead of one straggler.
    Parcel statistics are never shared between lands: blocks only decide which
    lands share a raster READ.
    """
    if not lands:
        return []
    lat0 = float(np.mean([geoms[l["id"]].centroid.y for l in lands if l["id"] in geoms] or [0.0]))
    m_per_deg_x = 111320.0 * float(np.cos(np.radians(lat0)))
    m_per_deg_y = 110540.0
    cells: Dict[tuple, List[dict]] = {}
    own: List[dict] = []
    for land in lands:
        g = geoms.get(land["id"])
        if g is None:
            continue
        minx, miny, maxx, maxy = g.bounds
        if max((maxx - minx) * m_per_deg_x, (maxy - miny) * m_per_deg_y) > max_span_m:
            own.append(land)                       # oversized parcel: its own block
            continue
        c = g.centroid
        key = (int(np.floor(c.x * m_per_deg_x / max_span_m)),
               int(np.floor(c.y * m_per_deg_y / max_span_m)))
        cells.setdefault(key, []).append(land)

    def _bounds(members):
        bs = [geoms[m["id"]].bounds for m in members]
        return box(min(b[0] for b in bs), min(b[1] for b in bs),
                   max(b[2] for b in bs), max(b[3] for b in bs))

    blocks: List[Tuple[List[dict], object]] = []
    for key in sorted(cells):                     # deterministic order
        members = sorted(cells[key], key=lambda l: str(l["id"]))
        for i in range(0, len(members), max(int(max_lands_per_block), 1)):
            chunk = members[i:i + max_lands_per_block]
            blocks.append((chunk, _bounds(chunk)))
    for land in sorted(own, key=lambda l: str(l["id"])):
        blocks.append(([land], _bounds([land])))
    return blocks


def scenes_for_block(scenes: list, block_geom) -> list:
    """Only the scenes whose footprint actually intersects this block.

    A tile-level STAC search returns every scene touching ANY land in the tile;
    handing all of them to every block makes each block attempt reads on
    scenes that cannot cover it. Filter by footprint first. A scene without a
    usable footprint is kept (never drop data on missing metadata)."""
    from shapely.geometry import shape
    out = []
    for s in scenes or []:
        try:
            fp = shape(s.geometry) if getattr(s, "geometry", None) else None
        except Exception:
            fp = None
        if fp is None or fp.intersects(block_geom):
            out.append(s)
    return out


def geometry_fingerprint(geom) -> str:
    """Stable short hash of a land's measurement geometry.

    Part of the scene-ledger key: if a farmer redraws the boundary the
    fingerprint changes and every scene is re-evaluated for the new field.
    Coordinates are rounded to ~1 cm so a round-trip through the database
    cannot change the hash of an unchanged boundary."""
    import hashlib
    from shapely import wkb
    from shapely.geometry import shape as _shape, mapping as _mapping
    try:
        g = geom if hasattr(geom, "wkb") else _shape(geom)
        from shapely import set_precision
        g = set_precision(g, 1e-7)
        return hashlib.sha1(wkb.dumps(g, hex=False)).hexdigest()[:16]
    except Exception:
        return hashlib.sha1(repr(_mapping(geom) if hasattr(geom, "geom_type") else geom).encode()).hexdigest()[:16]
