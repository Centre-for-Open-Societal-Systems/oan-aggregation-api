from openg2p_fastapi_common.context import dbengine
from sqlalchemy.ext.asyncio import async_sessionmaker


def async_session() -> async_sessionmaker:
    """Session factory bound to the shared async engine from fastapi-common.

    A new session per unit of work; the engine (and its connection pool) is
    shared across the process, so the app stays stateless and horizontally
    scalable — every pod talks to the same Postgres.
    """
    return async_sessionmaker(dbengine.get(), expire_on_commit=False)
