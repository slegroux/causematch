import logging
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FORM, ORIGIN, coordinator_for, sign_in
from starlette.testclient import TestClient
from test_access import ROUTES

from app import data, migrations, notify, propublica, security
from app.db import db as app_db
from app.db import orgs, profiles, rate_events
from main import UVICORN, app

ROOT = Path(__file__).resolve().parent.parent
EVIL = "https://causematch.test.evil.example"

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


@pytest.mark.parametrize("ext", ["js", "css", "png"])
def test_encoded_dotdot_does_not_serve_a_public_extension_file(anon, tmp_path, ext):
    """The old bug served these, signed out, from anywhere on disk."""
    import os

    marker = tmp_path / f"x.{ext}"
    marker.write_text("OUTSIDE-MARKER")
    rel = (
        os.path.relpath(marker, os.getcwd())
        .replace("/..", "/%2e%2e")
        .replace("../", "%2e%2e/")
    )
    for path in (f"/{rel}", f"/%2e%2e/{rel}"):
        r = get_raw(anon, path)
        assert r.status_code in (303, 400, 403, 404)
        assert "OUTSIDE-MARKER" not in r.text


# --- rate limits: persistent ----------------------------------------------------------------


def codes_sent():
    return len(notify.get_verifier().sent)


def test_sixth_code_request_within_ten_minutes_is_429(anon):
    for _ in range(5):
        assert anon.post("/login/send", data={"phone": "3105550111"}).status_code == 200
    r = anon.post(
        "/login/send", data={"phone": "(310) 555-0111"}
    )  # same number, written another way
    assert r.status_code == 429 and "Too many tries" in r.text
    assert r.headers["retry-after"]
    assert codes_sent() == 5
    # another number from another address is untouched
    other = TestClient(app, client=("203.0.113.9", 1))
    assert other.post("/login/send", data={"phone": "3105550112"}).status_code == 200


def test_code_limits_per_phone_slide_and_cap_the_day():
    phone, t = "+13105550113", 1_000_000.0
    rules = ((security.SMS_PER_PHONE, phone),)
    assert all(security.allow(*rules, now=t + i) for i in range(5))
    assert not security.allow(*rules, now=t + 5)
    assert security.allow(
        *rules, now=t + 601
    )  # the first ones left the 10-minute window
    for i in range(4):
        assert security.allow(*rules, now=t + 1300 + i * 601)
    assert not security.allow(*rules, now=t + 9000)  # 10 today
    assert security.allow(*rules, now=t + 24 * 3600 + 1)


def test_codes_per_ip_cap_at_twenty_an_hour(anon):
    for i in range(20):
        r = anon.post("/login/send", data={"phone": f"31055502{i:02d}"})
        assert r.status_code == 200
    assert anon.post("/login/send", data={"phone": "3105550299"}).status_code == 429
    assert codes_sent() == 20


def test_a_suspended_number_is_counted_like_any_other(anon):
    """The limit runs before the suspended check, so a 429 says nothing about the account."""
    sign_in(TestClient(app), "3105550114")
    profiles.update(
        id=data.get_or_create_volunteer("+13105550114"), suspended_at="2026-01-01"
    )
    rate_events.delete_where()  # the sign-in's own send
    codes = [
        anon.post("/login/send", data={"phone": "3105550114"}).status_code
        for _ in range(6)
    ]
    assert codes == [200] * 5 + [429]


def test_eleventh_code_check_in_ten_minutes_is_429_even_with_the_right_code(anon):
    anon.post("/login/send", data={"phone": "3105550115"})
    for _ in range(10):
        r = anon.post("/login/verify", data={"code": "123456"})
        assert r.status_code == 200 and "didn't work" in r.text
    r = anon.post("/login/verify", data={"code": "000000"}, follow_redirects=False)
    assert r.status_code == 429
    assert anon.get("/deck", follow_redirects=False).headers["location"] == "/login"


