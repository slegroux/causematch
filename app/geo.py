import csv
from functools import cache
from pathlib import Path


@cache
def _zips():
    """{zip: (lat, lon)} for every ZIP in app/la_zips.csv. Its city column (mostly in the
    City of Los Angeles) stays in the file for metrics; nothing here gates on it."""
    lines = (
        ln for ln in (Path(__file__).parent / "la_zips.csv").open() if ln[0] != "#"
    )
    return {r["zip"]: (float(r["lat"]), float(r["lon"])) for r in csv.DictReader(lines)}


def zip_latlon(zip_code):
    """(lat, lon) for a Los Angeles County ZIP, else None: where volunteers live and
    where openings and imported nonprofits may be."""
    return _zips().get(zip_code)
