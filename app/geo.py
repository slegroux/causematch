import csv
import math
from functools import cache
from pathlib import Path


def _rows(filename):
    """The rows of a CSV file in app/, skipping its # comment lines."""
    lines = (ln for ln in (Path(__file__).parent / filename).open() if ln[0] != "#")
    return csv.DictReader(lines)


@cache
def _zips():
    """{zip: (lat, lon)} for every ZIP in app/la_zips.csv."""
    return {r["zip"]: (float(r["lat"]), float(r["lon"])) for r in _rows("la_zips.csv")}


def zip_latlon(zip_code):
    """(lat, lon) for a Los Angeles County ZIP, else None: where volunteers live and
    where openings and imported nonprofits may be."""
    return _zips().get(zip_code)


@cache
def _neighborhoods():
    return {
        r["name"].casefold(): (r["name"], (float(r["lat"]), float(r["lon"])))
        for r in _rows("la_neighborhoods.csv")
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


def shown_point(lat, lon):
    """A posted device location as a page may show it again: rounded, or nothing."""
    point = round_point(lat, lon) if lat or lon else None
    return (f"{point[0]:.2f}", f"{point[1]:.2f}") if point else ("", "")


# Names most people in Los Angeles know. The nearest center to a ZIP is often a small
# area (90034: Reynier Village); one of these within KNOWN_MARGIN_KM more reads better (Palms).
WELL_KNOWN = frozenset(
    ("Boyle Heights", "Burbank", "Chinatown", "Culver City", "Downtown", "Eagle Rock",
     "Echo Park", "Encino", "Glendale", "Hancock Park", "Highland Park", "Hollywood",
     "Inglewood", "Koreatown", "Leimert Park", "Little Tokyo", "Long Beach", "Los Feliz",
     "Mar Vista", "North Hollywood", "Palms", "Pasadena", "Pico-Union", "Playa Vista",
     "San Pedro", "Santa Monica", "Sherman Oaks", "Studio City", "Van Nuys", "Venice",
     "Watts", "West Adams", "West Los Angeles", "Westchester", "Westlake", "Westwood",
     "Wilmington")
)  # fmt: skip
KNOWN_MARGIN_KM = 1.0
KM_PER_DEGREE = 111.2


def zip_neighborhood(zip_code):
    """The listed area whose center is nearest a Los Angeles County ZIP's point, or a
    well-known one at most KNOWN_MARGIN_KM farther; None for an unknown ZIP."""
    point = zip_latlon(zip_code)
    if not point:
        return None
    lat, lon = point
    squeeze = math.cos(
        math.radians(lat)
    )  # a degree of longitude is shorter than one of latitude
    by_distance = sorted(
        (
            math.hypot(n[1][0] - lat, (n[1][1] - lon) * squeeze) * KM_PER_DEGREE,
            n[0],
        )
        for n in _neighborhoods().values()
    )
    nearest = by_distance[0][0]
    known = next(
        (
            name
            for km, name in by_distance
            if km <= nearest + KNOWN_MARGIN_KM and name in WELL_KNOWN
        ),
        None,
    )
    return known or by_distance[0][1]
