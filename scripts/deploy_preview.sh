#!/usr/bin/env bash
# Deploy the password-protected preview (CAUSEMATCH_ENV=preview) to Cloud Run.
#
#   scripts/deploy_preview.sh
#
# Settings come from the environment (defaults in brackets): PROJECT [causematch-la],
# REGION [us-west1], SERVICE [causematch-preview], SERVICE_ACCOUNT [causematch-preview@
# the project], SEED_SRC [the real-orgs folder in ~/.omc/reports: orgs.json, logos-raw/,
# photos-raw/], CONTACT_EMAIL [the privacy page's address], MIN_INSTANCES [1: keeps one
# instance warm so a demo never waits for a cold start and the data survives between visits;
# about $10-15 a month, and MIN_INSTANCES=0 scripts/deploy_preview.sh turns it back off],
# ADMIN_EMAILS [none: comma-separated addresses the preview makes admins at startup and at
# each reset; they sign in at /org/login, by email when the preview sends real email],
# STAGING_REAL_EMAIL [1: real email when production's mail secrets exist; 0 keeps the
# preview all fake].
#
# The seed is copied into ./preview-seed for the build only: git ignores it, .gcloudignore
# sends it, and it is removed when this script exits. The secrets PREVIEW_PASSWORD,
# SESSION_SECRET, PHONE_HASH_KEY, CERTIFICATE_KEY and TASKS_TOKEN must already be in Secret Manager, readable
# by the service account the service runs as.
#
# Real email: production never shares a key with the preview, with ONE exception, the
# owner's choice: the mail account. SMTP_USERNAME_PROD and SMTP_PASSWORD_PROD are passed as
# SMTP_USERNAME and SMTP_PASSWORD when both exist (checked with gcloud secrets describe;
# only one is refused) and STAGING_REAL_EMAIL isn't 0. The preview's service account needs
# secretAccessor on those two, granted by hand (else the deploy fails). So revoking the app
# password stops both, and both share the sender's daily allowance (Gmail's 500: the preview
# is capped at SMTP_MAX_PER_DAY=50, production at its 400). No other _PROD secret
# goes to the preview. With them the preview emails every address but the demo's for real,
# marked "[Staging]" (notify.StagingEmailer), and its CPU is always allocated
# (--no-cpu-throttling), since mail goes out after the response; without them it sends no
# email and shows each sign-in link on the page. CAUSEMATCH_FAKE_EMAIL=1 stays in both: it
# is what keeps the demo's addresses fake.
set -euo pipefail

PROJECT="${PROJECT:-causematch-la}"
REGION="${REGION:-us-west1}"
SERVICE="${SERVICE:-causematch-preview}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-causematch-preview@$PROJECT.iam.gserviceaccount.com}"
SEED_SRC="${SEED_SRC:-$HOME/.omc/reports/2026-10-15-causematch-design/real-orgs}"
CONTACT_EMAIL="${CONTACT_EMAIL:-hello@causematch.test}"
MIN_INSTANCES="${MIN_INSTANCES:-1}"
STAGING_REAL_EMAIL="${STAGING_REAL_EMAIL:-1}"
ADMIN_EMAILS="${ADMIN_EMAILS:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SEED="${ROOT:?}/preview-seed"
cd "$ROOT"

# PROJECT is an id anyone could have taken (causematch was): send the seed only to the
# project named Causematch. Unreachable or unknown: refused.
name="$(gcloud projects describe "$PROJECT" --format 'value(name)' 2>/dev/null)" || name=""
if [ "$name" != "Causematch" ]; then
	echo "project $PROJECT is not the one named Causematch (got '$name'): refusing" >&2
	exit 1
fi
has_secret() {
	gcloud secrets describe "$1" --project "$PROJECT" --format 'value(name)' >/dev/null 2>&1
}
secrets="PREVIEW_PASSWORD=PREVIEW_PASSWORD:latest,SESSION_SECRET=SESSION_SECRET:latest,PHONE_HASH_KEY=PHONE_HASH_KEY:latest,CERTIFICATE_KEY=CERTIFICATE_KEY:latest,TASKS_TOKEN=TASKS_TOKEN:latest"
cpu_flag=--cpu-throttling
mail_env=""
# Real email is optional, and half of SMTP refuses to start (config.smtp).
smtp=0
case "$STAGING_REAL_EMAIL" in
0) echo "STAGING_REAL_EMAIL=0: the preview sends no email" ;;
1)
	has_secret SMTP_USERNAME_PROD && smtp=$((smtp + 1))
	has_secret SMTP_PASSWORD_PROD && smtp=$((smtp + 2))
	;;
