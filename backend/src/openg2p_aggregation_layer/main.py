#!/usr/bin/env python3

# ruff: noqa: I001

from openg2p_aggregation_layer.app import Initializer
from openg2p_fastapi_common.ping import PingInitializer

initializer = Initializer()
PingInitializer()

app = initializer.return_app()

# The Kafka producer and the aggregation consumers are started from
# Initializer.fastapi_app_startup in app.py, not from here: the base
# Initializer passes its own lifespan to FastAPI, which makes any
# ``@app.on_event`` handler registered on this module a no-op.

if __name__ == "__main__":
    initializer.main()
