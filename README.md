# Causematch

Swipe to volunteer. Causematch matches people with nonprofits across Los Angeles County that need a hand (food, environment and parks, animal shelters, homelessness services): volunteers swipe through openings ranked for them, the nonprofit says yes too, and a chat opens.

- **Volunteers** use it on their phone: sign in with a text code, answer a short onboarding, swipe. Each card says why it's shown (causes, travel time, when you're free, kind of work); "I'm free today" and a filter sheet narrow the deck for a visit.
- **Match preferences** live in Me, then Preferences, and stay private: up to two places you help from (a neighborhood or ZIP area, or your device's location rounded to about 1 km, never an address) with how you travel and how far, causes rated love / open / not for me, the days and dates you're free, the kind of work and languages, and whether each one only ranks openings or also hides the ones that don't fit.
- **Profiles** (Me, then Your profile): photo, name, pronouns, short bio, interests and the neighborhood nonprofits see. A nonprofit sees what `SHARED_WITH_NONPROFIT` lists (that profile, the causes you love or are open to, a general summary of when you're free, the languages you speak and how often you'd like to help) only after you like one of its openings, as "Sam L." until it says yes. The privacy page (`/privacy`) says the same.
- **Nonprofits** use the dashboard: post openings anywhere in Los Angeles County with their kind of work (setting, effort, group size, first-timer friendly, languages), a photo and a link to their own event page, add their logo, accept volunteers, mark shifts done. Homelessness services openings are group shifts only (adults, no one-to-one contact).
- **Last-minute calls**: a nonprofit short of people in the next 48 hours posts an urgent call on an opening ("needs 2, tomorrow 9 am"), optionally accepting the first ones who say yes. Volunteers who turned on last-minute calls under Preferences and are near, free and outside their quiet hours get a text (or an email) linking to the card, which their deck shows first. Limits: 3 calls per org a day, 2 waves per call of at most 3 × the people needed (25 at most), 3 notices per volunteer a week, and at most half of each SMS ceiling, so sign-in codes always have room. A wave that would reach no one (everyone in their quiet hours) waits for a later run. Each urgent email carries a signed unsubscribe link (`/urgent/off/<key>`, works signed out) and a `List-Unsubscribe` header.

Built with [FastHTML](https://fastht.ml), [MonsterUI](https://monsterui.answer.ai) and SQLite.

Status: early development.

## Configuration

Environment variables (`.env.example` lists them all, without values). The app refuses to start in production without:

- `CAUSEMATCH_ENV`, `SESSION_SECRET`, `PUBLIC_BASE_URL`, `TASKS_TOKEN`
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_VERIFY_SID`, `TWILIO_FROM_NUMBER`
- `EMAIL_API_KEY`, `EMAIL_FROM`
- `CONTACT_EMAIL`: the address on the privacy page
- `PHONE_HASH_KEY`: keys the hash kept of a deleted account's phone when a block, report or suspension must outlive it, and the rate-limit hashes
- `TRUSTED_PROXY_HOPS` on Cloud Run (see Security)

`SESSION_SECRET`, `PHONE_HASH_KEY` and `TASKS_TOKEN` must be at least 32 random characters in production, and `PUBLIC_BASE_URL` an `https://` origin with no path.

### Scheduled jobs

The host scales to zero, so nothing runs in the background: Cloud Scheduler calls two endpoints with `Authorization: Bearer $TASKS_TOKEN` (anything else gets 401).

| Job | Schedule | Does |
|---|---|---|
| `POST /tasks/reminders` | hourly (`0 * * * *`) | shift reminders 23 to 25 hours ahead, retries of failed or deferred "you're matched" and message texts, and blanking decided claim requests |
| `POST /tasks/urgent` | every 10 minutes (`*/10 * * * *`) | urgent calls' notification waves: wave 1 at the first run after a call is posted, wave 2 thirty minutes later if it hasn't filled |

```sh
gcloud scheduler jobs create http causematch-urgent --location=$REGION \
  --schedule='*/10 * * * *' --time-zone=America/Los_Angeles \
  --uri="$PUBLIC_BASE_URL/tasks/urgent" --http-method=POST \
  --headers="Authorization=Bearer $TASKS_TOKEN"
```

The reminders job is created the same way, with `--schedule='0 * * * *'` and `/tasks/reminders`.

## Security

`app/security.py` holds the request-level defences; `security.Guard` wraps the whole app, ahead of sessions and Beforeware.

- **Same-origin writes.** Every POST/PUT/PATCH/DELETE must carry an `Origin` equal to `PUBLIC_BASE_URL`'s origin (or, with no `Origin`, a `Referer` on it), else 403. This covers the public sign-in and claim forms too. Only `/tasks/*` is exempt (Cloud Scheduler sends a bearer token and no `Origin`). Websocket handshakes need the exact `Origin`, else they are closed with 1008.
- **Headers** on every response: a CSP allowing only this origin (`script-src 'self'`, `style-src 'self'`, no inline code), `X-Frame-Options: DENY`, `nosniff`, a referrer policy, a permissions policy, `Cache-Control: no-store` outside `/static`, and HSTS in production.
- **Rate limits.** Sending codes and links, wrong code checks, claims and ProPublica calls are counted in the `rate_event` table (keyed by a hash, never the phone, email or IP), so a restart doesn't reset them; swipes, chat, report/block posts and photo uploads use in-memory token buckets per user. Over the limit: 429, and a warning naming the limit. Codes are counted per (phone, client address), per phone per day, per address, per IPv6 /48, and against a ceiling on every text the app sends (`SMS_MAX_PER_HOUR`, default 100; `SMS_MAX_PER_DAY`, default 500). A notification text over the ceiling is recorded `deferred` and the hourly sweep sends it later.
- **Client IP.** `TRUSTED_PROXY_HOPS` says how many addresses the proxies in front append to `X-Forwarded-For`: the client is that many places from the right, and anything further left (whatever the client sent) is ignored. 0, the default, uses the socket peer. On Cloud Run it must be set: 1 for the plain `*.run.app` URL, 2 behind an external Application Load Balancer. IPv4-mapped IPv6 counts as IPv4; IPv6 clients are counted per /64. With `CAUSEMATCH_LOG_LEVEL=DEBUG` each limited request logs a hashed client key (never the address), for checking the setting after a deploy.
- **Bodies** over 64 KB get 413, except on the photo upload paths (`security.UPLOADS`), which take up to 6 MB.
- **Photos** (`app/photos.py`) are decoded by Pillow and re-encoded as fresh JPEGs, so no EXIF, GPS or other metadata survives and the browser never gets the uploaded bytes. The size is read from the header and refused past a per-format cap before decoding (JPEG 50 MP, WebP 8 MP, PNG/GIF 12 MP); a JPEG's marker stream is walked first and refused unless it is plain (baseline, extended or progressive, at most 50 scans, a coefficient budget for multi-scan files, no stray bytes). The decode itself runs in a child process (`app/photo_worker.py`, about 60 ms per upload), at most two at a time, with an environment of `PATH` and `LANG` only (no secrets): it is killed after 10 s or past 4 MB of output, it can't dump core, and on Linux it also runs under `RLIMIT_AS` (512 MB) and `RLIMIT_CPU` (8 s); macOS doesn't enforce `RLIMIT_AS`, so development has the CPU limit and the timeout only. The child runs as the same user: it limits resources, it doesn't drop privilege. A WebP's size is read from its header in Python, so the app process itself never runs libwebp on an upload. A decode shrinks at once to 4 MP, so one upload stays under about 200 MB. Uploads share a per-user bucket (10, then one a minute). The bytes live in SQLite (the `photo` table, replicated with the rest), under unguessable keys, served at `/photos/{key}.jpg` with an access check: a volunteer's photo only to themselves, an admin, and a nonprofit whose inbox shows one of their matches (`data._sees_volunteer`), anyone else gets 404. Give Cloud Run at least 1 GiB of memory for two worst-case decodes at once. In production an error is a plain 500 page with a reference, and the log line has the request id and stack frames but not the exception's message (it can hold a phone number).
- **Development mode is for localhost only:** its session secret is public and its fake SMS accepts `000000`. Production refuses both.
- **Session cookie:** `__Host-cm_session` in production (Secure, HttpOnly, SameSite=Lax, Path=/), `cm_session` in development.

Run the app with the flags in the `Procfile` (`--ws-max-size 65536`, `--limit-concurrency 200`, ...); `python main.py` uses the same ones.

### Front-end assets

Everything the browser loads is served from `/static`: htmx, Franken UI and daisyUI are vendored under `static/vendor/` (versions and checksums in `static/vendor/README.md`), and MonsterUI's inline theme code lives in `static/theme.js` and `static/theme.css`. Tailwind is compiled ahead of time into `static/vendor/app.css`: after adding Tailwind classes, run `scripts/build_css.sh` (it fetches a pinned, checksum-verified Tailwind CLI) and commit the result.

The design tokens (warm palette, one color per cause, the self-hosted Nunito font) are CSS variables in `static/theme.css`, also Tailwind colors (`bg-cause-food`, `text-brand`, ...) through `tailwind.config.js`. Form fields go through `app/ui.py` (`TextInput`, `Choice`, `Checks`) so each one carries its Franken UI class; `tests/test_ui.py` checks every page.

### Demo

`uv run python scripts/demo.py` serves the app on a throwaway database with demo data and prints how to sign in as a volunteer, a coordinator and an admin. It refuses production. `--seed-file PATH` loads nonprofits from a JSON file kept outside the repo (its docstring has the format); their openings are titled "Sample: ..." and every page shows a demo banner. Logos and event photos come from `logos-raw/` and `photos-raw/` next to the seed file, read only through `photos.process` (SVG logos are skipped); every demo volunteer's photo is drawn by the script, never a real person. `scripts/screenshots.py --out DIR` screenshots every page of the same demo with Playwright.

### Schema changes

Adding a column or a table: edit the class in `app/db.py` (`transform=True` does the rest). Renames, data changes, index changes and anything touching triggers need a numbered migration in `app/migrations/` instead; its docstring says how. Pending migrations run at startup, or by hand with `uv run python -m app.migrations [--db PATH]`.

### Secret scan

`scripts/secret_scan.sh` runs gitleaks with `.gitleaks.toml` over the git history.

## Admin

Orgs start unverified and their openings stay out of every deck until an admin approves them at `/admin/orgs` (EIN checked against ProPublica) or with `scripts/verify_org.py`. Reports, suspensions, org deactivation, removing a reported volunteer's photo and unblocking are in `/admin/reports` and `/admin/blocks`. Nonprofits come from [ProPublica Nonprofit Explorer](https://projects.propublica.org/nonprofits/) (IRS public data): `scripts/import_orgs.py --city "Los Angeles" --state CA` imports a sample of Los Angeles ones (any LA County city works), `--ein` imports one, and a coordinator asks for access at `/org/claim` (an admin approves at `/admin/claims`). A report from a volunteer names an org, so to remove one coordinator use `scripts/remove_coordinator.py`.

## License

MIT
