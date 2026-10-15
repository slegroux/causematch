"""What the ProPublica import keeps, shared by scripts/import_orgs.py and the admin claims page.

The boundary is the Los Angeles County ZIP list (app/la_zips.csv), not the postal place
name: a nonprofit mailing from "Los Angeles" with a ZIP outside the county is out, and one
in Pasadena or Santa Monica is in. The NTEE code picks the cause.
"""

import re

from app import geo, propublica

# NTEE major letter -> cause; scripts/import_orgs.py documents the code ranges.
NTEE_CAUSES = {
    "K": "food-insecurity",
    "C": "environment-parks",
    "D": "animal-shelters",
    "L": "homelessness-services",
}
NTEE_GROUP_IDS = {"C": 3, "D": 3, "K": 5, "L": 5}  # the API's ntee[id] values
CAUSE_LETTER = {cause: letter for letter, cause in NTEE_CAUSES.items()}
CAUSE_TERMS = {
    "food-insecurity": ("food", "pantry", "meals"),
    "environment-parks": ("park", "garden", "environment", "conservation"),
    "animal-shelters": ("animal", "rescue", "humane"),
    "homelessness-services": ("homeless", "shelter", "housing"),
}
# Inclusive number ranges within a letter; None keeps the whole letter.
NTEE_RANGES = {"K": (30, 36), "C": (20, 99), "D": (20, 23), "L": None}
# Postal place names in Los Angeles County: its 88 cities, the City of Los Angeles'
# neighbourhoods and the unincorporated places mail goes to. They avoid a lookup for every
# hit in the state (the search results carry no ZIP). Only a prefilter: the ZIP list decides.
PLACE_NAMES = {
    name.lower()
    for name in (
        # the 88 cities
        "Agoura Hills", "Alhambra", "Arcadia", "Artesia", "Avalon", "Azusa",
        "Baldwin Park", "Bell", "Bell Gardens", "Bellflower", "Beverly Hills", "Bradbury",
        "Burbank", "Calabasas", "Carson", "Cerritos", "Claremont", "Commerce", "Compton",
        "Covina", "Cudahy", "Culver City", "Diamond Bar", "Downey", "Duarte", "El Monte",
        "El Segundo", "Gardena", "Glendale", "Glendora", "Hawaiian Gardens", "Hawthorne",
        "Hermosa Beach", "Hidden Hills", "Huntington Park", "Industry", "City of Industry",
        "Inglewood", "Irwindale", "La Canada Flintridge", "La Habra Heights", "La Mirada",
        "La Puente", "La Verne", "Lakewood", "Lancaster", "Lawndale", "Lomita",
        "Long Beach", "Los Angeles", "Lynwood", "Malibu", "Manhattan Beach", "Maywood",
        "Monrovia", "Montebello", "Monterey Park", "Norwalk", "Palmdale",
        "Palos Verdes Estates", "Paramount", "Pasadena", "Pico Rivera", "Pomona",
        "Rancho Palos Verdes", "Redondo Beach", "Rolling Hills", "Rolling Hills Estates",
        "Rosemead", "San Dimas", "San Fernando", "San Gabriel", "San Marino",
        "Santa Clarita", "Santa Fe Springs", "Santa Monica", "Sierra Madre", "Signal Hill",
        "South El Monte", "South Gate", "South Pasadena", "Temple City", "Torrance",
        "Vernon", "Walnut", "West Covina", "West Hollywood", "Westlake Village", "Whittier",
        # City of Los Angeles neighbourhoods with their own postal name
        "Arleta", "Canoga Park", "Chatsworth", "Encino", "Granada Hills", "Harbor City",
        "Hollywood", "Lake View Terrace", "Mission Hills", "North Hills",
        "North Hollywood", "Northridge", "Pacific Palisades", "Pacoima", "Panorama City",
        "Playa Del Rey", "Playa Vista", "Reseda", "San Pedro", "Sherman Oaks",
        "Studio City", "Sun Valley", "Sunland", "Sylmar", "Tarzana", "Toluca Lake",
        "Tujunga", "Valley Village", "Van Nuys", "Venice", "West Hills", "Wilmington",
        "Winnetka", "Woodland Hills",
        # unincorporated places and other postal names
        "Acton", "Agua Dulce", "Altadena", "Canyon Country", "Castaic", "East Los Angeles",
        "Florence", "Hacienda Heights", "Harbor Gateway", "Lennox", "West Carson",
        "Willowbrook",
        "La Canada", "La Crescenta", "Lake Hughes", "Littlerock", "Llano",
        "Marina Del Rey", "Montrose", "Newhall", "Pearblossom", "Rancho Dominguez",
        "Rowland Heights", "Saugus", "Stevenson Ranch", "Topanga", "Universal City",
        "Valencia", "Valyermo",
    )
}  # fmt: skip


class NotImportable(ValueError):
    """Why this nonprofit is not imported. Worded for an admin."""


def ntee_cause(code):
    """The cause an NTEE code maps to, else None.

    K30-K36 food banks, pantries, meal programs (not K03 societies, K20 agriculture, K40
    nutrition advocacy); C20-C99 environment (not the C01-C19 alliances and support
    groups); D20-D23 animal protection, humane societies and sanctuaries (not D40
    veterinary services, D30 wildlife or D50 zoos); every L code (housing and shelter).
    """
    m = re.fullmatch(r"([A-Z])(\d{2})\w*", code.upper()) if code else None
    cause = NTEE_CAUSES.get(m.group(1)) if m else None
    if not cause:
        return None
    limits = NTEE_RANGES[m.group(1)]
    return (
        cause if limits is None or limits[0] <= int(m.group(2)) <= limits[1] else None
    )


def classify(org, cause=None):
    """(cause, lat, lon) for a propublica.lookup() record, else NotImportable. `cause`
    overrides the NTEE mapping (an admin's choice); the ZIP check always applies."""
    place = geo.zip_latlon(org["zip"])
    if place is None:
        raise NotImportable(
            f"ZIP {org['zip'] or '(none)'} is not a Los Angeles County ZIP"
        )
    cause = cause or ntee_cause(org["ntee"])
    if cause is None:
        raise NotImportable(f"NTEE code {org['ntee'] or '(none)'} maps to no cause")
    return cause, *place


def import_ein(ein, client=None, dry_run=False, cause=None):
    """Look one EIN up and import it under the same rules as the bulk import: "created",
    "updated" or (dry run) "would import". NotImportable says why not; ValueError for a
    malformed EIN; LookupFailed when ProPublica can't be asked. Network first, then the
    one write, so the data lock is never held across a request."""
    org = propublica.lookup(ein, client)
    if org is None:
        raise NotImportable("ProPublica has no nonprofit with that EIN")
    cause, lat, lon = classify(org, cause)
    verb = "would import"
    if not dry_run:
        from app import data  # opening the database creates it: not in a dry run

        verb = data.upsert_imported_org(
            org["ein"],
            org["name"],
            org["city"],
            org["zip"],
            org["ntee"],
            cause,
            lat,
            lon,
        )
    return {"verb": verb, "cause": cause, **org}
