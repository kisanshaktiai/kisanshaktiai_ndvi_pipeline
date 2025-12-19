from datetime import datetime, timedelta

from pystac_client import Client
import planetary_computer as pc

from config import LOOKBACK_DAYS, S2_COLLECTION


def fetch_s2_items(geometry):
    """
    Fetch Sentinel-2 L2A items from Microsoft Planetary Computer
    for a given land geometry.
    """

    catalog = Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=pc.sign_inplace,
    )

    end = datetime.utcnow()
    start = end - timedelta(days=LOOKBACK_DAYS)

    search = catalog.search(
        collections=[S2_COLLECTION],
        intersects=geometry,
        datetime=f"{start.isoformat()}Z/{end.isoformat()}Z",
        query={
            "eo:cloud_cover": {"lt": 30}
        },
    )

    return list(search.get_items())
