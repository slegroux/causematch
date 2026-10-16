"""Screenshot every page of a demo (scripts/demo.py's data), for reviewing the design.

uv run python scripts/screenshots.py --out DIR [--seed-file PATH] [--port 5078]

Volunteer pages at iPhone 13 size (v*.png), nonprofit and admin pages at 1280x860 (o*.png,
a*.png). Runs the demo in this process, on a throwaway database, and needs Playwright's
chromium (uv run playwright install chromium).
"""

import argparse
import sys
import threading
import time
from pathlib import Path

import demo  # scripts/demo.py: this script's folder is first on sys.path

from app import demo_seed  # demo put the repo on sys.path


def start_server(port):
    import uvicorn

    from main import UVICORN, app

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", **UVICORN)
    )
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise SystemExit("the demo server did not start")
        time.sleep(0.05)
    return server


def volunteer_sign_in(page, phone):
    page.goto("/login")
    page.get_by_label("Mobile number").fill(phone)
    page.get_by_role("button", name="Text me a code").click()
    page.get_by_label("Code").fill("000000")
    page.get_by_role("button", name="Sign in").click()


def staff_sign_in(page, email, lands_on):
    page.goto(demo_seed.sign_in_link(email))
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"**{lands_on}")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--seed-file", help="nonprofits JSON, kept outside the repo")
    p.add_argument("--port", type=int, default=5078)
    a = p.parse_args()
    demo.refuse_production()
    if a.seed_file and demo.inside_this_repo(a.out):
        sys.exit(
            "--out must be outside the repo with --seed-file: real org data never "
            "enters git"
        )
    a.out.mkdir(parents=True, exist_ok=True)
    demo.configure(a.port, seed_file=a.seed_file)
    info = demo_seed.seed(a.seed_file)
    server = start_server(a.port)
    url = f"http://127.0.0.1:{a.port}"
    shots = []

    from playwright.sync_api import sync_playwright

    def shot(page, name, full_page=True):
        page.wait_for_load_state("networkidle")
        page.screenshot(path=a.out / f"{name}.png", full_page=full_page)
        shots.append(name)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        phone = browser.new_context(**pw.devices["iPhone 13"], base_url=url)
        v = phone.new_page()
        v.goto("/login")
        shot(v, "v01-login", full_page=False)
        v.get_by_label("Mobile number").fill("(310) 555-0142")
        v.get_by_role("button", name="Text me a code").click()
        v.get_by_label("Code").wait_for()
        shot(v, "v02-code", full_page=False)
        v.get_by_label("Code").fill("000000")
        v.get_by_role("button", name="Sign in").click()
        v.wait_for_url("**/onboarding")
        shot(v, "v03-onboarding")
        phone.clear_cookies()
        volunteer_sign_in(v, demo_seed.ME_PHONE)
        v.wait_for_url("**/deck")
        # the Home Screen toast shows (an iPhone user agent, in a tab); then dismiss it, so
        # it covers nothing in the other shots
        shot(v, "v04a-home-screen-toast", full_page=False)
        v.get_by_role("button", name="Dismiss").click()
        shot(v, "v04-deck", full_page=False)
        for n in range(1, 7):  # the next cards: photos, logos and links vary by org
            v.wait_for_timeout(500)  # swipe.js ignores taps right after a swap
            v.get_by_role("button", name="Pass").click()
            v.wait_for_load_state("networkidle")
            shot(v, f"v04-deck-{n}", full_page=False)
        for name, path in (
            ("v05-matches", "/matches"),
            ("v06-chat", f"/chat/{info['match_id']}"),
            ("v07-history", "/history"),
            ("v08-profile", "/profile"),
            ("v09-preferences", "/me/preferences"),
            ("v10-deck-free-today", "/deck?today=1"),
            ("v12-my-profile", "/me/profile"),
            ("v13-report", f"/matches/{info['match_id']}/report"),
        ):
            v.goto(path)
            shot(v, name, full_page=name != "v06-chat")
        v.goto("/deck")
        v.locator("summary").click()  # the deck's filter sheet, open over the card
        shot(v, "v11-deck-filters", full_page=False)

        desk = browser.new_context(
            viewport={"width": 1280, "height": 860}, base_url=url
        )
        o = desk.new_page()
        o.goto("/org/login")
        shot(o, "o01-login", full_page=False)
        staff_sign_in(o, demo_seed.COORDINATOR[0], "/org")
        shot(o, "o02-dashboard")
        for name, path in (
            ("o03-inbox", "/org/inbox"),
            ("o04-new-opening", "/org/openings/new"),
            ("o05-shifts", "/org/shifts"),
            ("o06-chat", f"/org/chat/{info['match_id']}"),
            ("o10-block", f"/org/matches/{info['match_id']}/block"),
            ("o07-privacy", "/privacy"),
            ("o08-organization", "/org/organization"),
            ("o09-opening-media", f"/org/openings/{info['opening_id']}/media"),
            ("o11-urgent-call", f"/org/openings/{info['opening_id']}/urgent"),
        ):
            o.goto(path)
            shot(o, name, full_page=name != "o06-chat")

        admin = browser.new_context(
            viewport={"width": 1280, "height": 860}, base_url=url
        )
        ad = admin.new_page()
        staff_sign_in(ad, demo_seed.ADMIN[0], "/admin")
        shot(ad, "a01-admin")
        ad.goto("/admin/orgs")
        shot(ad, "a02-admin-orgs")
        ad.goto("/admin/reports")
        shot(ad, "a03-admin-reports")

        # a volunteer with no photo: the deck's "Add a photo" nudge
        bare = browser.new_context(**pw.devices["iPhone 13"], base_url=url)
        n = bare.new_page()
        volunteer_sign_in(n, demo_seed.NO_PHOTO_PHONE)
        n.wait_for_url("**/deck")
        n.get_by_role("button", name="Dismiss").click()
        shot(n, "v10-deck-photo-nudge")
        browser.close()
    server.should_exit = True
    print(f"{len(shots)} screenshots in {a.out}")


if __name__ == "__main__":
    main()
