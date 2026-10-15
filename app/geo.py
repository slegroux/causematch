import csv
from functools import cache
from pathlib import Path


@cache
def _zips():
    lines = (
        ln for ln in (Path(__file__).parent / "la_zips.csv").open() if ln[0] != "#"
    )
    return {
        r["zip"]: ((float(r["lat"]), float(r["lon"])), r["city"] == "1")
        for r in csv.DictReader(lines)
    }


def zip_latlon(zip_code):
    """(lat, lon) for a Los Angeles County ZIP, else None."""
    found = _zips().get(zip_code)
    return found[0] if found else None


def city_zip_latlon(zip_code):
    """(lat, lon) for a ZIP mostly inside the City of Los Angeles, else None."""
    found = _zips().get(zip_code)
    return found[0] if found and found[1] else None
