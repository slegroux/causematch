# The preview's and production's image: scripts/deploy_preview.sh and
# scripts/deploy_production.sh have Cloud Build build it from this folder (.gcloudignore says
# what is sent). entrypoint.sh runs the Procfile's uvicorn, under Litestream when
# LITESTREAM_BUCKET is set (production), as a user that owns nothing but data/.
# Base images pinned by digest (multi-arch index), looked up from the registries on
# 2026-10-15: python:3.12-slim and uv 0.12.0, and on 2026-11-07: litestream 0.5.17. To
# update, read the new index digest (Docker-Content-Digest of the tag's manifest) and
# change both the tag and the digest.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

COPY --from=ghcr.io/astral-sh/uv:0.12.0@sha256:606e70c71c852d03f611b1e56a195d08648507018a7057fab82c4974c4eae105 /uv /usr/local/bin/uv
# Litestream from its official image (the binary alone), pinned by digest like uv: one
# layer, no curl or tar in this image and no download step to check, where the release
# tarball would need both. Release 0.5.17 (2026-08-31), not 0.5.18 (4 days old when pinned).
COPY --from=litestream/litestream:0.5.17@sha256:4b02b9859a6b6b4087d8b8944e15f7e984bd7957cba322bbeee38b0e27b9656a /usr/local/bin/litestream /usr/local/bin/litestream

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

COPY main.py entrypoint.sh litestream.yml ./
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

# The Procfile's uvicorn, without uv run (no syncing as this user at startup), under
# Litestream when LITESTREAM_BUCKET is set
CMD ["/app/entrypoint.sh"]
