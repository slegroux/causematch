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
- `PHONE_HASH_KEY`: keys the hash kept of a deleted account's phone when a block, report or suspension must outlive it

## Admin

Orgs start unverified and their openings stay out of every deck until an admin approves them at `/admin/orgs` (EIN checked against ProPublica) or with `scripts/verify_org.py`. Reports, suspensions, org deactivation and unblocking are in `/admin/reports` and `/admin/blocks`. A report from a volunteer names an org, so to remove one coordinator use `scripts/remove_coordinator.py`.

## License

MIT
