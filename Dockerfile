# The preview's image: scripts/deploy_preview.sh has Cloud Build build it from this folder
# (.gcloudignore says what is sent). Runs the Procfile's uvicorn, as a user that owns
# nothing but data/.
# Base images pinned by digest (multi-arch index), looked up from the registries on
# 2026-10-15: python:3.12-slim and uv 0.12.0. To update, read the new index digest
# (Docker-Content-Digest of the tag's manifest) and change both the tag and the digest.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

COPY --from=ghcr.io/astral-sh/uv:0.12.0@sha256:606e70c71c852d03f611b1e56a195d08648507018a7057fab82c4974c4eae105 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

# Dependencies first, so a code change reuses this layer. Pillow's wheels bring their
# own libjpeg, libpng and libwebp: nothing to apt-get.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY main.py ./
COPY app ./app
COPY static ./static
# preview-seed/ (orgs.json, logos-raw/, photos-raw/) is there only when the deploy script
# put it there: .python-version always matches, so the COPY never fails on its own.
COPY .python-version preview-see[d] ./preview-seed/
RUN rm ./preview-seed/.python-version \
    && useradd --system --uid 10001 --no-create-home causematch \
    && mkdir data \
    && chown causematch data

# Code and the venv stay root's: the app can't change them. The photo worker
# (python -I app/photo_worker.py) is the venv's python on a file it can read.
USER causematch

# The Procfile's flags, without uv run (no syncing as this user at startup)
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --ws-max-size 65536 --limit-concurrency 200 --timeout-keep-alive 15 --timeout-graceful-shutdown 8 --no-proxy-headers"]
