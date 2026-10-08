import uuid
from datetime import datetime, timezone
from typing import Optional

from openg2p_fastapi_common.models import BaseORMModel
from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BaseORMModelWithId(BaseORMModel):
    __abstract__ = True

    id: Mapped[str] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def __init__(self, **kwargs):
        # Populate the primary key eagerly so dependent rows built in the same
        # unit-of-work can reference this row's id before the session flushes.
        if kwargs.get("id") is None:
            kwargs["id"] = str(uuid.uuid4())
        super().__init__(**kwargs)