def test_sign_in_links_cap_per_address_whether_or_not_it_is_a_coordinator(anon):
    _, known = coordinator_for(0)
    for email in (known, "nobody@example.org"):
        codes = [
            anon.post("/org/login/send", data={"email": email}).status_code
            for _ in range(6)
        ]
        assert codes == [200] * 5 + [429]
    assert len(notify.get_emailer().sent) == 5  # only the coordinator's five


def test_sign_in_links_cap_per_ip():
    c = TestClient(app, client=("198.51.100.7", 1))
    codes = [
        c.post("/org/login/send", data={"email": f"x{i}@example.org"}).status_code
        for i in range(21)
    ]
    assert codes == [200] * 20 + [429]


def test_claims_cap_per_ip_and_per_email():
    from test_claim import FORM as CLAIM

    c = TestClient(app, client=("198.51.100.8", 1))
    codes = [
        c.post("/org/claim", data={**CLAIM, "email": f"m{i}@example.org"}).status_code
        for i in range(6)
    ]
    assert codes == [200] * 5 + [429]
    codes = [
        TestClient(app, client=(f"198.51.100.{20 + i}", 1))
        .post("/org/claim", data=CLAIM)
        .status_code
        for i in range(4)
    ]
    assert codes == [200] * 3 + [429]


def test_an_invalid_claim_is_not_counted():
    from test_claim import FORM as CLAIM

    c = TestClient(app, client=("198.51.100.9", 1))
    for _ in range(8):
        assert c.post("/org/claim", data={**CLAIM, "ein": "12"}).status_code == 200
    assert c.post("/org/claim", data=CLAIM).status_code == 200


def test_propublica_lookups_and_imports_share_a_limit(monkeypatch):
    from test_safety import client_for, serve_fixture

    monkeypatch.setattr(security, "PROPUBLICA", (("propublica", 2, 3600),))
    from test_claim import admin_client

    monkeypatch.setattr(propublica, "make_client", client_for(serve_fixture))
    admin = admin_client()
    org = orgs()[0].id
    url, ein = f"/admin/orgs/{org}/lookup", {"ein": "86-1620575"}
    assert admin.get(url, params=ein).status_code == 200
    assert admin.get(url, params=ein).status_code == 200
    assert admin.get(url, params=ein).status_code == 429


def test_counters_hold_no_phone_email_or_ip(anon):
    anon.post("/login/send", data={"phone": "3105550116"})
    anon.post("/org/login/send", data={"email": "who@example.org"})
    keys = " ".join(e.key for e in rate_events())
    assert keys and not re.search(r"3105550116|who@|testclient|\+1", keys)


def test_counters_live_in_the_database_so_a_restart_keeps_them(anon):
    for _ in range(5):
        anon.post("/login/send", data={"phone": "3105550117"})
    security.reset_buckets()  # what a restart clears
    assert anon.post("/login/send", data={"phone": "3105550117"}).status_code == 429
    assert rate_events.count >= 5


# --- rate limits: in memory -----------------------------------------------------------------


def test_swipes_are_throttled_per_user(user):
    codes = [
        user.post("/swipe", data={"opening_id": "x", "liked": "0"}).status_code
        for _ in range(31)
    ]
    assert codes == [200] * 30 + [429]


def test_chat_posts_are_throttled_per_user(user):
    codes = [
        user.post(
            "/chat/1/send", data={"msg": "hi"}, follow_redirects=False
        ).status_code
        for _ in range(21)
    ]
    assert 429 not in codes[:20] and codes[20] == 429


def test_report_and_block_posts_share_a_throttle(user):
    codes = [
        user.post(f"/matches/1/{kind}", data={}, follow_redirects=False).status_code
        for kind in ("report", "block") * 5 + ("report",)
    ]
    assert 429 not in codes[:10] and codes[10] == 429


