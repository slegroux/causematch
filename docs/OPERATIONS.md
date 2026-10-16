# Operations

Everything needed to configure, run, secure and deploy Causematch: environment variables, scheduled jobs, the security model, front-end assets, the demo, the preview deploy, schema changes, the secret scan and admin tasks. For what the app does, see the [README](../README.md); for how it is built, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Configuration

Environment variables (`.env.example` lists them all, without values). The app refuses to start in production without:

- `CAUSEMATCH_ENV`, `SESSION_SECRET`, `PUBLIC_BASE_URL`, `TASKS_TOKEN`
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_VERIFY_SID`, `TWILIO_FROM_NUMBER`
- `EMAIL_API_KEY`, `EMAIL_FROM`
- `CONTACT_EMAIL`: the address on the privacy page
- `PHONE_HASH_KEY`: keys the hash kept of a deleted account's phone when a block, report or suspension must outlive it, the rate-limit hashes, and the urgent unsubscribe links
- `TRUSTED_PROXY_HOPS` on Cloud Run (see Security)

`SESSION_SECRET`, `PHONE_HASH_KEY` and `TASKS_TOKEN` must be at least 32 random characters in production, and `PUBLIC_BASE_URL` an `https://` origin with no path.

### Scheduled jobs

The host scales to zero, so nothing runs in the background: Cloud Scheduler is to call two endpoints with `Authorization: Bearer $TASKS_TOKEN` (anything else gets 401).

