# syntax=docker/dockerfile:1
#
# 声画织场 (Reel Loom) — single-instance media service image.
#
# The image is read-only at runtime: all mutable state lives under /data,
# which must be a writable volume owned (or chowned at first start) by the
# unprivileged service account.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS build
ARG PIP_INDEX_URL
WORKDIR /wheelhouse
COPY pyproject.toml README.md ./
COPY reel_loom ./reel_loom
# Resolve every dependency (PyAV ships self-contained manylinux wheels) and
# build the project wheel without installing into the build stage.
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheelhouse/wheels .

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    LOOM_DATA=/data \
    LOOM_FFMPEG=/usr/bin/ffmpeg \
    LOOM_HOST=0.0.0.0 \
    LOOM_PORT=8729 \
    LOOM_WORKERS=2 \
    LOOM_RENDER_TIMEOUT=600 \
    LOOM_SHUTDOWN_TIMEOUT=30

# Real media toolchain for service-side encoding; wget backs the
# capability-gated container HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        tini \
        wget \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 loom \
    && useradd --system --uid 10001 --gid 10001 --home-dir /opt/reel-loom --shell /usr/sbin/nologin loom

# Install everything from pre-built wheels: no compiler or PyPI access in
# the runtime stage.
COPY --from=build /wheelhouse/wheels /tmp/wheels
RUN python -m pip install --no-cache-dir --no-index --find-links /tmp/wheels reel-loom \
    && rm -rf /tmp/wheels

WORKDIR /opt/reel-loom
# The package comes from the wheel above; copy only the runtime entrypoint.
COPY --chown=loom:loom deploy/entrypoint.py /usr/local/bin/loom-entrypoint
RUN chmod 0555 /usr/local/bin/loom-entrypoint

# Single writable data boundary: media blobs, scratch, SQLite.
RUN install -d -o loom -g loom -m 0750 /data
VOLUME ["/data"]

USER loom:loom
EXPOSE 8729

# Container-level health is a real capability check, not a port probe: the
# endpoint only answers 200 after the startup encode/decode preflight and
# reports 503 while starting or draining.  Kept shell-only so the same probe
# works for overridden entrypoints that carry no application code.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=12 \
    CMD sh -c 'wget -q -O - http://127.0.0.1:8729/loom/v1/health 2>/dev/null | grep -q "\"phase\": \"ready\"" || exit 1'

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/loom-entrypoint"]
