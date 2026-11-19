# Agri Stack use-case composite API.
# Build context: the composite/ folder of the agri-stack repo, e.g.
#   docker build -f composite/docker/agri-composite-api/Dockerfile composite
FROM python:3.12-slim-bookworm

# External OpenG2P framework ref. CI resolves it to a commit SHA (pins in
# .github/workflows/build-publish.yml). Crypto, including the partner-mgmt key
# store, is in openg2p_fastapi_common.crypto.
ARG FASTAPI_COMMON_REF=develop
# iam-core (the console's staff login), develop line. CI resolves it to a commit SHA.
ARG IAM_CORE_REF=develop

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# git is needed only to pip-install the framework and iam-core from GitHub;
# removed in the same layer. All other dependencies ship prebuilt wheels.
COPY backend /src/agri-composite
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && pip install \
      "git+https://github.com/openg2p/openg2p-fastapi-common@${FASTAPI_COMMON_REF}#subdirectory=openg2p-fastapi-common" \
      "git+https://github.com/openg2p/iam@${IAM_CORE_REF}#subdirectory=iam-core" \
 && pip install /src/agri-composite \
 && apt-get purge -y git && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* /src/agri-composite

# Sample use cases baked in; a cluster mounts its own over AGRI_COMPOSITE_USE_CASES_DIR.
COPY use-cases /app/use-cases

# Non-root (uid/gid 1001, matching the chart's securityContext). The app writes
# nothing at runtime; gunicorn's worker heartbeat goes to /tmp (an emptyDir
# when the root filesystem is read-only).
RUN groupadd -g 1001 openg2p \
 && useradd -u 1001 -g openg2p -m -d /home/openg2p -s /usr/sbin/nologin openg2p \
 && chown -R 1001:1001 /app
USER 1001
ENV HOME=/home/openg2p

ENV AGRI_COMPOSITE_APP_HOST=0.0.0.0 \
    AGRI_COMPOSITE_APP_PORT=8000 \
    AGRI_COMPOSITE_NO_OF_WORKERS=2 \
    AGRI_COMPOSITE_USE_CASES_DIR=/app/use-cases

EXPOSE 8000

# No migrate step: the console's one table is created at start-up. exec: gunicorn gets the signals.
CMD ["sh", "-c", "exec gunicorn openg2p_agri_composite.main:app --workers ${AGRI_COMPOSITE_NO_OF_WORKERS} --worker-class uvicorn.workers.UvicornWorker --worker-tmp-dir /tmp --no-control-socket --bind ${AGRI_COMPOSITE_APP_HOST}:${AGRI_COMPOSITE_APP_PORT} --graceful-timeout 20 --keep-alive 5 --log-level info"]
