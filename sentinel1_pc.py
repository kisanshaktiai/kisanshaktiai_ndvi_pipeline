from datetime import datetime, timedelta

from pystac_client import Client
import planetary_computer as pc

from config import LOOKBACK_DAYS, S1_COLLECTION


def fetch_s1_items(geometry):
    """
    Fetch Sentinel-1 GRD items (VV/VH) from Planetary Computer
    """

    catalog = Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=pc.sign_inplace,
    )

    end = datetime.utcnow()
    start = end - timedelta(days=LOOKBACK_DAYS)

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
