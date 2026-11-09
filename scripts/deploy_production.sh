#!/usr/bin/env bash
# Deploy production (CAUSEMATCH_ENV=production) to Cloud Run, on its *.run.app URL.
#
#   CPU_ALWAYS_ALLOCATED=1 scripts/deploy_production.sh    (or =0: see below)
#
# Settings come from the environment (defaults in brackets): PROJECT [causematch-la],
# REGION [us-west1], SERVICE [causematch], SERVICE_ACCOUNT [causematch-prod@ the project],
# LITESTREAM_BUCKET [the project id + -litestream], CONTACT_EMAIL [the privacy page's
# address; required], GMAIL_PURGE [unset; daily once the mailbox purge runs, OPERATIONS
# step 6], ADMIN_EMAILS [unset; comma-separated addresses startup makes admins, docs/ADMIN_GUIDE.md
# Becoming an admin], CPU_ALWAYS_ALLOCATED [required, 1 or 0].
#
# One instance, always: --min-instances 1 --max-instances 1. The database is SQLite on the
# instance's disk, streamed to the bucket by Litestream (entrypoint.sh) and restored from it
# when an instance starts. Two instances would be two databases, each overwriting the other's
# replica, and the rate limits and Gmail's daily count are kept in that file too. Never raise
# --max-instances while on SQLite. --concurrency 80 is Cloud Run's default made explicit:
# open chat websockets count against it, and it must stay at or under uvicorn's
# --limit-concurrency 200 (entrypoint.sh), or uvicorn answers 503 before Cloud Run queues.
#
# CPU_ALWAYS_ALLOCATED=1 adds --no-cpu-throttling: sign-in links and notices go out after the
# response, and Cloud Run throttles the CPU then unless it is always allocated. It bills the
# instance for its whole life (with one kept warm, all month): a cost to choose at deploy, so
# the script asks for it every time (docs/OPERATIONS.md, Email through Gmail, step 7).
#
# Secrets come from Secret Manager under their own _PROD names, so production never shares a
# key with the preview: SESSION_SECRET_PROD, PHONE_HASH_KEY_PROD, CERTIFICATE_KEY_PROD,
# TASKS_TOKEN_PROD, SMTP_USERNAME_PROD and SMTP_PASSWORD_PROD, each readable by the service
# account. GOOGLE_CLIENT_ID_PROD and GOOGLE_CLIENT_SECRET_PROD are optional: passed when both
# exist (checked with gcloud secrets describe), refused when only one does. Nothing is seeded.
set -euo pipefail

PROJECT="${PROJECT:-causematch-la}"
REGION="${REGION:-us-west1}"
SERVICE="${SERVICE:-causematch}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-causematch-prod@$PROJECT.iam.gserviceaccount.com}"
LITESTREAM_BUCKET="${LITESTREAM_BUCKET:-$PROJECT-litestream}"
CONTACT_EMAIL="${CONTACT_EMAIL:-}"
GMAIL_PURGE="${GMAIL_PURGE:-}"
ADMIN_EMAILS="${ADMIN_EMAILS:-}"
CPU_ALWAYS_ALLOCATED="${CPU_ALWAYS_ALLOCATED:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# PROJECT is an id anyone could have taken (causematch was): deploy only to the project
# named Causematch. Unreachable or unknown: refused.
name="$(gcloud projects describe "$PROJECT" --format 'value(name)' 2>/dev/null)" || name=""
if [ "$name" != "Causematch" ]; then
	echo "project $PROJECT is not the one named Causematch (got '$name'): refusing" >&2
	exit 1
fi
case "$CPU_ALWAYS_ALLOCATED" in
1) cpu_flag=--no-cpu-throttling ;;
0) cpu_flag=--cpu-throttling ;;
*)
	echo "set CPU_ALWAYS_ALLOCATED=1 (emails go out reliably, billed all month) or 0" \
		"(cheaper, some links and notices late or lost): see the header" >&2
	exit 1
	;;