> **Not set up yet.** No scheduler jobs exist today, so shift reminders, retries and urgent waves don't go out on their own. Create the jobs below as part of the production launch. A preview can't be called this way at all: its password gate reads the same `Authorization` header, so a preview would first need `/tasks/*` exempted from `PreviewGate` or the token moved to its own header.

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
- **Headers** on every response: a CSP allowing only this origin (`script-src 'self'`, `style-src 'self'`, no inline code), `X-Frame-Options: DENY`, `nosniff`, a referrer policy, a permissions policy, `Cache-Control: no-store` outside `/static` and `/favicon.ico` (unless a route sets its own, as photos do), and HSTS in production.
- **Rate limits.** Sending codes and links, wrong code checks, claims and ProPublica calls are counted in the `rate_event` table (keyed by a hash, never the phone, email or IP), so a restart doesn't reset them; swipes, chat, report/block posts and photo uploads use in-memory token buckets per user. Over an HTTP limit: 429 and a warning naming it (chat frames over the bucket close the socket with 1013). Codes are counted per (phone, client address), per phone per day, per address, per IPv6 /48, and against a ceiling on every text the app sends (`SMS_MAX_PER_HOUR`, default 100; `SMS_MAX_PER_DAY`, default 500). A notification text over the ceiling is recorded `deferred`; the reminders task (planned hourly, see Scheduled jobs) resends deferred "you're matched" and message texts, and a later urgent run retries a deferred urgent notice.
- **Client IP.** `TRUSTED_PROXY_HOPS` says how many addresses the proxies in front append to `X-Forwarded-For`: the client is that many places from the right, and anything further left (whatever the client sent) is ignored. 0, the default, uses the socket peer. On Cloud Run it must be set: 1 for the plain `*.run.app` URL, 2 behind an external Application Load Balancer. IPv4-mapped IPv6 counts as IPv4; IPv6 clients are counted per /64. With `CAUSEMATCH_LOG_LEVEL=DEBUG` each limited request logs a hashed client key (never the address), for checking the setting after a deploy.
- **Bodies** over 64 KB get 413, except on the photo upload paths (`security.UPLOADS`), which take up to 6 MB.
- **Photos** (`app/photos.py`) are decoded by Pillow and re-encoded as fresh JPEGs, so no EXIF, GPS or other metadata survives and the browser never gets the uploaded bytes. The size is read from the header and refused past a per-format cap before decoding (JPEG 50 MP, WebP 8 MP, PNG/GIF 12 MP); a JPEG's marker stream is walked first and refused unless it is plain (baseline, extended or progressive, at most 50 scans, a coefficient budget for multi-scan files, no stray bytes). The decode itself runs in a child process (`app/photo_worker.py`, about 60 ms per upload), at most two at a time, with an environment of `PATH` and `LANG` only (no secrets): it is killed after 10 s or past 4 MB of output, it can't dump core, and on Linux it also runs under `RLIMIT_AS` (512 MB) and `RLIMIT_CPU` (8 s); macOS doesn't enforce `RLIMIT_AS`, so development has the CPU limit and the timeout only. The child runs as the same user: it limits resources, it doesn't drop privilege. A WebP's size is read from its header in Python, so the app process itself never runs libwebp on an upload. A decode shrinks at once to 4 MP, so one upload stays under about 200 MB. Uploads share a per-user bucket (10, then one a minute). The bytes live in SQLite (the `photo` table), so a database backup covers them too. They are stored under unguessable keys, served at `/photos/{key}.jpg` with an access check: a volunteer's photo only to themselves, an admin, and a nonprofit whose inbox shows one of their matches, with no block either way (`data._sees_volunteer`); anyone else gets 404. Give Cloud Run at least 1 GiB of memory for two worst-case decodes at once. In production an error is a plain 500 page with a reference, and the log line has the request id and stack frames but not the exception's message (it can hold a phone number).
- **Development mode is for localhost only:** its session secret is public and its fake SMS accepts `000000`. Production refuses both.
- **Preview** (`CAUSEMATCH_ENV=preview`, also on Cloud Run) is production behind a password: the same secrets, cookie, HSTS and checks, plus `PREVIEW_PASSWORD` (at least 16 characters, else the app refuses to start). `security.PreviewGate`, just inside `Guard`, lets nobody in without it, on every request: pages, `/static`, `/photos`, `/tasks` and websockets. The link to send is the service URL with `?access=<PREVIEW_PASSWORD>`. On a top-level navigation (or from curl, which sends no Sec-Fetch headers) a GET with the right key (constant-time compare) gets a 303 to the same path and query without the key, and the cookie `__Host-cm_preview` (Secure, HttpOnly, SameSite=Lax, Path=/, 30 days). The cookie holds its issue time and an HMAC-SHA256 keyed with `SESSION_SECRET` over the password's digest and that time, so it can't be made without the secret, is refused after 30 days, and a new password voids every one. A request with `?access=` that isn't a navigation (an `<img>` on another site, say) is refused without weighing or counting the key, and a browser that already has the cookie just gets the 303 without the key. The cookie lets the browser in from then on, websocket handshakes included; HTTP Basic auth with the password (any user name) still works, for scripts and `curl -u`. Anything else gets a plain 401 page ("This is a private preview. Ask the Causematch team for the link.") with no `WWW-Authenticate`, so browsers show no password dialog; websockets are closed with 1008. Wrong keys, wrong or expired cookies and wrong Basic passwords are counted per client address (10, then one a minute; past that 429 without checking); a wrong cookie is also deleted, so it counts once. The app's access log redacts the key (`security.RedactTokens`), but Cloud Run's own request log records the full URL of the link's first request, key included: anyone who can read the project's logs can read the password. To revoke every link and cookie, rotate the password. Make the password with `python -c 'import secrets; print(secrets.token_urlsafe(24))'`: random, and URL-safe, so the link needs no percent-encoding. Send the link by direct message, not in a channel or document that unfurls links (an unfurler fetches it and logs the URL too). Only `/robots.txt` answers without it (`Disallow: /`), and every response carries `X-Robots-Tag: noindex, nofollow`. A preview may use the fake SMS and emailer, shows a "Preview" banner on every page, seeds the demo's data into its empty database at startup (`app/demo_seed.py`: `PREVIEW_SEED_DIR`, else the invented orgs), logs the demo coordinator's and admin's sign-in links, and shows a coordinator's new link on `/org/login` instead of emailing it (an admin's stays in the log, with the address masked). Production refuses all of these.
- **Session cookie:** `__Host-cm_session` in production (Secure, HttpOnly, SameSite=Lax, Path=/), `cm_session` in development.

Run the app with the flags in the `Procfile` (`--ws-max-size 65536`, `--limit-concurrency 200`, ...); `python main.py` uses the same ones.

### Front-end assets

Everything the browser loads is served from `/static`: htmx, Franken UI and daisyUI are vendored under `static/vendor/` (versions and checksums in `static/vendor/README.md`), and MonsterUI's inline theme code lives in `static/theme.js` and `static/theme.css`. Tailwind is compiled ahead of time into `static/vendor/app.css`: after adding Tailwind classes, run `scripts/build_css.sh` (it fetches a pinned, checksum-verified Tailwind CLI) and commit the result.

