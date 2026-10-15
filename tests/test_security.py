import pytest

from app.db import profiles
from tests.conftest import FORM

TRAVERSAL = [
    "/%2e%2e/main.py",
    "/%2e%2e/%2e%2e/etc/passwd.txt",
    "/..%2fmain.py",
    "/..%2f..%2fREADME.md",
    "/static/../main.py",
    "/static/%2e%2e/main.py",
    "/%2e%2e/pyproject.toml",
    "/app/la_zips.csv",
    "/main.py",
    "/README.md",
]
SECRETS = ("fast_app", "[project]", "root:", "Los Angeles County ZIP", "MIT License")


def get_raw(client, path):
    return client.get(path, follow_redirects=False)


@pytest.mark.parametrize("path", TRAVERSAL)
def test_no_file_outside_static_is_served_signed_out(anon, path):
    r = get_raw(anon, path)
    assert r.status_code in (303, 400, 403, 404)
    assert not any(m in r.text for m in SECRETS)


@pytest.mark.parametrize("path", TRAVERSAL)
def test_no_file_outside_static_is_served_signed_in(user, path):
    r = get_raw(user, path)
    assert r.status_code in (400, 403, 404)
    assert not any(m in r.text for m in SECRETS)


def test_static_dir_is_served(anon):
    assert get_raw(anon, "/static/.gitkeep").status_code == 200


def test_signed_out_post_onboarding_goes_to_login(anon):
    r = anon.post("/onboarding", data=FORM, follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_posted_role_and_coordinates_are_ignored(new_user):
    form = {
        **FORM,
        "role": "admin",
        "lat": "1.5",
        "lon": "2.5",
        "radius_km": "999",
        "id": "1",
    }
    new_user.post("/onboarding", data=form)
    (p,) = profiles(where="phone = ?", where_args=[new_user.phone])
    assert p.role == "volunteer"
    assert p.radius_km == 16
    assert p.lat != 1.5 and p.lon != 2.5


def test_volunteer_coordinates_never_reach_html(user):
    (p,) = profiles(where="phone = ?", where_args=[user.phone])
    for path in ("/deck", "/onboarding", "/"):
        r = user.get(path, follow_redirects=True)
        for v in (p.lat, p.lon):
            assert str(v) not in r.text
            assert f"{v:.2f}" not in r.text


@pytest.mark.parametrize(
    "field,limit",
    [("first_name", 80), ("neighborhood", 80), ("other_causes", 300), ("email", 254)],
)
def test_over_long_text_rejected(new_user, field, limit):
    value = "a" * (limit + 1)
    if field == "email":
        value = "a" * (limit - 5) + "@ex.com"  # valid shape, too long
    r = new_user.post(
        "/onboarding", data={**FORM, field: value}, follow_redirects=False
    )
    assert r.status_code == 200 and "too long" in r.text
    (p,) = profiles(where="phone = ?", where_args=[new_user.phone])
    assert not p.onboarded_at


def test_file_upload_in_text_field_is_400_not_500(new_user):
    r = new_user.post(
        "/onboarding",
        data={k: v for k, v in FORM.items() if k != "first_name"},
        files={"first_name": ("x.txt", b"hello")},
    )
    assert r.status_code == 400


def test_extra_onboarding_fields_are_stored(new_user):
    new_user.post(
        "/onboarding",
        data={
            **FORM,
            "skills": ["language"],
            "language_other": "Spanish",
            "volunteered_last_year": "yes",
        },
    )
    (p,) = profiles(where="phone = ?", where_args=[new_user.phone])
    assert p.language_other == "Spanish" and p.volunteered_last_year == "yes"


def test_bad_volunteered_value_rejected(new_user):
    r = new_user.post("/onboarding", data={**FORM, "volunteered_last_year": "maybe"})
    assert r.status_code == 200 and "wasn't recognized" in r.text


def test_la_zip_list_is_county_only():
    from app.geo import zip_latlon

    assert zip_latlon("90704")  # Avalon, Catalina Island
    assert zip_latlon("90012")
    for z in ("90620", "91320", "91709", "10001"):
        assert zip_latlon(z) is None