esac
if [ -z "$CONTACT_EMAIL" ]; then
	echo "set CONTACT_EMAIL, the address on /privacy" >&2
	exit 1
fi
# ; separates the env vars below (gcloud's ^;^ syntax): commas and @ appear in values.
case "$CONTACT_EMAIL$GMAIL_PURGE$LITESTREAM_BUCKET$ADMIN_EMAILS" in
*";"*)
	echo "CONTACT_EMAIL, GMAIL_PURGE, LITESTREAM_BUCKET and ADMIN_EMAILS may not contain ;" >&2
	exit 1
	;;
esac
# A preview deploy that was killed can leave the real nonprofits' seed here, and
# .gcloudignore would send it into production's image.
if [ -e preview-seed ]; then
	echo "preview-seed/ exists (a preview deploy's leftover): remove it first" >&2
	exit 1
fi

has_secret() {
	gcloud secrets describe "$1" --project "$PROJECT" --format 'value(name)' >/dev/null 2>&1
}
secrets=""
missing=""
for s in SESSION_SECRET PHONE_HASH_KEY CERTIFICATE_KEY TASKS_TOKEN SMTP_USERNAME SMTP_PASSWORD; do
	if has_secret "${s}_PROD"; then
		secrets="$secrets,$s=${s}_PROD:latest"
	else
		missing="$missing ${s}_PROD"
	fi
done
if [ -n "$missing" ]; then
	echo "missing from Secret Manager in $PROJECT:$missing" >&2
	exit 1
fi
# Google sign-in is optional, and half of it refuses to start (config.google_oauth).
google=0
has_secret GOOGLE_CLIENT_ID_PROD && google=$((google + 1))
has_secret GOOGLE_CLIENT_SECRET_PROD && google=$((google + 2))
case "$google" in
0) echo "no GOOGLE_*_PROD secrets: deploying without Google sign-in" ;;
3) secrets="$secrets,GOOGLE_CLIENT_ID=GOOGLE_CLIENT_ID_PROD:latest,GOOGLE_CLIENT_SECRET=GOOGLE_CLIENT_SECRET_PROD:latest" ;;
*)
	echo "only one of GOOGLE_CLIENT_ID_PROD and GOOGLE_CLIENT_SECRET_PROD exists: add" \
		"the other or delete it" >&2
	exit 1
	;;
esac
secrets="${secrets#,}"

env_vars="CAUSEMATCH_ENV=production;TRUSTED_PROXY_HOPS=1;LITESTREAM_BUCKET=$LITESTREAM_BUCKET;CONTACT_EMAIL=$CONTACT_EMAIL"
if [ -n "$GMAIL_PURGE" ]; then
	env_vars="$env_vars;GMAIL_PURGE=$GMAIL_PURGE"
fi
if [ -n "$ADMIN_EMAILS" ]; then
	env_vars="$env_vars;ADMIN_EMAILS=$ADMIN_EMAILS"
fi

describe_url() {
	gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
		--format 'value(status.url)' 2>/dev/null || true
}

# The first deploy has no URL yet: a placeholder origin, replaced right after.
# --set-env-vars and --set-secrets replace the whole set each time, so nothing set by hand
# survives a deploy (CAUSEMATCH_COLLECT_PHONE, the fakes, PREVIEW_*: never set here).
url="$(describe_url)"
gcloud run deploy "$SERVICE" --source . \
	--project "$PROJECT" --region "$REGION" \
	--service-account "$SERVICE_ACCOUNT" \
	--allow-unauthenticated \
	--min-instances 1 --max-instances 1 --cpu-boost "$cpu_flag" \
	--memory 1Gi --cpu 1 --concurrency 80 \
	--set-env-vars "^;^$env_vars;PUBLIC_BASE_URL=${url:-https://placeholder.invalid}" \
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

echo "Production: $deployed"
echo "Check: curl -s -o /dev/null -w '%{http_code}' $deployed/login   (expect 200)"
if [ "$google" = 3 ]; then
	echo "Google: the OAuth client's redirect URI must be $deployed/auth/google/callback"
fi