def test_a_bucket_refills_over_time():
    assert all(security.take_token("chat", 7, now=100.0) for _ in range(20))
    assert not security.take_token("chat", 7, now=100.0)
    assert security.take_token("chat", 7, now=100.6)  # 2 tokens a second
    assert security.take_token("chat", 8, now=100.0)  # per user


# --- cross-site requests --------------------------------------------------------------------

POSTS = sorted(path for method, path in ROUTES if method == "POST")


def concrete(path):
    return re.sub(r"\{\w+\}", "1", path)


@pytest.mark.parametrize("path", [p for p in POSTS if not p.startswith("/tasks/")])
def test_every_post_route_refuses_a_foreign_origin(anon, path):
    r = anon.post(concrete(path), data={}, headers={"Origin": EVIL})
    assert r.status_code == 403 and "outside Causematch" in r.text


def test_cross_origin_post_is_403_and_does_nothing(anon):
    r = anon.post("/login/send", data={"phone": "3105550118"}, headers={"Origin": EVIL})
    assert r.status_code == 403
    assert codes_sent() == 0


@pytest.mark.parametrize("who", ["user", "coord", "admin"])
def test_cross_origin_is_refused_for_signed_in_users_too(who, request):
    if who == "admin":
        from test_claim import admin_client

        client, path = admin_client(), "/admin/claims/1/approve"
    else:
        client = request.getfixturevalue(who)
        path = "/swipe" if who == "user" else "/org/openings/new"
    assert client.post(path, data={}, headers={"Origin": EVIL}).status_code == 403
    assert client.post(path, data={}, headers={"Origin": "null"}).status_code == 403


def test_no_origin_needs_a_same_origin_referer(anon):
    del anon.headers["origin"]
    assert anon.post("/login/send", data={"phone": "3105550119"}).status_code == 403
    for ref in (EVIL + "/login", "https://causematch.test:8443/login", "not a url"):
        r = anon.post(
            "/login/send", data={"phone": "3105550119"}, headers={"Referer": ref}
        )
        assert r.status_code == 403
    r = anon.post(
        "/login/send",
        data={"phone": "3105550119"},
        headers={"Referer": ORIGIN + "/login"},
    )
    assert r.status_code == 200 and codes_sent() == 1


def test_an_origin_is_matched_exactly(anon):
    for origin in (
        ORIGIN + "/",
        ORIGIN.replace("https", "http"),
        ORIGIN + ":443",
        ORIGIN.upper(),
    ):
        r = anon.post(
            "/login/send", data={"phone": "3105550120"}, headers={"Origin": origin}
        )
        assert r.status_code == 403, origin


def test_safe_methods_are_not_checked(anon):
    assert anon.get("/login", headers={"Origin": EVIL}).status_code == 200


def test_tasks_take_the_token_without_an_origin(monkeypatch):
    monkeypatch.setenv("TASKS_TOKEN", "t")
    c = TestClient(app)
    del c.headers["origin"]
    r = c.post("/tasks/reminders", headers={"Authorization": "Bearer t"})
    assert r.status_code == 200
    assert c.post("/tasks/reminders").status_code == 401  # still the token's call


# --- headers --------------------------------------------------------------------------------

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self' wss://causematch.test; font-src 'self'; frame-ancestors 'none'; "
    "base-uri 'self'; form-action 'self'; object-src 'none'"
)


@pytest.mark.parametrize("path", ["/", "/login", "/nope"])
def test_security_headers_on_every_response(anon, path):
    h = anon.get(path, follow_redirects=False).headers
    assert h["content-security-policy"] == CSP
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert (
        h["permissions-policy"]
        == "camera=(), microphone=(), geolocation=(), payment=()"
    )
    assert h["x-frame-options"] == "DENY"
    assert h["cache-control"] == "no-store"
    assert re.fullmatch(r"[0-9a-f]{32}", h["x-request-id"])
    assert "strict-transport-security" not in h  # development: http


def test_signed_in_pages_are_no_store(user):
    assert user.get("/deck").headers["cache-control"] == "no-store"


