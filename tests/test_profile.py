"""A volunteer's profile details: name, pronouns, bio and interests, the name nonprofits
see, and the optional last name at onboarding."""

import json
import re

import pytest
from conftest import FORM, uid_of

from app import data
from app.choices import INTERESTS, MAX_INTERESTS
from app.db import profiles
from app.forms import HIDDEN_CHARS_MESSAGE

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
    assert "before the change can't go on a certificate" in page.replace("&#x27;", "'")


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
        ({"pronouns": "she\x00her"}, "hidden or special characters"),
        (
            {"last_name": "Le\u200be"},
            "hidden or special characters",
        ),  # zero-width space
        (
            {"bio": "Hi \u202eereht"},
            "hidden or special characters",
        ),  # right-to-left override
        ({"first_name": "Sam\ufeff"}, "hidden or special characters"),
        ({"pronouns": "they\tthem"}, "hidden or special characters"),
        ({"last_name": "Lee\u3164"}, "hidden or special characters"),  # Hangul filler
        ({"last_name": "\u2800"}, "hidden or special characters"),  # braille blank
        ({"bio": "hi\U000e0041"}, "hidden or special characters"),  # a tag character
        (
            {"bio": "e\u0301\u0302\u0303\u0304"},
            "hidden or special characters",
        ),  # 4 marks
        (
            {"pronouns": "they\u2028them"},
            "hidden or special characters",
        ),  # line separator
        ({"bio": "private \ue000"}, "hidden or special characters"),  # private use
        (
            {"last_name": "\u0301"},
            "hidden or special characters",
        ),  # a mark with nothing under it
        ({"last_name": "Le\u034fe"}, "hidden or special characters"),  # grapheme joiner
        (
            {"last_name": "Lee\ufe00"},
            "hidden or special characters",
        ),  # variation selector
        (
            {"last_name": "Lee\ufe0f"},
            "hidden or special characters",
        ),  # emoji style, no emoji
        (
            {"bio": "e\u0301\u0302\u200d\u0303\u0304"},
            "hidden or special characters",
        ),  # ZWJ
    ],
)
def test_bad_details_are_refused_and_change_nothing(user, over, message):
    save(user)
    r = save(user, **over)
    assert r.status_code == 400 and message in r.text.replace("&#x27;", "'")
    p = profiles[uid_of(user)]
    assert (p.display_name, p.last_name, p.pronouns) == ("Sam", "Lee", "they/them")


@pytest.mark.parametrize(
    "field, value",
    [
        ("email", "sam\u200b@example.com"),  # zero-width space
        ("other_causes", "owls \u202eslwo"),  # right-to-left override
        ("neighborhood", "Palms\ufeff"),  # zero-width no-break space
    ],
)
def test_hidden_characters_are_refused_in_the_fields_moved_from_onboarding(
    user, field, value
):
    save(user, email="sam@example.com", other_causes="owls", neighborhood="Palms")
    r = save(user, **{field: value})
    assert r.status_code == 400 and "hidden or special characters" in r.text
    p = profiles[uid_of(user)]
    assert (p.email, p.other_causes, p.neighborhood) == (
        "sam@example.com",
        "owls",
        "Palms",
    )


def test_a_bio_keeps_emoji_line_breaks_and_accents(user):
    bio = "Proud \U0001f3f3\ufe0f\u200d\U0001f308 volunteer, I \u2764\ufe0f LA.\nJosé, Zoë."
    assert save(user, bio=bio, last_name="Nguyễn").status_code == 303
    assert profiles[uid_of(user)].bio == bio


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
        (
            "Sam",
            "'O'Neil",
            False,
            "Sam O.",
        ),  # the first letter, not the first character
        ("Sam", "123", False, "Sam"),
    ],
)
def test_volunteer_name(first, last, full, shown):
    assert data.volunteer_name(first, last, full) == shown


# --- onboarding --------------------------------------------------------------------------


