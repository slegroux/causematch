#!/bin/sh
# The image's start (Dockerfile CMD): the Procfile's uvicorn, without uv run.
#
# With LITESTREAM_BUCKET set (production): first, when the database file is missing (every
# new Cloud Run instance), restore it from gs://$LITESTREAM_BUCKET/causematch.db. No replica
# yet (the very first start) is fine, and the app creates the database. Any other restore
# error stops the start, so an instance never serves an empty database while a replica it
# could not read exists: the previous revision keeps serving. Then Litestream runs uvicorn
# (-exec), streams every change to the bucket, passes SIGTERM on to uvicorn and syncs once
# more when it exits. uvicorn's 8 s graceful shutdown leaves about 2 s of Cloud Run's 10.
#
# Without it (the preview, a local docker run): plain uvicorn, the database lives and dies
# with the container. Cloud Run without it is refused unless CAUSEMATCH_ENV=preview: the app
# counts any other Cloud Run service as production (config.is_production), and its data
# would go with the instance.
set -eu

DB="${CAUSEMATCH_DB:-/app/data/causematch.db}"
CONFIG="$(dirname "$0")/litestream.yml"

# The Procfile's flags (tests/test_security.py checks them against main.UVICORN)
set -- uvicorn main:app --host 0.0.0.0 --port "${PORT:-8080}" --ws-max-size 65536 \
	--limit-concurrency 200 --timeout-keep-alive 15 --timeout-graceful-shutdown 8 \
	--no-proxy-headers

if [ -z "${LITESTREAM_BUCKET:-}" ]; then
	if [ -n "${K_SERVICE:-}" ] && [ "${CAUSEMATCH_ENV:-}" != "preview" ]; then
		echo "production on Cloud Run needs LITESTREAM_BUCKET: refusing to keep the" \
			"only copy of the database in the container" >&2
		exit 1
	fi
	exec "$@"
fi

export LITESTREAM_DB="$DB"
if [ ! -e "$DB" ]; then
	litestream restore -config "$CONFIG" -if-replica-exists "$DB"
fi
# -exec takes one string, split on spaces: none of the arguments has one.
exec litestream replicate -config "$CONFIG" -exec "$*"
