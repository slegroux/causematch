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


@cache
def _neighborhoods():
    lines = (
        ln
        for ln in (Path(__file__).parent / "la_neighborhoods.csv").open()
        if ln[0] != "#"
    )
    return {
        r["name"].casefold(): (r["name"], (float(r["lat"]), float(r["lon"])))
        for r in csv.DictReader(lines)
    }


def neighborhoods():
    """Every Countywide Statistical Area's name, A to Z (app/la_neighborhoods.csv)."""
    return [name for name, _ in _neighborhoods().values()]


def neighborhood_latlon(name):
    """(name as listed, (lat, lon)) for a Los Angeles County area, any case; else None."""
    return _neighborhoods().get(" ".join(name.split()).casefold())


# Los Angeles County's bounding box, islands included (San Clemente Island reaches 32.8),
# with a margin: a device location outside it is refused.
LA_BOX = (32.7, 34.9, -119.0, -117.6)  # lat min, lat max, lon min, lon max


def round_point(lat, lon):
    """A device location rounded to 2 decimals (about 1 km), or None when it isn't a point
    in Los Angeles County. Never store a finer one."""
    try:
        lat, lon = round(float(lat), 2), round(float(lon), 2)
    except (TypeError, ValueError, OverflowError):
        return None
    south, north, west, east = LA_BOX
    return (lat, lon) if south <= lat <= north and west <= lon <= east else None