def test_onboarding_refuses_hidden_characters_in_its_text(new_user):
    r = new_user.post("/onboarding", data={**FORM, "first_name": "Sam\u3164"})
    assert HIDDEN_CHARS_MESSAGE in r.text.replace("&#x27;", "'")
    assert not profiles[uid_of(new_user)].onboarded_at


def test_onboarding_is_answered_once(user):
    """Later changes go through /me/profile, which checks them the same way."""
    r = user.post(
        "/onboarding", data={**FORM, "first_name": "Other"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/deck"
    assert profiles[uid_of(user)].display_name == "Sam"


def test_deleting_the_account_erases_the_details(user):
    save(user)
    uid = uid_of(user)
    user.post("/profile/delete")
    p = profiles[uid]
    assert (p.last_name, p.pronouns, p.bio, p.interests) == (None, None, None, None)


def test_the_neighborhood_nonprofits_see_is_edited_in_the_profile(user):
    assert save(user, neighborhood="Echo Park").status_code == 303
    assert profiles[uid_of(user)].neighborhood == "Echo Park"
    for bad, why in (
        ("Echo\u202e Park", "hidden or special"),
        ("x" * 81, "80 characters"),
    ):
        r = save(user, neighborhood=bad)
        assert r.status_code == 400 and why in r.text.replace("&#x27;", "'")
    assert profiles[uid_of(user)].neighborhood == "Echo Park"


@pytest.mark.parametrize(
    "bio",
    [
        "Proud \U0001f3f3\ufe0f\u200d\U0001f308 ally",  # rainbow flag
        "Our \U0001f468\u200d\U0001f469\u200d\U0001f467 helps",  # a family
        "\U0001f469\U0001f3fd\u200d\U0001f4bb by day",  # skin tone, then a joiner
        "Team \u0031\ufe0f\u20e3 and \u0023\ufe0f\u20e3",  # keycaps 1 and #
    ],
)
def test_a_bio_keeps_emoji_sequences_and_keycaps(user, bio):
    assert save(user, bio=bio).status_code == 303
    assert profiles[uid_of(user)].bio == bio


@pytest.mark.parametrize(
    "bio",
    [
        "a" + "\u200d" * 500 + "b",  # joiners between letters, a wall of them
        "Ca\u200dll me",  # one, hiding inside a word
        "x\u200d\U0001f308",  # a letter before it
        "1\ufe0f",  # emoji style on a digit, no keycap
    ],
)
def test_a_bio_refuses_joiners_outside_emoji(user, bio):
    r = save(user, bio=bio)
    assert r.status_code == 400 and "hidden or special characters" in r.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("last_name", "\u0936\u094d\u200d\u0930\u0940"),  # Devanagari, a ZWJ conjunct
        ("last_name", "\u0915\u094d\u200c\u0937"),  # Devanagari, ZWNJ: no conjunct
        ("first_name", "\u0dc1\u0dca\u200d\u0dbb\u0dd3"),  # Sinhala "Shri"
        ("bio", "\u0d9a\u0dca\u200d\u0dc2 \u0dc0\u0dd2\u0dc1\u0dca\u0dc0"),  # Sinhala
    ],
)
def test_names_keep_the_joiners_of_indic_conjuncts(user, field, value):
    assert save(user, **{field: value}).status_code == 303


@pytest.mark.parametrize(
    "value",
    [
        "Ca\u200cll me",  # a non-joiner between Latin letters
        "\u0915\u094d\u200d\u200d\u0937",  # two joiners in a row
        "\u0915\u094d\u200dA",  # a Devanagari virama, then a Latin letter
        "\u0915\u200d\u0dbb",  # Devanagari before, Sinhala after
    ],
)
def test_joiners_outside_one_indic_word_are_refused(user, value):
    r = save(user, last_name=value)
    assert r.status_code == 400 and "hidden or special characters" in r.text


def test_email_and_other_causes_are_edited_in_the_profile(user):
    assert save(user, email="sam@example.com", other_causes="owls").status_code == 303
    p = profiles[uid_of(user)]
    assert (p.email, p.other_causes) == ("sam@example.com", "owls")
    assert save(user, email="nope").status_code == 400
