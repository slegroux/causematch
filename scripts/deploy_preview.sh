#!/usr/bin/env bash
# Deploy the password-protected preview (CAUSEMATCH_ENV=preview) to Cloud Run.
#
#   scripts/deploy_preview.sh
#
# Settings come from the environment (defaults in brackets): PROJECT [causematch],
# REGION [us-west1], SERVICE [causematch-preview], SEED_SRC [the real-orgs folder in
# ~/.omc/reports: orgs.json, logos-raw/, photos-raw/], CONTACT_EMAIL [the privacy page's
# address].
#
# The seed is copied into ./preview-seed for the build only: git ignores it, .gcloudignore
# sends it, and it is removed when this script exits. The secrets PREVIEW_PASSWORD,
# SESSION_SECRET, PHONE_HASH_KEY and TASKS_TOKEN must already be in Secret Manager.
set -euo pipefail

PROJECT="${PROJECT:-causematch}"
REGION="${REGION:-us-west1}"
SERVICE="${SERVICE:-causematch-preview}"
SEED_SRC="${SEED_SRC:-$HOME/.omc/reports/2026-10-15-causematch-design/real-orgs}"
CONTACT_EMAIL="${CONTACT_EMAIL:-hello@causematch.test}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SEED="${ROOT:?}/preview-seed"
cd "$ROOT"

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
	--allow-unauthenticated \
	--min-instances 0 --max-instances 1 \
	--memory 1Gi --cpu 1 --concurrency 80 \
	--set-env-vars "CAUSEMATCH_ENV=preview,PREVIEW_SEED_DIR=/app/preview-seed,TRUSTED_PROXY_HOPS=1,CAUSEMATCH_FAKE_SMS=1,CAUSEMATCH_FAKE_EMAIL=1,CONTACT_EMAIL=$CONTACT_EMAIL,PUBLIC_BASE_URL=${url:-https://placeholder.invalid}" \
	--set-secrets "PREVIEW_PASSWORD=PREVIEW_PASSWORD:latest,SESSION_SECRET=SESSION_SECRET:latest,PHONE_HASH_KEY=PHONE_HASH_KEY:latest,TASKS_TOKEN=TASKS_TOKEN:latest"

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
echo "Sign-in links: in the service's logs at startup, or /org/login with pat@demo.causematch.test"
