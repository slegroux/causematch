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
# STAGING_REAL_EMAIL [1: real email when the mail secrets below exist; 0 keeps the preview
# all fake], CPU_ALWAYS_ALLOCATED [0: --cpu-throttling, so mail sent after the response can
# arrive late, as in production today; 1: --no-cpu-throttling, billed all month].
#
# Fake email, so the demo's addresses never get real email (and, without the mail secrets
# below, no address does); no SMS, fake or real, so its pages say "email" where production
# does (notify.texts_on: the beta collects no phone numbers).
#
# The seed is copied into ./preview-seed for the build only: git ignores it, .gcloudignore
# sends it, and it is removed when this script exits. The secrets PREVIEW_PASSWORD,
# SESSION_SECRET, PHONE_HASH_KEY, CERTIFICATE_KEY and TASKS_TOKEN must already be in Secret Manager, readable
# by the service account the service runs as.
#
# Real email (unless STAGING_REAL_EMAIL=0): the preview's own SMTP_USERNAME_STAGING and
# SMTP_PASSWORD_STAGING when both exist; else, the ONE exception to "production never
# shares a key with the preview", by the owner's choice, production's SMTP_USERNAME_PROD
# and SMTP_PASSWORD_PROD. Passed as SMTP_USERNAME and SMTP_PASSWORD; each pair both or
# neither (checked with gcloud secrets describe; one alone is refused). The preview's
# service account needs secretAccessor on the pair, granted by hand (else the deploy
# fails). With production's pair, revoking the app password stops both, both share the
# sender's daily allowance (Gmail's 500: the preview is capped at 50, notify.STAGING_MAX_
# PER_DAY, production at its 400), and the preview can read the account's Sent folder.
# No other _PROD secret goes to the preview. With a pair the preview emails sign-in links
# to every address but the demo's, and everything else only to addresses that signed in
# with one (notify.StagingEmailer), marked "[Staging]"; without one it sends no email and
# shows each sign-in link on the page. CAUSEMATCH_FAKE_EMAIL=1 stays in both: it is what
# keeps the demo's addresses fake.
set -euo pipefail

PROJECT="${PROJECT:-causematch-la}"
REGION="${REGION:-us-west1}"
SERVICE="${SERVICE:-causematch-preview}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-causematch-preview@$PROJECT.iam.gserviceaccount.com}"
SEED_SRC="${SEED_SRC:-$HOME/.omc/reports/2026-10-15-causematch-design/real-orgs}"
CONTACT_EMAIL="${CONTACT_EMAIL:-hello@causematch.test}"
MIN_INSTANCES="${MIN_INSTANCES:-1}"
STAGING_REAL_EMAIL="${STAGING_REAL_EMAIL:-1}"
CPU_ALWAYS_ALLOCATED="${CPU_ALWAYS_ALLOCATED:-0}"
ADMIN_EMAILS="${ADMIN_EMAILS:-}"

# Production's SERVICE or SERVICE_ACCOUNT left in the shell would redeploy production as a
# preview: its env replaced (no LITESTREAM_BUCKET), its database the demo seed's, and its
# sessions read against that database. So SERVICE is a preview's name or nothing:
# causematch-preview, or causematch-preview-<lowercase letters, digits, hyphens>; never a
# fully qualified one. And never production's service account.
if ! [[ "$SERVICE" =~ ^causematch-preview(-[a-z0-9-]+)?$ ]]; then
	echo "SERVICE ($SERVICE) is not causematch-preview or causematch-preview-<name>:" \
		"refusing" >&2
	exit 1
fi
case "$SERVICE_ACCOUNT" in
causematch-prod@*)
	echo "SERVICE_ACCOUNT ($SERVICE_ACCOUNT) names production: refusing" >&2
	exit 1
	;;
esac

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
# Explicit either way, so a deploy turns a previous always-allocated one back off.
case "$CPU_ALWAYS_ALLOCATED" in
0) cpu_flag=--cpu-throttling ;;
1) cpu_flag=--no-cpu-throttling ;;
*)
	echo "CPU_ALWAYS_ALLOCATED must be 0 (the default) or 1" >&2
	exit 1
	;;
esac
mail_env=""
# Real email is optional, and half of SMTP refuses to start (config.smtp). pair NAME: how
# many of SMTP_USERNAME_NAME (1) and SMTP_PASSWORD_NAME (2) exist.
pair() {
	n=0
	has_secret "SMTP_USERNAME_$1" && n=$((n + 1))
	has_secret "SMTP_PASSWORD_$1" && n=$((n + 2))
	echo "$n"
}
mail=""
case "$STAGING_REAL_EMAIL" in
0) echo "STAGING_REAL_EMAIL=0: the preview sends no email" ;;
1)
	for name in STAGING PROD; do
		case "$(pair "$name")" in
		0) ;;
		3)
			mail="$name"
			break
			;;
		*)
			echo "only one of SMTP_USERNAME_$name and SMTP_PASSWORD_$name exists: add" \
				"the other or delete it, or set STAGING_REAL_EMAIL=0" >&2
			exit 1
			;;
		esac
	done
	[ -n "$mail" ] || echo "no SMTP_*_STAGING or SMTP_*_PROD secrets: the preview sends no email"
	;;
*)
	echo "STAGING_REAL_EMAIL must be 1 (the default) or 0" >&2
	exit 1
	;;
esac
if [ -n "$mail" ]; then
	secrets="$secrets,SMTP_USERNAME=SMTP_USERNAME_$mail:latest,SMTP_PASSWORD=SMTP_PASSWORD_$mail:latest"
	# Gmail's 500 a day is per account, and each deploy counts its own ceiling: production's
	# 400 plus these 50 stay under it (the app caps a preview at 50 too)
	mail_env=";SMTP_MAX_PER_DAY=50"
	echo "real email: $SERVICE_ACCOUNT needs access to SMTP_USERNAME_$mail and SMTP_PASSWORD_$mail"
fi
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
	--set-env-vars "^;^CAUSEMATCH_ENV=preview;PREVIEW_SEED_DIR=/app/preview-seed;TRUSTED_PROXY_HOPS=1;CAUSEMATCH_FAKE_EMAIL=1;CAUSEMATCH_FAKE_OAUTH=1;CAUSEMATCH_DEMO_MATCHES=1;CONTACT_EMAIL=$CONTACT_EMAIL;ADMIN_EMAILS=$ADMIN_EMAILS$mail_env;PUBLIC_BASE_URL=${url:-https://placeholder.invalid}" \
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
