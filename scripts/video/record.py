"""Record the full story: a new nonprofit and a new volunteer join, match, chat, and the
volunteer's hours get confirmed.

python record.py BASE_URL SERVER_LOG BEATS.json DURATIONS.json DB OUT_DIR

BASE_URL is scripts/video/serve.py's server and DB its throwaway database (one step edits
it: "a few days later" moves the shift's start into the past). Three recordings (phone:
Riley; jordan: the new coordinator; admin) and timeline.json. Each beat lasts at least as
long as its voice-over line. Any console error, page error, failed request, 5xx answer or
server error makes the run fail, after the recordings are saved so you can look.
"""

import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, LOG = sys.argv[1], Path(sys.argv[2])
BEATS = json.loads(Path(sys.argv[3]).read_text())
DUR = json.loads(Path(sys.argv[4]).read_text())
DB = sys.argv[5]
OUT = Path(sys.argv[6])
OUT.mkdir(parents=True, exist_ok=True)
GAP = 0.8  # seconds of quiet after each line
SHIFT_HOURS = 3  # the opening's length: "3 hours", which Attended confirms in one tap

CURSOR = """
(() => {
  const go = () => {
    if (document.getElementById('cm-cursor')) return;
    const d = document.createElement('div');
    d.id = 'cm-cursor';
    Object.assign(d.style, {position: 'fixed', left: '-100px', top: '-100px', width: '26px',
      height: '26px', margin: '-13px 0 0 -13px', borderRadius: '50%', zIndex: 2147483647,
      background: 'rgba(192,69,42,0.30)', border: '2px solid rgba(192,69,42,0.85)',
      pointerEvents: 'none', transition: 'transform 120ms ease'});
    document.documentElement.appendChild(d);
    const at = (x, y) => { d.style.left = x + 'px'; d.style.top = y + 'px'; };
    addEventListener('mousemove', e => { at(e.clientX, e.clientY);
      sessionStorage.setItem('cm-xy', e.clientX + ',' + e.clientY); }, true);
    addEventListener('mousedown', () => { d.style.transform = 'scale(0.65)'; }, true);
    addEventListener('mouseup', () => { d.style.transform = 'scale(1)'; }, true);
    const last = sessionStorage.getItem('cm-xy');
    if (last) { const [x, y] = last.split(','); at(x, y); }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go); else go();
})();
"""

problems = []


def watch(name, page):
    """Collect what a person watching the browser's console would see go wrong."""
    page.on(
        "console",
        lambda m: (
            problems.append(
                f"{name} console {m.type}: {m.text} ({m.location.get('url')})"
            )
            if m.type in ("error", "assert")
            else None
        ),
    )
    page.on("pageerror", lambda e: problems.append(f"{name} page error: {e}"))
    page.on(
        "requestfailed", lambda r: problems.append(f"{name} request failed: {r.url}")
    )
    page.on(
        "response",
        lambda r: (
            problems.append(f"{name} HTTP {r.status}: {r.url}")
            if r.status >= 500
            else None
        ),
    )


def hold(s):
    time.sleep(s)


def point(page, loc, steps=18):
    """Move the cursor onto loc, first scrolling it clear of the fixed top bar and the
    phone's bottom tab bar (a tap under the tab bar would hit the tab instead)."""
    loc.scroll_into_view_if_needed()
    box = loc.bounding_box()
    vh = page.viewport_size["height"]
    if box["y"] < 80 or box["y"] + box["height"] > vh - 120:
        loc.evaluate("e => e.scrollIntoView({behavior: 'smooth', block: 'center'})")
        hold(0.8)
        box = loc.bounding_box()
    page.mouse.move(
        box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=steps
    )
    hold(0.3)


def tap(page, loc):
    point(page, loc)
    loc.click()


def smooth_to(page, loc):
    loc.evaluate("e => e.scrollIntoView({behavior: 'smooth', block: 'center'})")
    hold(0.9)


def type_in(page, loc, text, delay=45):
    tap(page, loc)
    loc.press_sequentially(text, delay=delay)
    hold(0.3)


