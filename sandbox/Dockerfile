# Cloud Run service "sandbox": executes creator strategy scripts (SPEC §2, §5.9, §10).
#
# Build from the repo root (BuildKit, for COPY --chmod):
#   DOCKER_BUILDKIT=1 docker build -f sandbox/Dockerfile -t sandbox .
#
# The container holds ONLY the sandbox package + the stdlib-only helpers it imports
# (app/errors.py, app/logging.py). No app.config, no DB drivers, no SDKs, no secrets baked in.
#
# ISOLATION — the in-process restrictions in app/sandbox/runner.py are defense in depth only.
# The boundary is this service's deployment:
#   gcloud run deploy sandbox \
#     --image=… --region=asia-southeast1 \
#     --execution-environment=gen1           # gVisor user-space kernel between scripts and the host
#     --service-account=sandbox@PROJECT.iam.gserviceaccount.com   # has NO IAM roles at all
#     --no-allow-unauthenticated --ingress=internal                # invoker: api + executor SAs only
#     --network=sandbox-vpc --subnet=sandbox-subnet --vpc-egress=all-traffic
#         # ↑ all egress goes into a VPC with NO Cloud NAT, no default internet route and a
#         #   deny-all egress firewall rule → scripts (and this process) cannot reach the internet,
#         #   Hyperliquid, the DB, or Google APIs. The metadata server is still reachable on Cloud Run,
#         #   which is why the service account must have no roles.
#     --set-secrets=SANDBOX_SHARED_SECRET=sandbox-shared-secret:latest
#     --concurrency=1 --cpu=2 --memory=2Gi --timeout=300 --max-instances=10 --no-cpu-throttling
#         # ↑ ONE request per instance (REVIEW_WEB_INFRA M5): two creators' scripts never share an instance
#
# Read-only root FS: Cloud Run has no read-only-rootfs flag (its writable layer is in-memory).
# Mitigations: the app is owned by root and not writable by the runtime user; script processes run with
# RLIMIT_FSIZE=0 (cannot write files), cwd in a throw-away temp dir, and no code is ever loaded from
# a writable path. Where a read-only root FS is available (GKE / docker run --read-only
# --tmpfs /tmp:rw,noexec,nosuid,size=64m), use it.
# pin-image: python:3.12-slim
ARG PYTHON_IMAGE=python:3.12-slim@sha256:PIN_ME
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv \
    PORT=8080

# Process model (REVIEW_WEB_INFRA M5): the service PARENT runs as root inside the gVisor sandbox for ONE reason —
# to drop every script child to its own fresh uid/gid (app/sandbox/runner.py _child_identity, uids 200000+, no
# supplementary groups). The parent never executes creator code; it reads SANDBOX_SHARED_SECRET once, deletes it from
# its environment and sets PR_SET_DUMPABLE=0 (service.py). Children: non-root per-run uid (RLIMIT_NPROC=0 enforced),
# empty environment, no_new_privs, non-dumpable, rlimits. A per-run uid means an escaped child cannot read the
# parent's /proc/<pid>/environ|mem, nor signal/ptrace another run. The sandbox SA has no IAM roles, the VPC has no
# egress and DNS answers NXDOMAIN (bootstrap.sh). The `sandbox` user below is kept for local `docker run --user`.
RUN groupadd --system --gid 10001 sandbox \
 && useradd --system --uid 10001 --gid sandbox --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin sandbox \
 && rm -rf /var/lib/apt/lists/* /root/.cache

WORKDIR /srv
# Root-owned, world-readable, not writable by the runtime user.
COPY --chown=root:root --chmod=0444 backend/app/__init__.py backend/app/errors.py backend/app/logging.py /srv/app/
COPY --chown=root:root --chmod=0444 backend/app/sandbox/*.py /srv/app/sandbox/
RUN chmod 0555 /srv /srv/app /srv/app/sandbox \
 && python3 -m compileall -q /srv/app

USER 0:0
EXPOSE 8080
CMD ["python3", "-m", "app.sandbox.service"]
