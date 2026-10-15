"""Regenerate app/la_zips.csv: ZIP code areas (ZCTAs) in Los Angeles County with their internal points.

A ZCTA counts as LA County when LA County holds the largest share of its land area.
Sources (US Census Bureau, public domain):
  2023 Gazetteer ZCTA file (internal points)
  2020 ZCTA-to-county relationship file (land area per county)

Run: uv run python scripts/build_la_zips.py
"""

import csv
import io
import urllib.request
import zipfile
from pathlib import Path

GAZETTEER = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_zcta_national.zip"
RELATIONSHIP = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"
LA_COUNTY = "06037"
OUT = Path(__file__).resolve().parent.parent / "app" / "la_zips.csv"


def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def main():
    with zipfile.ZipFile(io.BytesIO(fetch(GAZETTEER))) as z:
        text = z.read(z.namelist()[0]).decode()
    points = {}
    for r in csv.DictReader(io.StringIO(text), delimiter="\t"):
        r = {k.strip(): v.strip() for k, v in r.items()}
        points[r["GEOID"]] = (r["INTPTLAT"], r["INTPTLONG"])

    best = {}  # zcta -> (land area, county)
    rel = csv.DictReader(
        io.StringIO(fetch(RELATIONSHIP).decode("utf-8-sig")), delimiter="|"
    )
    for r in rel:
        z, county = r["GEOID_ZCTA5_20"], r["GEOID_COUNTY_20"]
        if not z or not county:
            continue
        land = int(r["AREALAND_PART"] or 0)
        if z not in best or land > best[z][0]:
            best[z] = (land, county)

    rows = sorted(
        (z, *points[z]) for z, (_, c) in best.items() if c == LA_COUNTY and z in points
    )
    with OUT.open("w") as f:
        f.write(
            "# Los Angeles County ZIP code areas (ZCTAs) with Census internal points. Public domain.\n"
            f"# Points: {GAZETTEER}\n"
            f"# County (largest land share = FIPS {LA_COUNTY}): {RELATIONSHIP}\n"
            "# Regenerate with scripts/build_la_zips.py\n"
            "zip,lat,lon\n"
        )
        for z, lat, lon in rows:
            f.write(f"{z},{lat},{lon}\n")
    print(f"{len(rows)} ZIPs -> {OUT}")


if __name__ == "__main__":
    main()
