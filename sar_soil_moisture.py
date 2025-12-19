import numpy as np

def to_db(arr):
    return 10 * np.log10(arr)

def soil_moisture(vv, vh):
    vv_db = to_db(vv)
    vh_db = to_db(vh)
    index = 0.6 * vv_db + 0.4 * (vv_db - vh_db)
    return float(np.nanmean(index))
