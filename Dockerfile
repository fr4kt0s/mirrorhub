# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---------- builder: hashed, locked dependencies into a venv ----------
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.txt

# ---------- base: app code, non-root user, no tools ----------
FROM ${PYTHON_IMAGE} AS base
ARG PUID=1000
ARG PGID=1000
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp
RUN groupadd --gid "${PGID}" mirror \
 && useradd --uid "${PUID}" --gid "${PGID}" --no-create-home --shell /usr/sbin/nologin mirror \
 && mkdir -p /var/lib/mirrorhub /gnupg /data /export \
 && chown mirror:mirror /var/lib/mirrorhub /gnupg
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY src /app/src
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh
USER mirror
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# ---------- apt-base: root + apt tuning (retries, optional proxy) ----------
FROM base AS apt-base
ARG APT_PROXY=""
USER root
RUN printf 'Acquire::Retries "3";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' \
        > /etc/apt/apt.conf.d/80mirrorhub \
 && if [ -n "${APT_PROXY}" ]; then \
        printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' \
            "${APT_PROXY}" "${APT_PROXY}" >> /etc/apt/apt.conf.d/80mirrorhub; \
    fi

# ---------- web: UI/API/SSE/scheduler; gnupg + keyring packages for key import ----------
FROM apt-base AS web
RUN apt-get update \
 && apt-get install -y --no-install-recommends gnupg gpgv ca-certificates \
 && apt-get install -y --no-install-recommends debian-archive-keyring \
 && (apt-get install -y --no-install-recommends ubuntu-keyring \
     || echo "WARNING: ubuntu-keyring not available in this base image; skipping") \
 && rm -rf /var/lib/apt/lists/*
USER mirror
ENV MIRRORHUB_ROLE=web
EXPOSE 8000
CMD ["uvicorn", "mirrorhub.web.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]

# ---------- worker-debmirror ----------
FROM apt-base AS worker-debmirror
RUN apt-get update \
 && apt-get install -y --no-install-recommends debmirror rsync gpgv gnupg ca-certificates \
        xz-utils bzip2 \
 && rm -rf /var/lib/apt/lists/*
USER mirror
ENV MIRRORHUB_ROLE=worker MIRRORHUB_TOOL=debmirror
CMD ["python", "-m", "mirrorhub.worker.main", "--tool", "debmirror"]

# ---------- worker-rsync ----------
FROM apt-base AS worker-rsync
RUN apt-get update \
 && apt-get install -y --no-install-recommends rsync ca-certificates \
 && rm -rf /var/lib/apt/lists/*
USER mirror
ENV MIRRORHUB_ROLE=worker MIRRORHUB_TOOL=rsync
CMD ["python", "-m", "mirrorhub.worker.main", "--tool", "rsync"]

# ---------- worker-wget ----------
FROM apt-base AS worker-wget
RUN apt-get update \
 && apt-get install -y --no-install-recommends wget ca-certificates \
 && rm -rf /var/lib/apt/lists/*
USER mirror
ENV MIRRORHUB_ROLE=worker MIRRORHUB_TOOL=wget
CMD ["python", "-m", "mirrorhub.worker.main", "--tool", "wget"]

# ---------- worker-export: tar, zstd, gnupg for signing ----------
FROM apt-base AS worker-export
RUN apt-get update \
 && apt-get install -y --no-install-recommends tar zstd gnupg coreutils \
 && rm -rf /var/lib/apt/lists/*
USER mirror
ENV MIRRORHUB_ROLE=worker MIRRORHUB_TOOL=export
CMD ["python", "-m", "mirrorhub.worker.main", "--tool", "export"]