def email_count():
    return LOG.read_text().count("FAKE EMAIL")


def link_after(before, masked):
    deadline = time.time() + 15
    while time.time() < deadline:
        text = LOG.read_text()
        if text.count("FAKE EMAIL") > before:
            hits = re.findall(
                rf"FAKE EMAIL to {re.escape(masked)}.*?(/org/login/verify\?token=[\w-]+)",
                text,
                re.DOTALL,
            )
            if hits:
                return hits[-1]
        time.sleep(0.2)
    raise RuntimeError(f"no sign-in link for {masked}")


def days_pass():
    """Time can't pass during a live recording: move Riley's shift at Silver Lake Garden
    Friends to two days ago (Los Angeles wall clock, 9 am), in the throwaway database."""
    started = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%dT09:00")
    with sqlite3.connect(DB, timeout=10) as db:
        done = db.execute(
            "UPDATE shift SET starts_at = ? WHERE status = 'scheduled' AND match_id IN "
            "(SELECT m.id FROM match m JOIN opening o ON o.id = m.opening_id "
            "JOIN org g ON g.id = o.org_id WHERE g.name = 'Silver Lake Garden Friends')",
            [started],
        ).rowcount
    if done != 1:
        raise RuntimeError(f"expected to move 1 shift, moved {done}")


timeline = []
t0 = None
need_until = 0.0


def beat(bid):
    """Start a beat once the previous one's line has had its time."""
    global need_until
    now = time.monotonic() - t0
    if now < need_until:
        hold(need_until - now)
    start = round(time.monotonic() - t0, 2)
    timeline.append({"id": bid, "t": start})
    need_until = start + DUR[bid] + GAP


