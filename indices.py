"""
indices.py - Sentinel-2 spectral index computation.

SCIENTIFIC BASIS. Every formula below is a published, citable index. No
threshold and no coefficient is invented here; the pipeline computes physics
and stores it. All agronomic interpretation happens later, in the Decision
Brain, from the values this module produces.

    NDVI   Rouse et al. 1974        (NIR - Red)   / (NIR + Red)
    SAVI   Huete 1988               1.5(NIR-Red)  / (NIR+Red+0.5)
    EVI    Huete et al. 2002        2.5(NIR-Red)  / (NIR+6Red-7.5Blue+1)
    NDRE   Barnes et al. 2000       (NIR - RE705) / (NIR + RE705)
    MCARI  Daughtry et al. 2000     [(RE-R)-0.2(RE-G)] x (RE/R)
    NDMI   Gao 1996                 (NIR - SWIR)  / (NIR + SWIR)
    NDWI   McFeeters 1996           (Green - NIR) / (Green + NIR)
    MNDWI  Xu 2006                  (Green - SWIR)/ (Green + SWIR)

Sentinel-2 band mapping (all ESA-documented centre wavelengths):
    B02 Blue 490nm | B03 Green 560nm | B04 Red 665nm
    B05 RedEdge1 705nm | B08 NIR 842nm | B11 SWIR1 1610nm

INPUTS MUST BE SURFACE REFLECTANCE in [0,1]. raster_utils.to_reflectance()
applies scale and BOA_ADD_OFFSET from STAC metadata rather than guessing from
pixel statistics - the offset (-1000 for processing baseline >= 04.00) breaks
the scale-invariance that normalised differences would otherwise enjoy.

THE THREE WATER INDICES ARE NOT INTERCHANGEABLE. This is the single most
consequential distinction in this file:
    NDMI  -> CANOPY water content. The plant water-stress index.
    NDWI  -> surface WATER BODY delineation (McFeeters).
    MNDWI -> water vs built-up discrimination (Xu).
v1 stored McFeeters NDWI and alerted on it as crop water stress with an
inverted threshold. Over vegetation McFeeters NDWI is strongly negative and
becomes MORE negative as the canopy improves, so the alert fired hardest on
the healthiest fields - 55.3% of all rows.
"""

import numpy as np
from config import EPS


# ---------------------------------------------------------------------------
# PHYSICAL VALIDITY RANGES
# Mathematical bounds of each formulation, not agronomic thresholds.
# A value outside these indicates a computation or reflectance fault.
# ---------------------------------------------------------------------------
INDEX_BOUNDS = {
    "NDVI":  (-1.0, 1.0),
    "SAVI":  (-1.5, 1.5),
    "EVI":   (-1.0, 2.5),
    "NDRE":  (-1.0, 1.0),
    "MCARI": (-1.0, 5.0),
    "NDMI":  (-1.0, 1.0),
    "NDWI":  (-1.0, 1.0),
    "MNDWI": (-1.0, 1.0),
}

# Which Sentinel-2 bands each index requires. Used to skip cleanly rather
# than emit a wrong number when a band is unavailable.
INDEX_BANDS = {
    "NDVI":  ("B08", "B04"),
    "SAVI":  ("B08", "B04"),
    "EVI":   ("B08", "B04", "B02"),
    "NDRE":  ("B08", "B05"),
    "MCARI": ("B05", "B04", "B03"),
    "NDMI":  ("B08", "B11"),
    "NDWI":  ("B03", "B08"),
    "MNDWI": ("B03", "B11"),
}


def _nd(a, b):
    """Normalised difference with a zero-denominator guard."""
    return (a - b) / (a + b + EPS)


def _available(bands: dict, name: str) -> bool:
    return all(k in bands and bands[k] is not None for k in INDEX_BANDS[name])