*)
	echo "STAGING_REAL_EMAIL must be 1 (the default) or 0" >&2
	exit 1
	;;
esac
case "$smtp" in
0) [ "$STAGING_REAL_EMAIL" = 0 ] || echo "no SMTP_*_PROD secrets: the preview sends no email" ;;
3)
	secrets="$secrets,SMTP_USERNAME=SMTP_USERNAME_PROD:latest,SMTP_PASSWORD=SMTP_PASSWORD_PROD:latest"
	cpu_flag=--no-cpu-throttling
	# Gmail's 500 a day is per account, and each deploy counts its own ceiling:
	# production's 400 plus these 50 stay under it
	mail_env=";SMTP_MAX_PER_DAY=50"
	echo "real email: $SERVICE_ACCOUNT needs access to SMTP_USERNAME_PROD and SMTP_PASSWORD_PROD"
	;;
*)
	echo "only one of SMTP_USERNAME_PROD and SMTP_PASSWORD_PROD exists: add the other or" \
		"delete it, or set STAGING_REAL_EMAIL=0" >&2
	exit 1
	;;
esac
# ; separates the env vars below (gcloud's ^;^ syntax): commas and @ appear in values.
case "$CONTACT_EMAIL$ADMIN_EMAILS" in
*";"*)
	echo "CONTACT_EMAIL and ADMIN_EMAILS may not contain ;" >&2
	exit 1
	;;
esac

for f in orgs.json logos-raw photos-raw; do
	[ -e "$SEED_SRC/$f" ] || {
		echo "$SEED_SRC/$f is missing" >&2
		exit 1
	}
done
# Real nonprofits' material never enters git (the repo is public).
if [ -n "$(git ls-files -- preview-seed)" ]; then
	echo "preview-seed/ is tracked by git: refusing to copy real org data into it" >&2
	exit 1
fi
if ! git check-ignore -q preview-seed/orgs.json; then
	echo "preview-seed/ is not ignored by git: refusing to copy real org data into it" >&2
	exit 1
fi

trap 'rm -rf -- "$SEED"' EXIT
rm -rf -- "$SEED"
mkdir "$SEED"
cp "$SEED_SRC/orgs.json" "$SEED/"
cp -R "$SEED_SRC/logos-raw" "$SEED_SRC/photos-raw" "$SEED/"

describe_url() {
	gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
		--format 'value(status.url)' 2>/dev/null || true
}

# The first deploy has no URL yet: a placeholder origin, replaced right after.
url="$(describe_url)"
gcloud run deploy "$SERVICE" --source . \
	--project "$PROJECT" --region "$REGION" \
	--service-account "$SERVICE_ACCOUNT" \
	--allow-unauthenticated \
	--min-instances "$MIN_INSTANCES" --max-instances 1 --cpu-boost "$cpu_flag" \
	--memory 1Gi --cpu 1 --concurrency 80 \
	--set-env-vars "^;^CAUSEMATCH_ENV=preview;PREVIEW_SEED_DIR=/app/preview-seed;TRUSTED_PROXY_HOPS=1;CAUSEMATCH_FAKE_SMS=1;CAUSEMATCH_FAKE_EMAIL=1;CAUSEMATCH_FAKE_OAUTH=1;CAUSEMATCH_DEMO_MATCHES=1;CONTACT_EMAIL=$CONTACT_EMAIL;ADMIN_EMAILS=$ADMIN_EMAILS$mail_env;PUBLIC_BASE_URL=${url:-https://placeholder.invalid}" \
	--set-secrets "$secrets"

deployed="$(describe_url)"
if [ -z "$deployed" ]; then
	echo "could not read the service URL" >&2
	exit 1
fi
if [ "$deployed" != "$url" ]; then
	gcloud run services update "$SERVICE" --project "$PROJECT" --region "$REGION" \
		--update-env-vars "PUBLIC_BASE_URL=$deployed"
fi

echo "Preview: $deployed  (open this URL: forms only work on the origin in PUBLIC_BASE_URL)"
echo "Password: gcloud secrets versions access latest --secret PREVIEW_PASSWORD --project $PROJECT"
echo "  (make one with: python -c 'import secrets; print(secrets.token_urlsafe(24))', URL-safe)"
echo "Link to send, by direct message (not a channel that unfurls links): $deployed/?access=<password>"
echo "Sign-in links: in the service's logs at startup, or /org/login with pat@demo.causematch.test"
