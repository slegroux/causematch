"""Regenerate app/la_neighborhoods.csv: Los Angeles County's Countywide Statistical Areas
(CSAs) with a centroid each, the places a volunteer can pick by name.

Names are the colloquial ones: "Los Angeles - Silver Lake" is "Silver Lake", "City of Santa
Monica" is "Santa Monica", "Unincorporated - Altadena" is "Altadena". Two areas left with
the same name are told apart by "(Los Angeles)" or "(unincorporated)". Land areas only
(no piers, breakwaters or water). The centroid is the area-weighted center of the
polygon, generalized to about 50 m: enough for a travel-time estimate.

Source: County of Los Angeles Enterprise GIS (eGIS), "Countywide Statistical Areas (CSAs)",
under the eGIS Terms of Use: a license to copy, publish, distribute and adapt the data,
for commercial or personal use, without implying the County's endorsement.

Run: uv run python scripts/build_la_neighborhoods.py
"""

import csv
import json
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date
from itertools import pairwise
from pathlib import Path

LAYER = (
    "https://public.gis.lacounty.gov/public/rest/services/LACounty_Dynamic/"
    "Political_Boundaries/MapServer/23"
)
TERMS = "https://egis-lacounty.hub.arcgis.com/pages/terms-of-use"
OUT = Path(__file__).resolve().parent.parent / "app" / "la_neighborhoods.csv"
PREFIXES = {
    "City of ": "",
    "Los Angeles - ": " (Los Angeles)",
    "Unincorporated - ": " (unincorporated)",
}


def fetch():
    query = urllib.parse.urlencode(
        {
            "where": "Feat_Type = 'Land'",
            "outFields": "LABEL",
            "outSR": "4326",
            "maxAllowableOffset": "0.0005",
            "geometryPrecision": "6",
            "f": "json",
        }
    )
    with urllib.request.urlopen(f"{LAYER}/query?{query}", timeout=120) as r:
        found = json.load(r)
    if found.get("exceededTransferLimit"):
        raise SystemExit("the layer returned a partial page: page through it")
    return found["features"]


def centroid(rings):
    """Area-weighted centroid of ESRI rings (holes run the other way, so they subtract)."""
    area = cx = cy = 0.0
    for ring in rings:
        for (x0, y0), (x1, y1) in pairwise(ring):
            cross = x0 * y1 - x1 * y0
            area += cross
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross
    return cy / (3 * area), cx / (3 * area)  # (lat, lon)


def split(label):
    """(colloquial name, the suffix that tells it from a namesake)."""
    for prefix, suffix in PREFIXES.items():
        if label.startswith(prefix):
            return label[len(prefix) :].strip(), suffix
    return label.strip(), ""


def main():
    areas = {}  # label -> rings of every part with that label
    for f in fetch():
        areas.setdefault(f["attributes"]["LABEL"], []).extend(f["geometry"]["rings"])
    names = Counter(split(label)[0] for label in areas)
    rows = []
    for label, rings in areas.items():
        name, suffix = split(label)
        lat, lon = centroid(rings)
        rows.append((name + suffix if names[name] > 1 else name, lat, lon))
    rows.sort(key=lambda r: r[0].lower())
    with OUT.open("w", newline="") as f:
        f.write(
            "# Los Angeles County Countywide Statistical Areas (CSAs), with a centroid each.\n"
            f"# Source: County of Los Angeles eGIS, {LAYER}\n"
            f"# License: eGIS Terms of Use, {TERMS} (copy, publish, adapt; no endorsement)\n"
            f"# Fetched {date.today().isoformat()}: {len(rows)} land areas (the layer is "
            "live and unversioned)\n"
            "# Regenerate with scripts/build_la_neighborhoods.py\n"
        )
        out = csv.writer(f, lineterminator="\n")
        out.writerow(["name", "lat", "lon"])
        for name, lat, lon in rows:
            out.writerow([name, f"{lat:.4f}", f"{lon:.4f}"])
    print(f"{len(rows)} areas -> {OUT}")


if __name__ == "__main__":
    main()
