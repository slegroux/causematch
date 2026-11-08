"""Smoke test for a deployed, password-protected preview: the path a presenter takes in
front of nonprofits, driven through the app's own pages in a phone-sized browser.

PREVIEW_PASSWORD=... uv run python scripts/smoke_preview.py https://<service>.run.app [--screenshots DIR]

Run it after each scripts/deploy_preview.sh. Each step prints `ok <step> <ms>` or
`FAIL <step>: <reason>`; later steps still run, and the exit code is 0 only when every
step passed. The last line is `summary: N ok, M failed, total Ts`.

The password rule: PREVIEW_PASSWORD comes from the environment only and is never printed,
logged or written to a file. Every message goes through scrub() (a browser error can quote
the `?access=` address it was loading), and a URL that contains `access=` is refused.

The URL must be https: a preview refuses any other origin. --insecure accepts a
self-signed certificate, for a local run only. A run makes two demo copies of the
volunteer; an address may make 10 an hour.
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

PHONE = {"width": 390, "height": 844}  # like scripts/video/record.py
COOKIE = "__Host-cm_preview"  # app/security.py PREVIEW_COOKIE
CHIP = "Confirms your hours"  # app/data.py CONFIRMS_HOURS
GENUINE = "This is a genuine Causematch certificate"  # app/routes/hours.py CheckPage
SECRET = ""


def scrub(text):
    for form in (SECRET, quote(SECRET, safe="")):
        text = text.replace(form, "***")
    return text


def need(ok, why):
    if not ok:
        raise AssertionError(why)


def run(base, shots, insecure):
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        def visitor():
            ctx = browser.new_context(viewport=PHONE, ignore_https_errors=insecure)
            ctx.set_default_timeout(15000)
            return ctx.new_page()

        def enter(page):
            """Follows the access link: sets the cookie, redirects to the site."""
            r = page.goto(f"{base}/?access={SECRET}")
            need(r.status == 200, f"with ?access= the answer was {r.status}")
            need(any(c["name"] == COOKIE for c in page.context.cookies()), "no cookie")

        V, N, G = visitor(), visitor(), visitor()  # volunteer, nonprofit, guest
        st = {}

        def text(page):
            return page.inner_text("body")

        def no_cookie(url):
            """GET with a client that has no cookies at all, not following redirects."""
            ctx = pw.request.new_context(ignore_https_errors=insecure)
            return ctx.get(url, max_redirects=0)

        def step(name, page, fn):
            t, err, ms = time.monotonic(), "", None
            try:
                ms = fn()
            except Exception as e:  # noqa: BLE001 - a failing step must not stop the next
                err = scrub((str(e).strip().splitlines() or [repr(e)])[0])
            ms = ms if isinstance(ms, int) else round((time.monotonic() - t) * 1000)
            print(f"FAIL {name}: {err[:200]}" if err else f"ok {name} {ms}", flush=True)
            results.append(not err)
            if shots:
                try:
                    page.screenshot(path=str(shots / f"{name}.png"))
                except (PlaywrightError, OSError) as e:  # a courtesy, never a failure
                    print(f"  (no screenshot: {scrub(str(e))[:80]})")

        def gate():
            r = no_cookie(base + "/")
            need(r.status == 401, f"without the cookie GET / gave {r.status}")
            for page in (V, N, G):
                enter(page)

        def volunteer_demo():
            V.goto(base + "/login")
            button = V.get_by_role("button", name="Try it as a volunteer")
            need(button.count(), "no 'Try it as a volunteer' button on /login")
            t = time.monotonic()
            with V.expect_response(lambda r: r.url.endswith("/login/demo")) as post:
                button.click()
            # 429: the address already made 10 demo copies this hour (a run makes two)
            need(post.value.status == 303, f"POST /login/demo gave {post.value.status}")
            V.wait_for_url("**/deck")
            return round((time.monotonic() - t) * 1000)

        def deck():
            cards = V.locator("[data-swipe-card]")
            need(cards.count() >= 1, "no card in the deck")
            if CHIP in cards.first.inner_text():
                return
            # Passing consumes cards and like_match needs this deck's first one, so
            # look further down in a scout's deck: a second fresh copy of the volunteer.
            S = visitor()
            enter(S)
            S.goto(base + "/login")
            S.get_by_role("button", name="Try it as a volunteer").click()
            S.wait_for_url("**/deck")
            for _ in range(15):
                top = S.locator("[data-swipe-card]")
                if not top.count():
                    break
                seen = top.first.inner_text()
                if CHIP in seen:
                    S.context.close()
                    return
                S.get_by_role("button", name="Pass").first.click()
                S.wait_for_function(
                    "t => { const c = document.querySelector('[data-swipe-card]');"
                    " return !c || c.innerText !== t }",
                    arg=seen,
                )
                S.wait_for_timeout(500)  # swipe.js ignores taps 400 ms after a swap
            S.context.close()
            raise AssertionError("no card confirms hours")

        def like_match():
            V.get_by_role("button", name="Like").first.click()
            try:  # an urgent card books at once (chat); a first like asks two questions
                V.wait_for_url(re.compile(r"/(chat/|onboarding/more)"), timeout=5000)
            except PlaywrightTimeout:
                pass
            if "/onboarding/more" in V.url:
                V.get_by_role("button", name="Skip").click()
                V.wait_for_url("**/deck")
            for _ in range(8):  # a stand-in team says yes about 4 seconds after a like
                V.goto(base + "/matches")
                link = V.locator("a[href^='/chat/']")
                if link.count():
                    st["match"] = link.first.get_attribute("href").rsplit("/", 1)[1]
                    return
                V.wait_for_timeout(2000)
            raise AssertionError("no match on /matches 16 seconds after the like")

        def hours():
            V.goto(base + "/history")
            need("Confirmed by" in text(V), "no confirmed hours")

        def certificate():
            V.goto(base + "/history")
            form = V.locator("form[action$='/certificate']").first
            need(form.count(), "no certificate form on /history")
            form.locator("select[name=period]").select_option("all")
            form.get_by_role("button", name="Get a certificate").click()
            V.wait_for_url("**/hours/certificate/*")
            link = re.search(r"https://\S+/check/[\w-]+", text(V))
            need(link, "no check link on the certificate page")
            st["check"] = link.group(0)
            pdf = V.context.request.get(f"{V.url}.pdf")
            ctype = pdf.headers.get("content-type", "")
            need(ctype.startswith("application/pdf"), f"PDF: {pdf.status} {ctype}")
            need(pdf.body()[:4] == b"%PDF", "the download does not start with %PDF")

        def check_public():
            need("check" in st, "no check link (the certificate step failed)")
            link = st["check"]
            need(
                urlsplit(link).netloc == urlsplit(base).netloc, "check link: other host"
            )
            # app/auth.py PUBLIC lets /check/... past the sign-in check, but PreviewGate
            # (app/security.py) wraps every path, so in a preview a browser with no access
            # cookie gets 401. Expected here; if /check is ever exempted, flip this.
            r = no_cookie(link)
            need(r.status == 401, f"no cookie: the check link gave {r.status}")
            # a browser with the access cookie but no sign-in (G, a third context)
            r = G.goto(link)
            need(r.status == 200, f"with the access cookie only: {r.status}")
            need(GENUINE in text(G), "the check page does not say genuine")

        def nonprofit_demo():
            N.goto(base + "/org/login")
            button = N.get_by_role("button", name="Try it as a nonprofit")
            need(button.count(), "no 'Try it as a nonprofit' button on /org/login")
            button.click()
            N.wait_for_url("**/org")
            need("Your openings" in text(N), "no dashboard after Try it as a nonprofit")
            need("match" in st, "no match from like_match to look for")
            # the seed's own Sam is in the inbox too, so find this visit's by its match id
            N.goto(base + "/org/inbox")
            need(
                N.locator(f"a[href='/org/chat/{st['match']}']").count(),
                "the volunteer's match is not in the nonprofit's inbox "
                "(the liked card's nonprofit is not the demo nonprofit?)",
            )

        def shifts():
            N.goto(base + "/org/shifts")
            body = text(N)
            need(
                "Did they come?" in body
                and ("Not marked yet" in body or "Marked this week" in body),
                "no shift to mark or marked",
            )

        def privacy():
            r = G.goto(base + "/privacy")
            need(r.status == 200, f"/privacy answered {r.status}")
            need("Hours certificates" in text(G), "no 'Hours certificates' on /privacy")

        def blocked():
            raise AssertionError("not run: the gate step failed")

        started = time.monotonic()
        for name, page, fn in (
            ("gate", G, gate),
            ("volunteer_demo", V, volunteer_demo),
            ("deck", V, deck),
            ("like_match", V, like_match),
            ("hours", V, hours),
            ("certificate", V, certificate),
            ("check_public", G, check_public),
            ("nonprofit_demo", N, nonprofit_demo),
            ("shifts", N, shifts),
            ("privacy", G, privacy),
        ):
            step(name, page, fn if name == "gate" or results[0] else blocked)
        browser.close()
    failed = results.count(False)
    total = time.monotonic() - started
    print(f"summary: {len(results) - failed} ok, {failed} failed, total {total:.1f}s")
    return failed


def main():
    global SECRET
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("url", help="the preview's https URL, without the access key")
    p.add_argument("--screenshots", metavar="DIR", help="save one PNG per step here")
    p.add_argument("--insecure", action="store_true", help="self-signed certificate")
    a = p.parse_args()
    SECRET = os.environ.get("PREVIEW_PASSWORD", "")
    if not SECRET:
        sys.exit("PREVIEW_PASSWORD is not set (see Smoke test in docs/OPERATIONS.md)")
    if "access=" in a.url:
        sys.exit("refusing a URL that contains access=: pass the bare preview URL")
    if urlsplit(a.url).scheme != "https":
        sys.exit("refusing a URL that is not https")
    shots = Path(a.screenshots) if a.screenshots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    print(f"smoke test of {a.url.rstrip('/')}", flush=True)
    try:
        failed = run(a.url.rstrip("/"), shots, a.insecure)
    except Exception as e:  # noqa: BLE001  no traceback: it could quote the key
        sys.exit(scrub(f"smoke test could not run: {e}".splitlines()[0]))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
