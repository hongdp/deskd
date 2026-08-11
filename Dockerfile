# syntax=docker/dockerfile:1.7
FROM python:3.13.14-bookworm@sha256:8b9a8b28d9cc221c6ab5d40e9cfcd99429959f6a8f5171612a99147975ab043f AS git_source

FROM python:3.13.14-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8 AS runtime

ARG DESKD_UID=65532
ARG DESKD_GID=65532

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/opt/deskd/src \
    DESKD_HOME=/var/lib/deskd \
    DESKD_DB=/var/lib/deskd/deskd.db \
    DESKD_HOST=0.0.0.0 \
    DESKD_PORT=8000 \
    DESKD_CONTROL_API_ONLY=1

# Git is copied from the matching pinned full Bookworm image.  This keeps the
# runtime apt-free: old Docker/seccomp hosts can make dpkg/lzma fail with false
# ENOMEM even when the machine has ample memory.  deskd uses only local built-in
# Git operations; network protocols and credential helpers are disabled by the
# workspace broker.
COPY --from=git_source /usr/bin/git /usr/bin/git
COPY --from=git_source /usr/lib/git-core /usr/lib/git-core
COPY --from=git_source /usr/share/git-core /usr/share/git-core

RUN groupadd --gid "${DESKD_GID}" deskd \
    && useradd --uid "${DESKD_UID}" --gid "${DESKD_GID}" \
       --home-dir /var/lib/deskd --no-create-home --shell /usr/sbin/nologin deskd

WORKDIR /opt/deskd
COPY requirements-container.in requirements-container.lock /tmp/
RUN python -m pip install --progress-bar off --only-binary=:all: \
      --require-hashes \
      --requirement /tmp/requirements-container.lock \
    && rm -f /tmp/requirements-container.in /tmp/requirements-container.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY scripts/deskd-container /usr/local/bin/deskd
# Installing the project through PEP 517 launches a build-isolation child
# interpreter. Docker 20.10's old default seccomp profile rejects that spawn on
# current Python with EPERM. Runtime dependencies come only from the
# hash-locked wheel set above; run the copied, immutable source tree directly.
RUN chmod 0755 /usr/local/bin/deskd \
    && mkdir -p /var/lib/deskd \
    && chown -R deskd:deskd /var/lib/deskd /opt/deskd

USER deskd:deskd
VOLUME ["/var/lib/deskd"]
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2))['ok']"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/deskd"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
