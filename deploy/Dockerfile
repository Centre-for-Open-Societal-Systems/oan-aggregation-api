FROM python:3.12-slim-bookworm

# External OpenG2P framework ref. Override via build-arg / CI input.
# develop: 1.2.0 moved crypto out of utils (same pin the CM build uses).
ARG FASTAPI_COMMON_REF=develop

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    "git+https://github.com/openg2p/openg2p-fastapi-common@${FASTAPI_COMMON_REF}#subdirectory=openg2p-fastapi-common"

# Build context = repo root.
COPY backend /src/backend
RUN pip install --no-cache-dir /src/backend

ENV PYTHONUNBUFFERED=1
ENV AGGREGATION_LAYER_APP_HOST=0.0.0.0
ENV AGGREGATION_LAYER_APP_PORT=8100
ENV AGGREGATION_LAYER_NO_OF_WORKERS=2

# Migrate, then serve. `&&` so a failed migrate crashes the pod instead of
# serving against a missing table.
#
# One signing key for all workers: set AGGREGATION_LAYER_SIGNING_P12_PATH (or
# _PRIVATE_KEY_PEM). Without it each worker generates its own ephemeral key and
# the registries reject every hop signed by a key PM does not hold.
CMD python3 -m openg2p_aggregation_layer.main migrate && \
    gunicorn "openg2p_aggregation_layer.main:app" \
      --workers ${AGGREGATION_LAYER_NO_OF_WORKERS} \
      --worker-class uvicorn.workers.UvicornWorker \
      --bind ${AGGREGATION_LAYER_APP_HOST}:${AGGREGATION_LAYER_APP_PORT} \
      --log-level info