def story(vol, org, adm):
    # intro, under the title card: the admin signs in, every page is opened
    beat("intro")
    before = email_count()
    adm.goto("/org/login")
    adm.get_by_label("Email").fill("ada@demo.causematch.test")
    adm.get_by_role("button", name="Email me a link").click()
    adm.goto(link_after(before, "a***@d"))
    adm.get_by_role("button", name="Sign in").click()
    adm.wait_for_url("**/admin")
    adm.goto("/admin/claims")
    org.goto("/org/claim")
    vol.goto("/login")
    for p, xy in ((vol, (195, 560)), (org, (640, 420)), (adm, (640, 420))):
        p.mouse.move(*xy)

    # 1. Jordan asks for access
    beat("claim")
    hold(1.2)
    type_in(org, org.get_by_label("Organization's EIN"), "99-0000001", 70)
    type_in(
        org, org.get_by_label("Organization name"), "Silver Lake Garden Friends", 35
    )
    type_in(org, org.get_by_label("Your name"), "Jordan Reyes", 45)
    type_in(org, org.get_by_label("Your email"), "jordan@silverlake.example", 30)
    tap(org, org.get_by_text("I'm authorized to represent this organization"))
    tap(org, org.get_by_role("button", name="Request access"))
    org.get_by_text("We'll review your request").wait_for()

    # 2. the admin approves
    beat("approve")
    adm.reload()
    hold(1.5)
    card = adm.locator("div", has_text="Jordan Reyes").last
    point(adm, card.get_by_text("Jordan Reyes").first)
    hold(1.5)
    tap(adm, card.get_by_role("button", name="Approve"))
    adm.get_by_text("Approved").first.wait_for()

    # 3. Jordan signs in from the emailed link
    beat("signin")
    org.goto("/org/login")
    hold(0.8)
    before = email_count()
    type_in(org, org.get_by_label("Email"), "jordan@silverlake.example", 30)
    tap(org, org.get_by_role("button", name="Email me a link"))
    link = link_after(before, "j***@s")
    hold(2.0)  # the "check your email" page
    org.goto(link)
    hold(1.0)
    tap(org, org.get_by_role("button", name="Sign in"))
    org.wait_for_url("**/org")

    # 4. Jordan says the nonprofit confirms hours, then posts an opening
    beat("post")
    hold(1.0)
    tap(org, org.get_by_role("link", name="Organization").first)
    org.wait_for_url("**/org/organization")
    hold(0.8)
    confirms = org.get_by_text("We can confirm volunteer hours")
    smooth_to(org, confirms)
    tap(org, confirms)
    hold(0.6)
    tap(org, org.get_by_role("button", name="Save details"))
    org.wait_for_url("**notice=saved**")
    hold(1.0)
    tap(org, org.get_by_role("link", name="Openings").first)
    org.wait_for_url("**/org")
    tap(org, org.get_by_role("link", name="Post an opening"))
    org.wait_for_url("**/org/openings/new")
    type_in(
        org,
        org.get_by_label('Opening title, e.g. "Saturday pantry sorting"'),
        "Weed and water the community garden",
        30,
    )
    type_in(
        org,
        org.get_by_label("What will volunteers do?"),
        "Pull weeds, water the beds and tidy the tool shed with our crew.",
        18,
    )
    org.locator("select[name=cause]").select_option("environment-parks")
    type_in(org, org.get_by_label("ZIP code"), "90026", 80)
    start = (datetime.now() + timedelta(days=3)).replace(hour=9, minute=0)
    when = org.get_by_label("Date and time of the next shift")
    smooth_to(org, when)
    when.fill(start.strftime("%Y-%m-%dT%H:%M"))
    length = org.locator("select[name=duration_minutes]")
    smooth_to(org, length)
    length.select_option(str(SHIFT_HOURS * 60))
    org.locator("select[name=recurrence]").select_option("one_time")
    slots = org.get_by_label("How many volunteers do you need?")
    smooth_to(org, slots)
    type_in(org, slots, "4", 80)
    org.locator("select[name=orientation_required]").select_option(index=2)
    adult = org.locator("input[name=adult_only_ok]")
    smooth_to(org, adult)
    point(org, adult)
    adult.check()
    hold(0.6)
    submit = org.locator("form button[type=submit]").last
    smooth_to(org, submit)
    tap(org, submit)
    org.wait_for_url("**/org")
    hold(1.0)

    # 5. Riley registers with a phone number
    beat("register")
    hold(1.0)
    type_in(vol, vol.get_by_label("Mobile number"), "(213) 555-0142", 70)
    tap(vol, vol.get_by_role("button", name="Text me a code"))
    code = vol.get_by_label("Code")
    code.wait_for()
    hold(0.8)
    type_in(vol, code, "000000", 120)
    tap(vol, vol.get_by_role("button", name="Sign in"))
    vol.wait_for_url("**/onboarding")

    # 6. onboarding
    beat("onboard")
    hold(0.6)
    type_in(vol, vol.locator("input[name=first_name]"), "Riley", 80)
    tap(vol, vol.get_by_text("I'm 18 or older"))
    type_in(vol, vol.get_by_label("Neighborhood or ZIP"), "90026", 90)
    for slug in ("environment-parks", "food-insecurity"):
        loc = vol.locator(f"label:has(input[name='cause_{slug}'][value=love])")
        smooth_to(vol, loc)
        tap(vol, loc)
    consent = vol.get_by_text("I agree to the")
    smooth_to(vol, consent)
    tap(vol, consent)
    tap(vol, vol.get_by_role("button", name="Start swiping"))
    vol.wait_for_url("**/deck")

    # 7. free times
    beat("times")
    hold(0.8)
    tap(vol, vol.get_by_role("link", name="Me").last)
    vol.wait_for_url("**/profile")
    hold(0.6)
    tap(vol, vol.get_by_role("link", name="Preferences").first)
    vol.wait_for_url("**/me/preferences")
    smooth_to(vol, vol.locator("input[name=availability]").first)
    for slot in ("sat_morning", "sun_morning", "wed_evening"):
        box = vol.locator(f"input[name=availability][value={slot}]")
        label = vol.locator(f"label:has(input[name=availability][value={slot}])")
        target = label if label.count() else box
        point(vol, target)
        target.click(force=True)
        hold(0.4)
    tap(vol, vol.get_by_role("button", name="Save preferences"))
    vol.wait_for_url("**saved=1**")

    # 8. the deck: like Jordan's opening
    beat("like")
    hold(0.6)
    tap(vol, vol.get_by_role("link", name="Openings").last)
    vol.wait_for_url("**/deck**")
    hold(2.0)
    for _ in range(12):
        if (
            "Silver Lake Garden Friends"
            in vol.locator("[data-swipe-card]").first.inner_text()
        ):
            break
        tap(vol, vol.get_by_role("button", name="Pass").first)
        hold(1.4)
    hold(1.6)
    tap(vol, vol.get_by_role("button", name="Like").first)
    # a first like asks two optional questions: skip them, back to the deck
    vol.wait_for_url("**/onboarding/more**", timeout=8000)
    hold(1.2)
    tap(vol, vol.get_by_role("button", name="Skip"))
    vol.wait_for_url("**/deck**")
    hold(0.5)

    # 9. Jordan's inbox
    beat("inbox")
    org.reload()
    hold(1.5)
    tap(org, org.get_by_role("link", name="Inbox").first)
    org.wait_for_url("**/org/inbox")
    row = (
        org.locator("div")
        .filter(has=org.get_by_text("Riley", exact=True))
        .filter(has=org.get_by_role("button", name="Accept"))
        .last
    )
    point(org, row.get_by_text("Riley", exact=True))

    # 10. accept, say hi
    beat("accept")
    tap(org, row.get_by_role("button", name="Accept"))
    org.wait_for_url("**notice=accepted**")
    hold(1.0)
    tap(org, org.get_by_role("link", name="Say hi").first)
    org.wait_for_url("**/org/chat/**")
    type_in(
        org,
        org.locator("input[name=msg]"),
        "Hi Riley! Thanks for signing up. Bring gloves if you have them.",
        30,
    )
    tap(org, org.get_by_role("button", name="Send"))

    # 11. Riley: it's a match, reply
    beat("reply")
    tap(vol, vol.get_by_role("link", name="Matches").last)
    vol.wait_for_url("**/matches**")
    hold(2.2)
    tap(vol, vol.get_by_role("link", name="Say hi").first)
    vol.wait_for_url("**/chat/**")
    hold(1.0)
    type_in(vol, vol.locator("input[name=msg]"), "Great, see you Sunday!", 55)
    tap(vol, vol.get_by_role("button", name="Send"))

    # 12. the reply lands; the dashboard flags it
    beat("answer")
    org.get_by_text("see you Sunday").wait_for()
    hold(1.6)
    tap(org, org.get_by_role("link", name="Openings").first)
    org.wait_for_url("**/org")
    answer = org.get_by_role("link", name="1 message to answer")
    point(org, answer)
    hold(0.8)
    answer.click()
    org.wait_for_url("**/org/inbox")

    # 13. shifts
    beat("shifts")
    tap(org, org.get_by_role("link", name="Shifts").first)
    org.wait_for_url("**/org/shifts")
    hold(0.8)
    point(org, org.get_by_text("Riley").first)

    # 14. a few days later: Jordan marks it attended, the hours already filled in
    beat("attend")
    days_pass()
    org.reload()
    hold(1.2)
    hours = org.locator("form select[name=minutes]").first
    point(org, hours)
    hold(1.8)  # the select reads "3 hours": the shift's own length
    if org.locator("form select[name=minutes] option[selected]").first.inner_text() != (
        f"{SHIFT_HOURS} hours"
    ):
        raise RuntimeError("the hours select isn't preset to the shift's length")
    tap(org, org.get_by_role("button", name="Attended").first)
    org.get_by_text(f"{SHIFT_HOURS} hours confirmed").wait_for()
    hold(0.8)

    # 15. Riley's My hours
    beat("hours")
    tap(vol, vol.get_by_role("link", name="Me").last)
    vol.wait_for_url("**/profile")
    hold(0.8)
    tap(vol, vol.get_by_role("link", name="My hours").first)
    vol.wait_for_url("**/hours")
    vol.get_by_text("Confirmed by").first.wait_for()
    hold(1.0)
    smooth_to(vol, vol.get_by_text("Confirmed by").first)

    # 16. Riley prefers nonprofits that confirm hours; the deck's cards say so
    beat("prefer")
    tap(vol, vol.get_by_role("link", name="Me").last)
    vol.wait_for_url("**/profile")
    hold(0.6)
    tap(vol, vol.get_by_role("link", name="Preferences").first)
    vol.wait_for_url("**/me/preferences")
    prefer = vol.locator("label:has(input[name=verified_hours][value=prefer])")
    smooth_to(vol, prefer)
    hold(0.8)
    tap(vol, prefer)
    hold(0.8)
    tap(vol, vol.get_by_role("button", name="Save preferences"))
    vol.wait_for_url("**saved=1**")
    tap(vol, vol.get_by_role("link", name="Openings").last)
    vol.wait_for_url("**/deck**")
    hold(2.0)
    for _ in range(12):
        if (
            vol.locator("[data-swipe-card]")
            .first.get_by_text("Confirms your hours")
            .count()
        ):
            break
        tap(vol, vol.get_by_role("button", name="Pass").first)
        hold(1.4)
    else:
        raise RuntimeError("no deck card says it confirms hours")
    hold(1.0)

    beat("outro")  # waits for the last line; the outro itself is a card
    timeline.append({"id": "end", "t": round(time.monotonic() - t0, 2)})


