import numpy as np

def compute_indices(b):
    for k, v in b.items():
        if not isinstance(v, np.ndarray):
            raise TypeError(f"Band {k} is not ndarray: {type(v)}")

    ndvi = (b["B08"] - b["B04"]) / (b["B08"] + b["B04"] + 1e-6)
    ndre = (b["B08"] - b["B05"]) / (b["B08"] + b["B05"] + 1e-6)
    ndwi = (b["B03"] - b["B08"]) / (b["B03"] + b["B08"] + 1e-6)

    return {
        "NDVI": ndvi,
        "NDRE": ndre,
        "NDWI": ndwi,
    }