def test_static_files_may_be_cached_and_are_gzipped(anon):
    r = anon.get("/static/vendor/app.css", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and "no-store" not in r.headers.get("cache-control", "")
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_pages_are_not_gzipped(user):
    r = user.get("/deck", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in r.headers


def test_hsts_in_production(anon, monkeypatch):
    monkeypatch.setattr(security, "is_production", lambda: True)
    h = anon.get("/login").headers
    assert h["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_the_sign_in_link_page_keeps_its_url_to_itself(anon):
    r = anon.get("/org/login/verify", params={"token": "abc"})
    assert r.headers["referrer-policy"] == "same-origin"


SCRIPT_INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>")
EXTERNAL_ASSET = re.compile(r"""(src=|rel="stylesheet" href=)["']?(https?:)?//""")


def test_no_page_has_inline_script_style_or_external_asset(user, coord, anon):
    pages = [
        (anon, "/login"),
        (anon, "/org/login"),
        (anon, "/org/claim"),
        (anon, "/privacy"),
        (anon, "/org/login/verify?token=x"),
        (user, "/deck"),
        (user, "/matches"),
        (user, "/profile"),
        (coord, "/org"),
        (coord, "/org/inbox"),
        (coord, "/org/openings/new"),
    ]
    for client, path in pages:
        html = client.get(path).text
        assert not SCRIPT_INLINE.search(html), path
        assert "<style" not in html and " style=" not in html, path
        assert not re.search(r"\son[a-z]+=", html), path
        assert not EXTERNAL_ASSET.search(html), path


def test_every_stylesheet_and_script_is_served(anon):
    html = anon.get("/login").text
    assets = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert len(assets) >= 8
    for a in assets:
        assert anon.get(a).status_code == 200, a


# --- body size ------------------------------------------------------------------------------


def test_a_body_over_64_kb_is_413(anon):
    r = anon.post("/login/send", data={"phone": "3" * 70_000})
    assert r.status_code == 413 and codes_sent() == 0


def test_a_chunked_body_over_64_kb_is_413(anon):
    body = iter([b"phone=" + b"3" * 40_000, b"3" * 40_000])
    r = anon.post(
        "/login/send",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 413 and codes_sent() == 0


# --- errors ---------------------------------------------------------------------------------


LEAK = "+13105550199 maria@example.org"


async def broken(scope, receive, send):
    raise ValueError(LEAK)  # a message holding PII, as a real one might


def test_production_errors_are_a_plain_page_logged_without_pii(monkeypatch, caplog):
    monkeypatch.setattr(security, "is_production", lambda: True)
    c = TestClient(security.Guard(broken), raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="causematch"):
        r = c.get("/boom?token=secret", headers={"Origin": ORIGIN})
    assert r.status_code == 500 and "Something went wrong" in r.text
    rid = r.headers["x-request-id"]
    assert rid in r.text and "Traceback" not in r.text and "+1310" not in r.text
    (rec,) = [x for x in caplog.records if rid in x.getMessage()]
    msg = rec.getMessage()
    assert "ValueError" in msg and "/boom" in msg
    assert "+1310" not in msg and "maria" not in msg and "secret" not in msg


def test_development_errors_still_raise():
    with pytest.raises(ValueError):
        TestClient(security.Guard(broken)).get("/boom")


def test_cloud_run_trace_id_is_the_request_id(anon):
    h = {"X-Cloud-Trace-Context": "105445aa7843bc8bf206b12000100000/1;o=1"}
    assert (
        anon.get("/login", headers=h).headers["x-request-id"]
        == "105445aa7843bc8bf206b12000100000"
    )


def test_the_access_log_drops_sign_in_tokens():
    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/org/login/verify?token=abc-DEF_1&x=1", "1.1", 200),
        None,
    )
    security.RedactTokens().filter(rec)
    assert "abc" not in rec.getMessage() and "token=[redacted]&x=1" in rec.getMessage()


# --- client IP ------------------------------------------------------------------------------


def scope(xff=None, peer="10.0.0.1"):
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return {"type": "http", "headers": headers, "client": (peer, 1)}


def test_off_cloud_run_the_header_is_ignored(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert security.client_ip(scope("1.2.3.4")) == "10.0.0.1"


@pytest.mark.parametrize(
    "xff,expected",
    [
        ("203.0.113.5", "203.0.113.5"),
        ("1.1.1.1, 203.0.113.5", "203.0.113.5"),  # the client's own entry is ignored
        ("1.1.1.1, 203.0.113.5, 35.191.3.4", "203.0.113.5"),  # a load balancer hop
        ("203.0.113.5, 130.211.0.9, 35.191.0.1", "203.0.113.5"),
        ("1.1.1.1, nonsense", "10.0.0.1"),  # unparseable on the right: the peer
        ("", "10.0.0.1"),
        (None, "10.0.0.1"),
        ("35.191.0.1", "10.0.0.1"),
        ("2001:db8::1", "2001:db8::1"),
    ],
)
def test_on_cloud_run_the_rightmost_untrusted_hop(monkeypatch, xff, expected):
    monkeypatch.setenv("K_SERVICE", "causematch")
    assert security.client_ip(scope(xff)) == expected


def test_ipv6_counts_per_64():
    assert security._subject("2001:db8:1:2:aaaa::1") == security._subject(
        "2001:db8:1:2:bbbb::9"
    )
    assert security._subject("2001:db8:1:3::1") != security._subject("2001:db8:1:2::1")
    assert security._subject("203.0.113.5") == "203.0.113.5"


# --- cookies and server settings ------------------------------------------------------------


def test_development_session_cookie(anon):
    anon.post("/login/send", data={"phone": "3105550121"})
    r = anon.post("/login/verify", data={"code": "000000"}, follow_redirects=False)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("cm_session=")
    assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
    assert "path=/" in cookie.lower()


def test_production_session_cookie_is_host_prefixed_and_secure(tmp_path):
    """Imports main as Cloud Run would (K_SERVICE), in a child process with dummy secrets."""
    code = (
        "from main import app\n"
        "from starlette.middleware.sessions import SessionMiddleware\n"
        "(m,) = [m for m in app.user_middleware if m.cls is SessionMiddleware]\n"
        "print(m.kwargs['session_cookie'], m.kwargs['https_only'], m.kwargs['same_site'],"
        " m.kwargs['path'], m.kwargs['domain'])\n"
    )
    env = {
        "PATH": os.environ["PATH"],
        "K_SERVICE": "causematch",
        "CAUSEMATCH_DB": str(tmp_path / "p.db"),
        "SESSION_SECRET": "s" * 32,
        "PUBLIC_BASE_URL": "https://causematch.example",
        "TASKS_TOKEN": "t",
        "TWILIO_ACCOUNT_SID": "AC0",
        "TWILIO_AUTH_TOKEN": "x",
        "TWILIO_VERIFY_SID": "VA0",
        "TWILIO_FROM_NUMBER": "+13105550100",
        "EMAIL_API_KEY": "x",
        "EMAIL_FROM": "hello@causematch.example",
        "CONTACT_EMAIL": "hello@causematch.example",
        "PHONE_HASH_KEY": "k",
    }
    out = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["__Host-cm_session", "True", "lax", "/", "None"]


def test_procfile_runs_uvicorn_with_mains_settings():
    line = (ROOT / "Procfile").read_text()
    assert f"--ws-max-size {UVICORN['ws_max_size']}" in line
    assert f"--limit-concurrency {UVICORN['limit_concurrency']}" in line
    assert f"--timeout-keep-alive {UVICORN['timeout_keep_alive']}" in line
    assert f"--timeout-graceful-shutdown {UVICORN['timeout_graceful_shutdown']}" in line
    assert "--no-proxy-headers" in line and UVICORN["proxy_headers"] is False


# --- migrations -----------------------------------------------------------------------------


def run_migrations(db):
    return subprocess.run(
        [sys.executable, "-m", "app.migrations", "--db", str(db)],
        check=False,
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=120,
    )


def v1_copy(tmp_path):
    """The test database as it stood before migrations existed (no schema_migrations)."""
    db = tmp_path / "v1.db"
    # through the app's own connection: a second SQLite library (sqlite3) on the live file
    # can miss what is still in its WAL
    app_db.execute("VACUUM INTO ?", [str(db)])
    with sqlite3.connect(db) as c:
        c.execute("DROP TABLE schema_migrations")
    return db


def test_migrations_apply_pending_steps_then_none(tmp_path):
    db = v1_copy(tmp_path)
    first = run_migrations(db)
    assert first.returncode == 0, first.stderr
    assert first.stdout.startswith(f"applied {len(migrations.available())} migration")
    second = run_migrations(db)
    assert second.returncode == 0 and second.stdout.startswith("applied 0 migration")
    with sqlite3.connect(db) as c:
        assert [v for (v,) in c.execute("SELECT version FROM schema_migrations")] == [
            v for v, _ in migrations.available()
        ]
        assert c.execute("SELECT count(*) FROM profile").fetchone() is not None


def test_a_missing_database_is_an_error(tmp_path):
    r = run_migrations(tmp_path / "none.db")
    assert r.returncode == 1 and not (tmp_path / "none.db").exists()


def test_the_running_database_is_migrated():
    done = {r["version"] for r in app_db.q("SELECT version FROM schema_migrations")}
    assert done == {v for v, _ in migrations.available()}


def apsw_db(path):
    import apsw

    return apsw.Connection(str(path))


def test_a_new_database_records_every_step_without_running_it(tmp_path):
    conn, ran = apsw_db(tmp_path / "new.db"), []
    assert migrations.migrate(conn, [(1, ran.append), (2, ran.append)]) == 0
    assert ran == []
    assert [v for (v,) in conn.execute("SELECT version FROM schema_migrations")] == [
        1,
        2,
    ]


def test_a_failing_step_changes_nothing_and_stops_the_run(tmp_path):
    conn = apsw_db(tmp_path / "old.db")
    conn.execute("CREATE TABLE thing (a TEXT); INSERT INTO thing VALUES ('x')")

    def rename(c):
        c.execute("ALTER TABLE thing RENAME COLUMN a TO b")

    def broken(c):
        c.execute("ALTER TABLE thing RENAME COLUMN b TO c")
        raise RuntimeError("half-way")

    ran = []
    with pytest.raises(RuntimeError):
        migrations.migrate(conn, [(1, rename), (2, broken), (3, ran.append)])
    assert ran == []
    assert [r[1] for r in conn.execute("PRAGMA table_info(thing)")] == ["b"]
    assert [v for (v,) in conn.execute("SELECT version FROM schema_migrations")] == [1]
    assert migrations.migrate(conn, [(1, rename), (2, lambda c: None)]) == 1


def test_migration_modules_are_numbered_once():
    names = [n for _, n in migrations.available()]
    assert names[0] == "0001_baseline"
    for _, name in migrations.available():
        mod = __import__(f"app.migrations.{name}", fromlist=["up"])
        assert callable(mod.up)


# --- secret scan ----------------------------------------------------------------------------


def test_secret_scan_config_and_script_exist():
    assert "useDefault = true" in (ROOT / ".gitleaks.toml").read_text()
    script = ROOT / "scripts" / "secret_scan.sh"
    assert os.access(script, os.X_OK)
    r = subprocess.run(
        [str(script)],
        check=False,
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin"},  # no gitleaks on this PATH
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0 and "not installed" in r.stdout
    r = subprocess.run(
        [str(script)],
        check=False,
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "CI": "true"},  # CI must not skip it
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode != 0