The design tokens (warm palette, one color per cause, the self-hosted Nunito font) are CSS variables in `static/theme.css`, also Tailwind colors (`bg-cause-food`, `text-brand`, ...) through `tailwind.config.js`. Form fields go through `app/ui.py` (`TextInput`, `Choice`, `Checks`) so each one carries its Franken UI class; `tests/test_ui.py` checks every page.

### Demo

`uv run python scripts/demo.py` serves the app on a throwaway database with demo data and prints how to sign in as a volunteer, a coordinator and an admin. It refuses production. `--seed-file PATH` loads nonprofits from a JSON file kept outside the repo (`app/demo_seed.py`'s docstring has the format); their openings are titled "Sample: ..." and every page shows a demo banner. Logos and event photos come from `logos-raw/` and `photos-raw/` next to the seed file, read only through `photos.process` (SVG logos are skipped); every demo volunteer's photo is drawn by the script, never a real person. `scripts/screenshots.py --out DIR` screenshots every page of the same demo with Playwright.

### Preview deploy

`scripts/deploy_preview.sh` deploys the password-protected preview (`CAUSEMATCH_ENV=preview`, see Security) to Cloud Run: service `causematch-preview` in project `causematch-la`, `us-west1`, running as the service account `causematch-preview@causematch-la.iam.gserviceaccount.com` (override with `PROJECT`, `REGION`, `SERVICE`, `SERVICE_ACCOUNT`). It needs the secrets `PREVIEW_PASSWORD`, `SESSION_SECRET`, `PHONE_HASH_KEY` and `TASKS_TOKEN` in Secret Manager, readable by that account, and no Twilio or email: the preview uses the fake ones. It copies the seed (`SEED_SRC`: `orgs.json`, `logos-raw/`, `photos-raw/`) into `preview-seed/`, which git ignores and `.gcloudignore` sends, builds the `Dockerfile` with Cloud Build (`gcloud run deploy --source .`), removes `preview-seed/` again, and sets `PUBLIC_BASE_URL` to the service URL it prints: open that URL, since forms only work on that origin. One instance at most, scaled to zero when idle, 1 GiB. The database lives in the container and is reseeded at each cold start; the demo coordinator's and admin's sign-in links are in the startup log, or `/org/login` shows a new coordinator link on the page. The script refuses a `PROJECT` whose name isn't `Causematch` (a project id is first come, first served).

- **Rotating the password** (a new `PREVIEW_PASSWORD` version, then redeploy) voids every access cookie and old link: the next request with an old cookie gets the 401 page, the cookie is deleted, and it counts as one wrong guess. Send the new link. A browser that still sends Basic credentials saved before the access link existed counts a wrong guess per request, so after 10 it gets 429 for up to 10 minutes: clear the site's saved credentials (or use a private window). Everyone behind one address (an office network) shares that count.
- **The seed also leaves this machine**: `gcloud run deploy --source .` uploads the build context, `preview-seed/` included, to the Cloud Build source bucket (`gs://run-sources-<project>-<region>`), and the image holding it stays in Artifact Registry (`cloud-run-source-deploy`). Both are private to the project; delete old ones, or set a lifecycle rule on the bucket, when the preview goes away.

### Schema changes

Adding a column or a table: edit the class in `app/db.py` (`transform=True` does the rest). Renames, data changes, index changes and anything touching triggers need a numbered migration in `app/migrations/` instead; its docstring says how. Pending migrations run at startup, or by hand with `uv run python -m app.migrations [--db PATH]`.

### Secret scan

`scripts/secret_scan.sh` runs gitleaks with `.gitleaks.toml` over the git history.

## Admin

Orgs start unverified and their openings stay out of every deck until an admin approves them at `/admin/orgs` (EIN checked against ProPublica) or with `scripts/verify_org.py`. Reports, suspensions, org deactivation, removing a reported volunteer's photo and unblocking are in `/admin/reports` and `/admin/blocks`. Nonprofits come from [ProPublica Nonprofit Explorer](https://projects.propublica.org/nonprofits/) (IRS public data): `scripts/import_orgs.py --city "Los Angeles" --state CA` imports a sample of Los Angeles ones (any LA County city works), `--ein` imports one, and a coordinator asks for access at `/org/claim` (an admin approves at `/admin/claims`). A report from a volunteer names an org, so to remove one coordinator use `scripts/remove_coordinator.py`.
