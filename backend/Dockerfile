# aijalon.trade backend image — ONE image, three roles (SPEC §2):
#   api       (default CMD)                                   SERVICE_ROLE=api
#   executor  same CMD, env SERVICE_ROLE=executor             (infra/gcp/run/executor.service.yaml)
#   migrate   command: python scripts/migrate.py              (infra/gcp/run/migrate.job.yaml)
# The sandbox has its own image (sandbox/Dockerfile).
#
# Build (context = backend/):   docker build -f backend/Dockerfile backend
# Supply chain:
#   * base image pinned by digest (`make pin` resolves it; the PIN_ME placeholder makes the build fail);
#   * Python deps installed ONLY from requirements.lock with --require-hashes (`make lock`), unless
#     ALLOW_UNLOCKED=1 (CI smoke build before the lock exists; the deploy workflow never sets it);
#   * no compilers / build tools in the final stage; non-root uid 10001; read-only code owned by root.

# pin-image: python:3.12-slim
ARG PYTHON_IMAGE=python:3.12-slim@sha256:PIN_ME

# ---------------------------------------------------------------------------------------------- build
FROM ${PYTHON_IMAGE} AS build
ARG ALLOW_UNLOCKED=0
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
# requirements.lock may not exist yet (first CI run before `make lock`); the glob keeps COPY from failing
COPY requirements.txt requirements*.lock ./
RUN python -m venv /opt/venv \
 && if [ -f requirements.lock ]; then \
      /opt/venv/bin/pip install --require-hashes --only-binary=:all: --no-deps -r requirements.lock; \
    elif [ "${ALLOW_UNLOCKED}" = "1" ]; then \
      echo "WARNING: no requirements.lock — unhashed install (ALLOW_UNLOCKED=1, never for production)"; \
      /opt/venv/bin/pip install --only-binary=:all: -r requirements.txt; \
    else \
      echo "ERROR: backend/requirements.lock missing — run 'make lock' and commit it" >&2; exit 1; \
    fi \
 && /opt/venv/bin/pip check \
 && (/opt/venv/bin/pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true)
# ^ pip/setuptools are not needed at runtime: removed from the venv (smaller attack surface)

# ---------------------------------------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="aijalon-backend" \
      org.opencontainers.image.source="https://github.com/joztern755/quantscriptmarket"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/srv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SERVICE_ROLE=api \
    PORT=8080
# Non-root runtime user; no home, no shell. Remove the system pip too (nothing installs at runtime).
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && (python -m pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true) \
 && rm -rf /root/.cache /var/lib/apt/lists/* /tmp/*
COPY --from=build /opt/venv /opt/venv
WORKDIR /srv
# Code is owned by root and not writable by the app user. .dockerignore allow-lists app/, migrations/, scripts/.
COPY --chown=root:root . /srv
# Legal documents (repo-root legal/) via the named build context `legal` (docker build --build-context legal=legal).
# The API hashes these exact bytes to verify what users consented to (must equal what the web serves).
COPY --from=legal --chown=root:root . /srv/legal
ENV LEGAL_DIR=/srv/legal
RUN python -m compileall -q /srv/app /srv/scripts \
 && chmod -R a-w /srv
USER 10001:10001
EXPOSE 8080
# `--proxy-headers`: Cloud Run's front end is the only peer. Client IP for rate limits / audit comes from
# CF-Connecting-IP, trusted only after the app has validated X-Edge-Auth (app/security, edge check).
CMD ["uvicorn", "app.api.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--no-server-header", "--no-date-header", \
     "--timeout-keep-alive", "620", "--timeout-graceful-shutdown", "8"]