def compute_indices(b: dict) -> dict:
    """
    Purpose
        Compute every spectral index the supplied bands allow.

    Inputs
        b : dict of float32 surface-reflectance arrays on a common 10 m grid.
            Keys B02 B03 B04 B05 B08 B11 (subset permitted).

    Outputs
        dict name -> float32 array, same shape, NaN outside valid pixels.
        Only indices whose required bands are present are returned.

    Scientific reasoning
        Each formula is cited above. Indices are computed on the SAME masked
        pixel set so every statistic downstream describes an identical
        population - otherwise NDVI and NDMI would describe different subsets
        of the field and could not be compared.

    Performance
        Vectorised numpy over a windowed field clip (typically 10^2-10^4 px).
        Negligible against network I/O.

    Error handling
        Missing bands skip that index rather than substituting a value.
        Division guarded by EPS. Non-finite results become NaN via the shared
        validity mask.
    """
    for k, v in b.items():
        if v is not None and not isinstance(v, np.ndarray):
            raise TypeError(f"Band {k} is not an ndarray: {type(v)}")

    out = {}

    # -- Canopy greenness / structure -------------------------------------
    if _available(b, "NDVI"):
        out["NDVI"] = _nd(b["B08"], b["B04"])

    if _available(b, "SAVI"):
        # L=0.5 soil-adjustment. Materially better than NDVI below ~40%
        # canopy cover - i.e. the emergence and establishment window where
        # absolute NDVI thresholds misclassify healthy crops.
        out["SAVI"] = 1.5 * (b["B08"] - b["B04"]) / (b["B08"] + b["B04"] + 0.5 + EPS)

    if _available(b, "EVI"):
        # Resistant to soil and aerosol effects; does not saturate at high
        # LAI the way NDVI does at canopy closure, when yield is being set.
        out["EVI"] = 2.5 * (b["B08"] - b["B04"]) / (
            b["B08"] + 6.0 * b["B04"] - 7.5 * b["B02"] + 1.0 + EPS)

    # -- Chlorophyll / nitrogen -------------------------------------------
    if _available(b, "NDRE"):
        # Red edge stays sensitive after NDVI saturates; the better N proxy
        # post canopy closure.
        out["NDRE"] = _nd(b["B08"], b["B05"])

    if _available(b, "MCARI"):
        red_edge_red = b["B05"] - b["B04"]
        red_edge_green = b["B05"] - b["B03"]
        b04_safe = np.where(b["B04"] > 0.01, b["B04"], np.nan)
        out["MCARI"] = np.clip(
            (red_edge_red - 0.2 * red_edge_green) * (b["B05"] / b04_safe), -1.0, 5.0)

    # -- Water: three DIFFERENT indices, never interchangeable ------------
    if _available(b, "NDMI"):
        # Gao 1996. CANOPY WATER CONTENT - the plant water-stress index.
        out["NDMI"] = _nd(b["B08"], b["B11"])

    if _available(b, "NDWI"):
        # McFeeters 1996. WATER BODY delineation ONLY.
        out["NDWI"] = _nd(b["B03"], b["B08"])

    if _available(b, "MNDWI"):
        # Xu 2006. Water vs built-up discrimination.
        out["MNDWI"] = _nd(b["B03"], b["B11"])

    # -- Shared validity mask ---------------------------------------------
    # Every index inherits the same finite-pixel set, so all statistics
    # downstream describe one identical population.
    if out:
        base = out.get("NDVI")
        valid = np.isfinite(base) if base is not None else None
        for k in list(out):
            arr = out[k]
            m = np.isfinite(arr) if valid is None else (valid & np.isfinite(arr))
            out[k] = np.where(m, arr, np.nan)

    return out


def validate_index(name: str, value: float) -> tuple:
    """
    Purpose
        Physical-plausibility gate applied before persistence.

    Returns
        (is_valid: bool, reason: str|None)

    Scientific reasoning
        Bounds are the mathematical range of each formulation, not agronomic
        thresholds. Rejecting here prevents a computation fault from reaching
        the Decision Brain wearing a plausible-looking number.
    """
    if value is None:
        return False, "null"
    v = float(value)
    if not np.isfinite(v):
        return False, "non_finite"
    lo, hi = INDEX_BOUNDS.get(name, (-1e9, 1e9))
    if not (lo <= v <= hi):
        return False, f"out_of_range[{lo},{hi}]"
    return True, None


def index_statistics(arr: np.ndarray) -> dict:
    """
    Purpose
        Full deterministic statistic set over the valid pixels of one index
        on ONE acquisition. Every value is spatial; no temporal aggregation
        occurs anywhere in this pipeline.

    Outputs
        mean, median, min, max, std, p10, p90, cv, count.

    Scientific reasoning
        p10/p90 and cv describe within-field heterogeneity, which is the
        strongest discriminator NDVI offers a diagnosis:
            uniform low  -> whole-field cause (nutrient, water)
            patchy  low  -> localised cause (pest, disease, soil)
        Percentiles are preferred over min/max for robustness: a single
        cloud-edge pixel moves the extremes but not the deciles.

    Determinism
        numpy nan-aware reductions over a fixed pixel set. Identical imagery
        yields bit-identical output. No sampling, no randomness.
    """
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return {}
    mean = float(np.mean(finite))
    std = float(np.std(finite))
    return {
        "mean":   mean,
        "median": float(np.median(finite)),
        "min":    float(np.min(finite)),
        "max":    float(np.max(finite)),
        "std":    std,
        "p10":    float(np.percentile(finite, 10)),
        "p90":    float(np.percentile(finite, 90)),
        "cv":     float(std / (abs(mean) + 1e-6)),
        "count":  int(finite.size),
    }