def main():
    global t0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        def ctx(**kw):
            c = browser.new_context(base_url=BASE, bypass_csp=True, **kw)
            c.add_init_script(CURSOR)
            return c

        phone_ctx = ctx(
            **pw.devices["iPhone 13"],
            record_video_dir=str(OUT / "raw-phone"),
            record_video_size={"width": 390, "height": 664},
        )
        laptop = {
            "viewport": {"width": 1280, "height": 800},
            "record_video_size": {"width": 1280, "height": 800},
        }
        jordan_ctx = ctx(record_video_dir=str(OUT / "raw-jordan"), **laptop)
        admin_ctx = ctx(record_video_dir=str(OUT / "raw-admin"), **laptop)
        vol = phone_ctx.new_page()
        t0 = time.monotonic()
        org = jordan_ctx.new_page()
        off_jordan = time.monotonic() - t0
        adm = admin_ctx.new_page()
        off_admin = time.monotonic() - t0
        for name, page in (("riley", vol), ("jordan", org), ("admin", adm)):
            watch(name, page)

        failure = None
        try:
            story(vol, org, adm)
        except Exception as e:  # noqa: BLE001  keep the recordings and a still of each page to look at
            failure = e
            for name, page in (("riley", vol), ("jordan", org), ("admin", adm)):
                page.screenshot(path=str(OUT / f"failed-{name}.png"))
        paths = {
            "phone": vol.video.path(),
            "jordan": org.video.path(),
            "admin": adm.video.path(),
        }
        for c in (phone_ctx, jordan_ctx, admin_ctx):
            c.close()
        browser.close()

    for name, p in paths.items():
        Path(p).rename(OUT / f"{name}.webm")
    (OUT / "timeline.json").write_text(
        json.dumps(
            {
                "offsets": {
                    "phone": 0.0,
                    "jordan": round(off_jordan, 3),
                    "admin": round(off_admin, 3),
                },
                "beats": timeline,
            },
            indent=1,
        )
    )
    log = LOG.read_text()
    server = [
        line
        for line in log.splitlines()
        if "Traceback" in line or re.search(r"\b(ERROR|CRITICAL)\b", line)
    ]
    problems.extend(f"server: {line}" for line in server)
    (OUT / "problems.txt").write_text("\n".join(problems))
    if failure:
        print(f"RECORDING FAILED: {failure!r}; see {OUT}/failed-*.png", file=sys.stderr)
    if problems:
        print("PROBLEMS DURING THE RECORDING:", *problems, sep="\n  ", file=sys.stderr)
    if failure or problems:
        sys.exit(1)
    print(json.dumps(timeline))


main()
