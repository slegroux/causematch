# Causematch

Swipe to volunteer. Causematch matches people with Los Angeles nonprofits that need a hand: volunteers swipe through openings ranked for them, the nonprofit says yes too, and a chat opens.

- **Volunteers** use it on their phone: sign in with a text code, pick causes, swipe.
- **Nonprofits** use the dashboard: post openings, accept volunteers, mark shifts done.

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

## Security

`app/security.py` holds the request-level defences; `security.Guard` wraps the whole app, ahead of sessions and Beforeware.

- **Same-origin writes.** Every POST/PUT/PATCH/DELETE must carry an `Origin` equal to `PUBLIC_BASE_URL`'s origin (or, with no `Origin`, a `Referer` on it), else 403. This covers the public sign-in and claim forms too. Only `/tasks/*` is exempt (Cloud Scheduler sends a bearer token and no `Origin`). Websocket handshakes need the exact `Origin`, else they are closed with 1008.
- **Headers** on every response: a CSP allowing only this origin (`script-src 'self'`, `style-src 'self'`, no inline code), `X-Frame-Options: DENY`, `nosniff`, a referrer policy, a permissions policy, `Cache-Control: no-store` outside `/static`, and HSTS in production.
- **Rate limits.** Sending codes and links, wrong code checks, claims and ProPublica calls are counted in the `rate_event` table (keyed by a hash, never the phone, email or IP), so a restart doesn't reset them; swipes, chat and report/block posts use in-memory token buckets per user. Over the limit: 429, and a warning naming the limit. Codes are counted per (phone, client address), per phone per day, per address, per IPv6 /48, and against a ceiling on every text the app sends (`SMS_MAX_PER_HOUR`, default 100; `SMS_MAX_PER_DAY`, default 500). A notification text over the ceiling is recorded `deferred` and the hourly sweep sends it later.
- **Client IP.** `TRUSTED_PROXY_HOPS` says how many addresses the proxies in front append to `X-Forwarded-For`: the client is that many places from the right, and anything further left (whatever the client sent) is ignored. 0, the default, uses the socket peer. On Cloud Run it must be set: 1 for the plain `*.run.app` URL, 2 behind an external Application Load Balancer. IPv4-mapped IPv6 counts as IPv4; IPv6 clients are counted per /64. With `CAUSEMATCH_LOG_LEVEL=DEBUG` each limited request logs a hashed client key (never the address), for checking the setting after a deploy.
- **Bodies** over 64 KB get 413. In production an error is a plain 500 page with a reference, and the log line has the request id and stack frames but not the exception's message (it can hold a phone number).
- **Development mode is for localhost only:** its session secret is public and its fake SMS accepts `000000`. Production refuses both.
- **Session cookie:** `__Host-cm_session` in production (Secure, HttpOnly, SameSite=Lax, Path=/), `cm_session` in development.

Run the app with the flags in the `Procfile` (`--ws-max-size 65536`, `--limit-concurrency 200`, ...); `python main.py` uses the same ones.

### Front-end assets

Everything the browser loads is served from `/static`: htmx, Franken UI and daisyUI are vendored under `static/vendor/` (versions and checksums in `static/vendor/README.md`), and MonsterUI's inline theme code lives in `static/theme.js` and `static/theme.css`. Tailwind is compiled ahead of time into `static/vendor/app.css`: after adding Tailwind classes, run `scripts/build_css.sh` (it fetches a pinned, checksum-verified Tailwind CLI) and commit the result.

The design tokens (warm palette, one color per cause, the self-hosted Nunito font) are CSS variables in `static/theme.css`, also Tailwind colors (`bg-cause-food`, `text-brand`, ...) through `tailwind.config.js`. Form fields go through `app/ui.py` (`TextInput`, `Choice`, `Checks`) so each one carries its Franken UI class; `tests/test_ui.py` checks every page.

### Demo

`uv run python scripts/demo.py` serves the app on a throwaway database with demo data and prints how to sign in as a volunteer, a coordinator and an admin. It refuses production. `--seed-file PATH` loads nonprofits from a JSON file kept outside the repo (its docstring has the format); their openings are titled "Sample: ..." and every page shows a demo banner. `scripts/screenshots.py --out DIR` screenshots every page of the same demo with Playwright.

### Schema changes

Adding a column or a table: edit the class in `app/db.py` (`transform=True` does the rest). Renames, data changes, index changes and anything touching triggers need a numbered migration in `app/migrations/` instead; its docstring says how. Pending migrations run at startup, or by hand with `uv run python -m app.migrations [--db PATH]`.

### Secret scan

`scripts/secret_scan.sh` runs gitleaks with `.gitleaks.toml` over the git history.

## Admin

Orgs start unverified and their openings stay out of every deck until an admin approves them at `/admin/orgs` (EIN checked against ProPublica) or with `scripts/verify_org.py`. Reports, suspensions, org deactivation and unblocking are in `/admin/reports` and `/admin/blocks`. Nonprofits come from [ProPublica Nonprofit Explorer](https://projects.propublica.org/nonprofits/) (IRS public data): `scripts/import_orgs.py --city "Los Angeles" --state CA` imports a sample of Los Angeles ones, `--ein` imports one, and a coordinator asks for access at `/org/claim` (an admin approves at `/admin/claims`). A report from a volunteer names an org, so to remove one coordinator use `scripts/remove_coordinator.py`.

## License

MIT
