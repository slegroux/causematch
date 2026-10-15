"""scripts/import_orgs.py against recorded ProPublica responses; the network is never used."""

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app import data, geo, propublica
from app import db as _db
from app.db import causes, org_members, orgs

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "propublica"

spec = importlib.util.spec_from_file_location(
    "import_orgs", ROOT / "scripts" / "import_orgs.py"
)
import_orgs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(import_orgs)

SEARCH = json.loads((FIXTURES / "search_food_bank.json").read_text())
ORG = json.loads((FIXTURES / "organization_861620575.json").read_text())
NOT_FOUND = json.loads((FIXTURES / "organization_not_found.json").read_text())
ARGS = ["--city", "Los Angeles", "--state", "CA"]


def detail(ein, name, city="Los Angeles", zipcode="90004-3544", ntee="K30", state="CA"):
    """The recorded organization response with a different nonprofit's fields."""
    body = copy.deepcopy(ORG)
    body["organization"].update(
        ein=int(ein),
        name=name,
        city=city,
        state=state,
        zipcode=zipcode,
        ntee_code=ntee,
    )
    return body


def hit(ein, name, city="Los Angeles", ntee="K31"):
    return {"ein": int(ein), "name": name, "city": city, "ntee_code": ntee}


# The recorded page: two Los Angeles food banks (one K31, one K30), a Sunland one under a
# Los Angeles name, Woodland, an unmapped P20 and a Northridge one.
DETAILS = {
    "861620575": ORG,
    "921003084": detail("921003084", "Community Space Food Bank"),
}


class Api:
    """The ProPublica API as MockTransport sees it: the same hits for every search query."""

    def __init__(self, hits=None, details=None, num_pages=1):
        self.hits = SEARCH["organizations"] if hits is None else hits
        self.details = DETAILS if details is None else details
        self.num_pages = num_pages
        self.searches, self.lookups, self.headers = [], [], []

    def __call__(self, request):
        self.headers.append(request.headers.get("user-agent"))
        if request.url.path.endswith("/search.json"):
            q = dict(request.url.params)
            self.searches.append(q)
            body = {**SEARCH, "organizations": self.hits, "num_pages": self.num_pages}
            return httpx.Response(200, json=body)
        ein = request.url.path.rsplit("/", 1)[1].removesuffix(".json")
        self.lookups.append(ein)
        found = self.details.get(ein)
        if isinstance(found, int):
            return httpx.Response(found)
        return (
            httpx.Response(200, json=found)
            if found
            else httpx.Response(404, json=NOT_FOUND)
        )


def run(api, *argv, capsys):
    """(exit code, stdout lines, stderr) of the script, with no real pause."""
    sleeps = []
    client = httpx.Client(transport=httpx.MockTransport(api))
    code = import_orgs.main([*ARGS, *argv], client=client, sleep=sleeps.append)
    out = capsys.readouterr()
    run.sleeps = sleeps
    return code, out.out.strip().splitlines(), out.err


FOOD = ["--cause", "food-insecurity"]


