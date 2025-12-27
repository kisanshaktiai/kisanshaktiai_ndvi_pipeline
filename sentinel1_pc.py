from datetime import datetime, timedelta

from pystac_client import Client
import planetary_computer as pc

from config import S1_LOOKBACK_DAYS, S1_COLLECTION


def fetch_s1_items(geometry):
    """
    Fetch Sentinel-1 GRD items (VV/VH) from Planetary Computer
    with extended lookback window for better data availability
    """

    catalog = Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=pc.sign_inplace,
    )

    end = datetime.utcnow()
    start = end - timedelta(days=S1_LOOKBACK_DAYS)

    search = catalog.search(
        collections=[S1_COLLECTION],
        intersects=geometry,
        datetime=f"{start.isoformat()}Z/{end.isoformat()}Z",
        query={
            "sar:polarizations": {"in": ["VV", "VH"]},
            "sar:instrument_mode": {"eq": "IW"},
        },
    )

    return list(search.get_items())
