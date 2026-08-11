# syntax=docker/dockerfile:1.7
FROM python:3.13.14-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8 AS runtime

ARG DESKD_UID=65532
ARG DESKD_GID=65532

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DESKD_HOME=/var/lib/deskd \
    DESKD_DB=/var/lib/deskd/deskd.db \
    DESKD_HOST=0.0.0.0 \
    DESKD_PORT=8000 \
    DESKD_CONTROL_API_ONLY=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y git tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${DESKD_GID}" deskd \
    && useradd --uid "${DESKD_UID}" --gid "${DESKD_GID}" \
       --home-dir /var/lib/deskd --no-create-home --shell /usr/sbin/nologin deskd

WORKDIR /opt/deskd
COPY requirements-container.in requirements-container.lock /tmp/
RUN python -m pip install --require-hashes \
      --requirement /tmp/requirements-container.lock \
    && rm -f /tmp/requirements-container.in /tmp/requirements-container.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-deps --no-build-isolation . \
    && mkdir -p /var/lib/deskd \
    && chown -R deskd:deskd /var/lib/deskd /opt/deskd

USER deskd:deskd
VOLUME ["/var/lib/deskd"]
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2))['ok']"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["deskd", "serve", "--host", "0.0.0.0", "--port", "8000"]
