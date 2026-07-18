# syntax=docker/dockerfile:1
#
# Multi-stage, multi-arch build. Verified to build natively for linux/arm64
# (Oracle Cloud Ampere) and linux/amd64. Uses BuildKit's automatic
# TARGETARCH so the same Dockerfile works whether you `docker build` directly
# on the ARM host (recommended) or cross-build with `docker buildx`.

ARG PYTHON_VERSION=3.11
ARG SUPERCRONIC_VERSION=v0.2.47

# -----------------------------------------------------------------------------
# Stage 1: fetch & checksum-verify the supercronic binary for the target arch.
# supercronic runs our schedule as PID 1 and streams job stdout/stderr
# straight to the container's stdout — no syslog, no log files to tail.
# -----------------------------------------------------------------------------
FROM debian:bookworm-slim AS supercronic
ARG TARGETARCH
ARG SUPERCRONIC_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "${TARGETARCH}" in \
        amd64) SHA1="712d2ece75da6f6e530192a151488578153e4e96" ;; \
        arm64) SHA1="93323899ddca3f1198f1796a4bf4418ed1e7982e" ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    BIN="supercronic-linux-${TARGETARCH}"; \
    URL="https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/${BIN}"; \
    curl -fsSL -o /tmp/supercronic "${URL}"; \
    echo "${SHA1}  /tmp/supercronic" | sha1sum -c -; \
    chmod +x /tmp/supercronic; \
    mv /tmp/supercronic /usr/local/bin/supercronic

# -----------------------------------------------------------------------------
# Stage 2: build the Python virtualenv. numpy/pandas/scipy/statsmodels all
# ship manylinux aarch64 wheels for 3.11, so build-essential is a safety net
# (source builds on ARM take a while but won't break the image).
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

# -----------------------------------------------------------------------------
# Stage 3: runtime image — no compilers, no curl, non-root user.
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 stockarb \
    && useradd --uid 1000 --gid stockarb --shell /bin/bash --create-home stockarb

COPY --from=supercronic /usr/local/bin/supercronic /usr/local/bin/supercronic
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY app/ /app/app/
COPY crontab /app/crontab
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/data \
    && chown -R stockarb:stockarb /app

USER stockarb

ENTRYPOINT ["/app/entrypoint.sh"]
