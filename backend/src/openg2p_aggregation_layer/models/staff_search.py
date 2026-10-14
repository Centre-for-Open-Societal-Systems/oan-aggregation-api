"""Audit of the staff search: who looked whom up, where, why, and what came back.

The staff search runs without the subject's consent, so nothing in the Consent
Manager records it; the registries' staff-apis see only this service's service
account. These tables are therefore the only place that names the staff user.

- ``staff_search_audit``: one row per search that passed the role check,
  including those that found nothing or failed. Its id is the ``searchId``.
- ``staff_tab_view_audit``: one row per tab opened from a search, linked by
  ``search_id``.
"""
from typing import Optional

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseORMModelWithId


class StaffSearchAudit(BaseORMModelWithId):
    __tablename__ = "staff_search_audit"

    # ── who ─────────────────────────────────────────────────────────────────
    staff_subject: Mapped[str] = mapped_column(String(255), index=True)   # token sub
    staff_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    staff_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # The roles that let the call through (the staff_search_roles held).
    staff_roles: Mapped[list] = mapped_column(JSONB, default=list)

    # ── whom, where, why ────────────────────────────────────────────────────
    foundational_id: Mapped[str] = mapped_column(String(255), index=True)
    registries: Mapped[list] = mapped_column(JSONB, default=list)
    purpose: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # ── what came back ──────────────────────────────────────────────────────
    # {registryCode: {"status": found|not_found|error, "records": n, "error": ...,
    #                 "found": [{"registerId": ..., "recordId": ...}]}}
    # "found" is also what a tab request is checked against.
    outcome: Mapped[dict] = mapped_column(JSONB, default=dict)


class StaffTabViewAudit(BaseORMModelWithId):
    __tablename__ = "staff_tab_view_audit"

    search_id: Mapped[str] = mapped_column(String(64), index=True)
    staff_subject: Mapped[str] = mapped_column(String(255), index=True)
    staff_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    registry_code: Mapped[str] = mapped_column(String(128))
    register_id: Mapped[str] = mapped_column(String(128))
    record_id: Mapped[str] = mapped_column(String(255))
    tab_id: Mapped[str] = mapped_column(String(255))

    # {"status": ok|error, "sections": n, "records": n, "error": ...}
    outcome: Mapped[dict] = mapped_column(JSONB, default=dict)
