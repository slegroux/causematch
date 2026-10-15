"""A volunteer's profile details: name, pronouns, bio and interests, the name nonprofits
see, and the optional last name at onboarding."""

import json
import re

import pytest
from conftest import FORM
from test_photos import uid_of

from app import data
from app.choices import INTERESTS, MAX_INTERESTS
from app.db import profiles

GOOD = {
    "first_name": "Sam",
    "last_name": "Lee",
    "pronouns": "they/them",
    "bio": "Weekend hiker.\nI like sorting things.",
    "interests": ["hiking", "cooking"],
}


def save(client, **over):
    return client.post(
        "/me/profile/details", data={**GOOD, **over}, follow_redirects=False
    )


def test_the_profile_page_has_the_details_form(user):
    page = user.get("/me/profile").text
    for field in ('name="first_name"', 'name="last_name"', 'name="pronouns"'):
        assert field in page
    assert 'name="bio"' in page and 'maxlength="280"' in page
    assert page.count('name="interests"') == len(INTERESTS)
    assert '<datalist id="pronoun-suggestions">' in page


def test_details_are_saved_and_shown(user):
    r = save(user)
    assert (
        r.status_code == 303 and r.headers["location"] == "/me/profile?notice=details"
    )
    p = profiles[uid_of(user)]
    assert (p.display_name, p.last_name, p.pronouns) == ("Sam", "Lee", "they/them")
    assert p.bio == GOOD["bio"]
    assert json.loads(p.interests) == ["hiking", "cooking"]
    page = user.get("/me/profile?notice=details").text
    assert "Sam Lee" in page and "they/them" in page and "Profile saved." in page
    assert re.search(r'<input[^>]*value="hiking"[^>]*checked', page)
    assert not re.search(r'<input[^>]*value="music"[^>]*checked', page)


def test_optional_fields_can_be_cleared(user):
    save(user)
    save(user, last_name="", pronouns="", bio="", interests=[])
    p = profiles[uid_of(user)]
    assert (p.last_name, p.pronouns, p.bio) == (None, None, None)
    assert json.loads(p.interests) == []


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"first_name": ""}, "Enter your first name"),
        ({"first_name": "x" * 81}, "Enter your first name"),
        ({"last_name": "x" * 81}, "Keep your last name"),
        ({"pronouns": "x" * 31}, "Keep your pronouns"),
        ({"bio": "x" * 281}, "Keep your bio"),
        ({"interests": ["hiking", "skydiving"]}, "Pick up to 6 interests"),
        (
            {"interests": [v for v, _ in INTERESTS][: MAX_INTERESTS + 1]},
            "Pick up to 6 interests",
        ),
        ({"pronouns": "she\x00her"}, "wasn't recognized"),
    ],
)
def test_bad_details_are_refused_and_change_nothing(user, over, message):
    save(user)
    r = save(user, **over)
    assert r.status_code == 400 and message in r.text.replace("&#x27;", "'")
    p = profiles[uid_of(user)]
    assert (p.display_name, p.last_name, p.pronouns) == ("Sam", "Lee", "they/them")


def test_a_bio_of_exactly_280_and_six_interests_are_fine(user):
    six = [v for v, _ in INTERESTS][:MAX_INTERESTS]
    assert save(user, bio="x" * 280, interests=six).status_code == 303
    assert len(json.loads(profiles[uid_of(user)].interests)) == 6


def test_a_file_part_is_refused(user):
    r = user.post(
        "/me/profile/details",
        data={"first_name": "Sam"},
        files={"bio": ("b.txt", b"hi", "text/plain")},
    )
    assert r.status_code == 400


def test_the_me_hub_links_to_the_profile(user):
    assert 'href="/me/profile"' in user.get("/profile").text


# --- the name nonprofits see ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "last", "full", "shown"),
    [
        ("Sam", "Lee", False, "Sam L."),
        ("Sam", "Lee", True, "Sam Lee"),
        ("Sam", "", False, "Sam"),
        ("Sam", None, True, "Sam"),
        ("Sam", "  de la Cruz ", False, "Sam D."),
        ("Sam", "de la Cruz", True, "Sam de la Cruz"),
        ("", "Lee", False, "Volunteer L."),
    ],
)
def test_volunteer_name(first, last, full, shown):
    assert data.volunteer_name(first, last, full) == shown


# --- onboarding --------------------------------------------------------------------------


def test_onboarding_asks_for_an_optional_last_name(new_user):
    assert 'name="last_name"' in new_user.get("/onboarding").text
    r = new_user.post("/onboarding", data={**FORM, "last_name": "Lee"})
    assert r.status_code == 200  # followed to the deck
    assert profiles[uid_of(new_user)].last_name == "Lee"


def test_onboarding_without_a_last_name(new_user):
    new_user.post("/onboarding", data=FORM)
    assert profiles[uid_of(new_user)].last_name is None


def test_onboarding_refuses_a_long_last_name(new_user):
    r = new_user.post("/onboarding", data={**FORM, "last_name": "x" * 81})
    assert "too long" in r.text and not profiles[uid_of(new_user)].onboarded_at


def test_deleting_the_account_erases_the_details(user):
    save(user)
    uid = uid_of(user)
    user.post("/profile/delete")
    p = profiles[uid]
    assert (p.last_name, p.pronouns, p.bio, p.interests) == (None, None, None, None)
