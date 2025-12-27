import numpy as np

def compute_indices(b):
    for k, v in b.items():
        if not isinstance(v, np.ndarray):
            raise TypeError(f"Band {k} is not ndarray: {type(v)}")

    # NDVI - Normalized Difference Vegetation Index
    ndvi = (b["B08"] - b["B04"]) / (b["B08"] + b["B04"] + 1e-6)
    
    # NDRE - Normalized Difference Red Edge (Nitrogen stress)
    ndre = (b["B08"] - b["B05"]) / (b["B08"] + b["B05"] + 1e-6)
    
    # NDWI - Normalized Difference Water Index
    ndwi = (b["B03"] - b["B08"]) / (b["B03"] + b["B08"] + 1e-6)
    
    # MCARI - Modified Chlorophyll Absorption Ratio Index
    # More sensitive to chlorophyll content and early stress detection
    # Better for sparse canopies and nitrogen management
    mcari = ((b["B05"] - b["B04"]) - 0.2 * (b["B05"] - b["B03"])) * (b["B05"] / (b["B04"] + 1e-6))

    return {
        "NDVI": ndvi,
        "NDRE": ndre,
        "NDWI": ndwi,
        "MCARI": mcari,
    }