def snapshot():
    """Every row of every table, and the connection's count of rows ever changed."""
    conn = _db.db.conn
    tables = [
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    rows = {t: list(conn.execute(f'SELECT * FROM "{t}"')) for t in tables}
    return rows, conn.totalchanges()


def test_the_ntee_mapping_is_the_documented_one():
    assert import_orgs.NTEE_CAUSES == {
        "K": "food-insecurity",
        "C": "environment-parks",
        "D": "animal-shelters",
        "L": "homelessness-services",
    }
    for letter, cause in import_orgs.NTEE_CAUSES.items():
        assert f"{letter}  " in import_orgs.__doc__ and cause in import_orgs.__doc__
    assert set(import_orgs.NTEE_CAUSES) == set(import_orgs.NTEE_GROUP_IDS)


def test_every_mapped_cause_is_a_cause_row():
    assert set(import_orgs.NTEE_CAUSES.values()) <= {c.slug for c in causes()}


def test_dry_run_prints_the_orgs_and_writes_nothing(capsys):
    before = snapshot()
    code, lines, _ = run(Api(), "--dry-run", *FOOD, capsys=capsys)
    after = snapshot()
    assert code == 0 and after == before
    assert lines[-1] == "2 orgs"
    assert sum(ln.startswith("would import:") for ln in lines) == 2


def test_a_dry_run_never_opens_the_database(tmp_path):
    code = (
        "import importlib.util, sys, httpx\n"
        "spec = importlib.util.spec_from_file_location('io_', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "empty = {'organizations': [], 'num_pages': 0}\n"
        "c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=empty)))\n"
        "m.main(['--city', 'Los Angeles', '--state', 'CA', '--dry-run'], client=c, sleep=lambda s: None)\n"
        "assert 'app.data' not in sys.modules and 'app.db' not in sys.modules\n"
    )
    db = tmp_path / "x" / "never.db"
    r = subprocess.run(
        [sys.executable, "-c", code, str(ROOT / "scripts" / "import_orgs.py")],
        cwd=ROOT,
        env={"CAUSEMATCH_DB": str(db), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "0 orgs"
    assert not db.exists()


def test_import_creates_unverified_unclaimed_cold_orgs(capsys):
    code, lines, _ = run(Api(), *FOOD, capsys=capsys)
    assert code == 0 and lines[-1] == "2 orgs"
    org = orgs(where="ein = ?", where_args=["861620575"])[0]
    assert (org.name, org.city, org.zip, org.ntee_code) == (
        "Los Angeles Food Bank",
        "Los Angeles",
        "90019",
        "K31",
    )
    assert json.loads(org.causes) == ["food-insecurity"]
    assert (org.lat, org.lon) == geo.city_zip_latlon("90019")
    assert not org.verified_at and org.intro == "cold" and not org.deactivated_at
    assert not org_members(where="org_id = ?", where_args=[org.id])


def test_a_second_run_updates_by_ein_and_keeps_an_admins_edits(capsys):
    run(Api(), *FOOD, capsys=capsys)
    n = orgs.count
    org = orgs(where="ein = ?", where_args=["861620575"])[0]
    orgs.update(
        id=org.id,
        intro="warm",
        about="Our story",
        verified_at="2026-10-15T00:00:00+00:00",
        causes=json.dumps(["environment-parks"]),
        name="Old name",
    )
    _, lines, _ = run(Api(), *FOOD, capsys=capsys)
    assert lines[-1] == "2 orgs" and orgs.count == n
    assert sum(ln.startswith("updated:") for ln in lines) == 2
    after = orgs[org.id]
    assert after.name == "Los Angeles Food Bank"  # the ProPublica record is refreshed
    assert (after.intro, after.about, after.causes) == (
        "warm",
        "Our story",
        '["environment-parks"]',
    )
    assert after.verified_at == "2026-10-15T00:00:00+00:00"


def test_a_search_hit_seen_under_every_query_is_one_org(capsys):
    api = Api()
    run(api, *FOOD, capsys=capsys)
    assert len(api.searches) == 4  # the city's name and three food words
    assert sorted(api.lookups) == ["861620575", "921003084"]


def test_only_this_city_and_a_city_zip_are_kept(capsys):
    hits = [
        *SEARCH["organizations"],
        hit(
            "111111111", "County Pantry"
        ),  # Los Angeles in search, ZIP outside the city
        hit("222222222", "Moved Pantry"),  # the lookup says it is in Pasadena now
        hit("333333333", "Texas Pantry"),  # same city name, other state
        hit("444444444", "No Code Pantry", ntee=""),
        hit("555555555", "Wrong Group Pantry", ntee="P20"),
        hit("666666666", "Shelter", ntee="L41"),  # another cause's letter
    ]
    details = {
        **DETAILS,
        "111111111": detail("111111111", "County Pantry", zipcode="90704"),
        "222222222": detail("222222222", "Moved Pantry", city="Pasadena"),
        "333333333": detail("333333333", "Texas Pantry", state="TX"),
    }
    api = Api(hits, details)
    _, lines, _ = run(api, *FOOD, "--dry-run", capsys=capsys)
    assert lines[-1] == "2 orgs"
    # Sunland, Woodland, Northridge, no code, P20 and L41 never cost a lookup
    assert sorted(set(api.lookups)) == [
        "111111111",
        "222222222",
        "333333333",
        "861620575",
        "921003084",
    ]


def test_the_city_must_be_exact_not_a_neighbourhood(capsys):
    api = Api([hit("931989260", "Glendale Food Bank", city="Sunland")])
    _, lines, _ = run(api, *FOOD, "--dry-run", capsys=capsys)
    assert lines[-1] == "0 orgs" and api.lookups == []


@pytest.mark.parametrize(
    "cause,letter",
    [
        ("environment-parks", "C"),
        ("animal-shelters", "D"),
        ("homelessness-services", "L"),
        ("food-insecurity", "K"),
    ],
)
def test_each_ntee_letter_goes_to_its_cause(capsys, cause, letter):
    ein = "777777777"
    api = Api(
        [
            hit(ein, "Some Org", ntee=f"{letter}20"),
            hit("888888888", "Other", ntee="Z99"),
        ],
        {ein: detail(ein, "Some Org", zipcode="90012", ntee=f"{letter}20")},
    )
    code, lines, _ = run(api, "--cause", cause, capsys=capsys)
    assert code == 0 and lines[-1] == "1 orgs"
    assert json.loads(orgs(where="ein = ?", where_args=[ein])[0].causes) == [cause]
    assert {s["ntee[id]"] for s in api.searches} == {
        str(import_orgs.NTEE_GROUP_IDS[letter])
    }


def test_malformed_records_are_skipped_and_the_run_goes_on(capsys):
    hits = [
        {"ein": "abc", "name": "Letters"},
        {"ein": None, "name": "No number"},
        {"ein": True, "name": "A boolean"},
        {"name": "No EIN at all", "city": "Los Angeles", "ntee_code": "K31"},
        {"ein": 123456789012, "name": "Too long", "city": "Los Angeles"},
        {"ein": 232323232, "name": "", "city": "Los Angeles"},
        "not an object",
        {"ein": 999999991, "name": "Bad city", "city": 7, "ntee_code": "K31"},
        hit("999999992", "Server Error"),
        hit("999999993", "Garbage Body"),
        hit("861620575", "Los Angeles Food Bank"),
    ]
    details = {
        "999999992": 500,
        "999999993": {"nope": 1},
        "861620575": ORG,
    }
    code, lines, err = run(Api(hits, details), *FOOD, capsys=capsys)
    assert code == 0 and lines[-1] == "1 orgs"
    assert [o.ein for o in orgs() if o.ein] == ["861620575"]
    assert "skipped" in lines[-2] and "2 errors" in lines[-2]
    assert "999999992" in err and "999999993" in err


def test_a_lookup_that_loses_a_leading_zero_asks_for_nine_digits(capsys):
    api = Api(
        [hit("12345678", "Old Pantry")],  # the API sends the integer 12345678
        {"012345678": detail("012345678", "Old Pantry", zipcode="90012")},
    )
    run(api, *FOOD, capsys=capsys)
    assert api.lookups == ["012345678"]
    assert orgs(where="ein = ?", where_args=["012345678"])


def test_paging_stops_at_the_cap(capsys):
    api = Api([], num_pages=100)
    code, lines, _ = run(api, *FOOD, "--max-pages", "3", capsys=capsys)
    assert code == 0 and lines[-1] == "0 orgs"
    assert len(api.searches) == 4 * 3
    assert {s["page"] for s in api.searches} == {"0", "1", "2"}


def test_paging_stops_at_the_last_page(capsys):
    api = Api([], num_pages=2)
    run(api, *FOOD, "--max-pages", "9", capsys=capsys)
    assert len(api.searches) == 4 * 2


def test_the_page_cap_has_a_ceiling(capsys):
    with pytest.raises(SystemExit):
        import_orgs.main([*ARGS, "--max-pages", "21"])
    assert "--max-pages" in capsys.readouterr().err


def test_limit_stops_the_run_early(capsys):
    api, before = Api(), orgs.count
    _, lines, _ = run(api, *FOOD, "--limit", "1", capsys=capsys)
    assert lines[-1] == "1 orgs" and api.lookups == ["861620575"]
    assert orgs.count == before + 1


def test_every_request_is_preceded_by_a_pause(capsys):
    api = Api()
    run(api, *FOOD, "--delay", "0.5", capsys=capsys)
    assert len(run.sleeps) == len(api.searches) + len(api.lookups)
    assert set(run.sleeps) == {0.5}


def test_the_user_agent_names_the_project():
    client = propublica.make_client()
    assert client.headers["user-agent"] == propublica.USER_AGENT
    assert propublica.USER_AGENT.startswith("causematch")
    client.close()


def test_a_failing_search_is_reported_and_the_exit_code_says_so(capsys):
    api = lambda request: httpx.Response(500)
    code, lines, err = run(api, *FOOD, capsys=capsys)
    assert code == 1 and lines[-1] == "0 orgs" and "search" in err


def test_other_cities_and_unknown_causes_are_refused():
    with pytest.raises(SystemExit):
        import_orgs.main(["--city", "Pasadena", "--state", "CA"])
    with pytest.raises(SystemExit):
        import_orgs.main([*ARGS, "--cause", "nope"])


def test_the_import_function_checks_what_it_is_given():
    place = (34.0, -118.0)
    with pytest.raises(ValueError):
        data.upsert_imported_org(
            "12", "X", "Los Angeles", "90012", "K31", "food-insecurity", *place
        )
    with pytest.raises(ValueError):
        data.upsert_imported_org(
            "123456789", "X", "Los Angeles", "90012", "K31", "no-such-cause", *place
        )
    with pytest.raises(ValueError):
        data.upsert_imported_org(
            "123456789", " ", "Los Angeles", "90012", "K31", "food-insecurity", *place
        )
    assert not orgs(where="ein = ?", where_args=["123456789"])
