"""The composite's call log, for the console's Activity page.

One row per authenticated partner query: when, which use case, which partner, the
HTTP status and outcome, the duration, each source's status and the ID of the
consent used (as the exchange Consent Manager named it). Never the subject, the
consent's content or any data. The Audit Manager still gets its events (it is the record
for forensics); this table only exists because the Audit Manager has no read API.

Off when no database is configured. A query never waits on it: ``record`` puts the
row on a bounded in-memory queue and one writer task per worker inserts the queue
in batches (one session and commit per batch). Maintenance (creating the table,
deleting rows past the retention period) runs in its own loop; on Postgres an
advisory lock lets one worker of all pods do it at a time.
"""

import asyncio
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import JSON, DateTime, Integer, String, delete, func, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_logger = logging.getLogger("agri_composite.activity")

OUTCOMES = ("success", "denied", "failure")
QUEUE_MAX = 10000  # rows waiting per worker; beyond this, rows are dropped (and counted)
BATCH_MAX = 500
COUNT_CAP = 10000  # the Activity page shows "10000+" rather than counting the whole table
MAINTENANCE_INTERVAL_SECONDS = 3600
PURGE_INTERVAL = timedelta(hours=24)
_LOCK_ID = 0x41C0_0001  # pg advisory lock: one maintenance run across workers and pods


class _Base(DeclarativeBase):
    pass


class ActivityRow(_Base):
    __tablename__ = "composite_activity"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    request_id: Mapped[str] = mapped_column(String(64))
    use_case: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    # The use case's name without "@major", for the "every major" filter (an equality, so indexed).
    use_case_name: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    partner_id: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    http_status: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(20), index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(200))
    duration_ms: Mapped[int] = mapped_column(Integer)
    sources: Mapped[Dict[str, str]] = mapped_column(JSON, default=dict)
    consent_id: Mapped[Optional[str]] = mapped_column(String(200))


# Added after the first release: created on tables made before it (Postgres).
_UPGRADES = (
    "ALTER TABLE composite_activity ADD COLUMN IF NOT EXISTS use_case_name VARCHAR(200)",
    "CREATE INDEX IF NOT EXISTS ix_composite_activity_use_case_name ON composite_activity (use_case_name)",
    "UPDATE composite_activity SET use_case_name = split_part(use_case, '@', 1) "
    "WHERE use_case_name IS NULL AND use_case IS NOT NULL",
    "ALTER TABLE composite_activity ADD COLUMN IF NOT EXISTS consent_id VARCHAR(200)",
)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:  # SQLite returns naive datetimes
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clip(value: Optional[str], size: int = 200) -> Optional[str]:
    return value[:size] if isinstance(value, str) else value


class ActivityStore:
    """``session_maker``: an async sessionmaker, or None (recording off)."""

    def __init__(self, session_maker=None, engine=None, retention_days: int = 90):
        self._session_maker = session_maker
        self._engine = engine
        self.retention_days = retention_days
        self.ready = False  # the table exists
        self.dropped = 0
        self._queue: Optional[asyncio.Queue] = None
        self._writer: Optional[asyncio.Task] = None
        self._maintenance: Optional[asyncio.Task] = None
        self._last_purge: Optional[datetime] = None

    @property
    def enabled(self) -> bool:
        return self._session_maker is not None

    @property
    def _postgres(self) -> bool:
        return self._engine is not None and self._engine.dialect.name == "postgresql"

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self, maintenance: bool = True) -> None:
        """Create the table (retried by the maintenance loop until it works), start the writer."""
        if not self.enabled:
            return
        await self.ensure_table()
        if self._writer is None:
            self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
            self._writer = asyncio.get_running_loop().create_task(self._write_loop())
        if maintenance and self._maintenance is None:
            self._maintenance = asyncio.get_running_loop().create_task(self._maintenance_loop())

    async def stop(self) -> None:
        if self._maintenance:
            self._maintenance.cancel()
            self._maintenance = None
        await self.drain()
        if self._writer:
            self._writer.cancel()
            self._writer = None

    async def ensure_table(self) -> bool:
        if self.ready or self._engine is None:
            return self.ready
        try:
            async with self._engine.begin() as conn:
                if self._postgres:
                    # Serialise creation across workers and pods (concurrent CREATE TABLEs collide).
                    await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_ID})
                await conn.run_sync(_Base.metadata.create_all)
                if self._postgres:
                    for statement in _UPGRADES:
                        await conn.execute(text(statement))
            self.ready = True
        except Exception as e:
            _logger.warning("Call log table not ready yet (%r); retrying in the maintenance loop", e)
        return self.ready

    # ── writing ──────────────────────────────────────────────────────────────

    def record(
        self, *, request_id: str, use_case: Optional[str], partner_id: Optional[str], http_status: int,
        outcome: str, reason: Optional[str], duration_ms: int, sources: Dict[str, str],
        consent_id: Optional[str] = None,
    ) -> None:
        if not self.enabled or self._queue is None:
            return
        row = {
            "id": str(uuid.uuid4()), "at": datetime.now(timezone.utc), "request_id": _clip(request_id, 64),
            "use_case": _clip(use_case), "use_case_name": _clip(use_case.split("@", 1)[0]) if use_case else None,
            "partner_id": _clip(partner_id), "http_status": http_status, "outcome": outcome,
            "reason": _clip(reason), "duration_ms": duration_ms, "sources": dict(sources),
            "consent_id": _clip(consent_id),
        }
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped % 1000 == 1:
                _logger.warning("Call log queue full; %d rows dropped so far", self.dropped)

    async def _write_loop(self) -> None:
        # Takes what is queued (up to BATCH_MAX) and inserts it in one commit: under load the
        # queue fills while a batch is written, so batches grow; when quiet, rows go at once.
        while True:
            batch = [await self._queue.get()]
            while len(batch) < BATCH_MAX:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await self._insert(batch)
            for _ in batch:
                self._queue.task_done()

    async def _insert(self, batch: List[Dict[str, Any]]) -> None:
        if not self.ready and not await self.ensure_table():
            _logger.warning("Call log table missing; %d rows not written", len(batch))
            return
        try:
            async with self._session_maker() as session:
                session.add_all([ActivityRow(**row) for row in batch])
                await session.commit()
        except Exception as e:
            _logger.warning("%d call log rows not written: %r", len(batch), e)

    async def drain(self) -> None:
        """Wait until every queued row is written (shutdown and tests)."""
        if self._queue is not None and self._writer is not None:
            await self._queue.join()

    # ── maintenance ──────────────────────────────────────────────────────────

    async def _maintenance_loop(self) -> None:
        # Spread the first run so a rollout's workers do not all start at once.
        await asyncio.sleep(random.uniform(30, 300))
        while True:
            try:
                await self.maintain()
            except Exception:
                _logger.warning("Call log maintenance failed", exc_info=True)
            await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)

    async def maintain(self) -> int:
        """Create the table if still missing; purge once a day. Returns the rows deleted."""
        if not await self.ensure_table():
            return 0
        now = datetime.now(timezone.utc)
        if self._last_purge and now - self._last_purge < PURGE_INTERVAL:
            return 0
        self._last_purge = now
        return await self.purge()

    async def purge(self) -> int:
        if not self.enabled or self.retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        async with self._session_maker() as session:
            if self._postgres:
                got = (await session.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _LOCK_ID})).scalar()
                if not got:
                    return 0  # another worker or pod is purging
            result = await session.execute(delete(ActivityRow).where(ActivityRow.at < cutoff))
            await session.commit()
            deleted = result.rowcount or 0
        if deleted:
            _logger.info("Call log: %d rows older than %d days deleted", deleted, self.retention_days)
        return deleted

    # ── reading ──────────────────────────────────────────────────────────────

    async def query(
        self, *, limit: int = 50, offset: int = 0, partner: Optional[str] = None,
        use_case: Optional[str] = None, outcome: Optional[str] = None,
    ) -> Tuple[int, bool, List[Dict[str, Any]]]:
        """(total, total_is_capped, items). The total stops counting at COUNT_CAP."""
        conditions = []
        if partner:
            conditions.append(ActivityRow.partner_id == partner)
        if use_case:
            # "loan-profile@1" is one major; "loan-profile" every major.
            conditions.append(ActivityRow.use_case == use_case if "@" in use_case
                              else ActivityRow.use_case_name == use_case)
        if outcome:
            conditions.append(ActivityRow.outcome == outcome)
        async with self._session_maker() as session:
            capped = select(ActivityRow.id).where(*conditions).limit(COUNT_CAP + 1).subquery()
            total = (await session.execute(select(func.count()).select_from(capped))).scalar() or 0
            rows = (await session.execute(
                select(ActivityRow).where(*conditions).order_by(ActivityRow.at.desc()).offset(offset).limit(limit)
            )).scalars().all()
        items = [
            {"at": _iso(r.at), "request_id": r.request_id, "use_case": r.use_case, "partner_id": r.partner_id,
             "http_status": r.http_status, "outcome": r.outcome, "reason": r.reason, "duration_ms": r.duration_ms,
             "sources": r.sources or {}, "consent_id": r.consent_id}
            for r in rows
        ]
        return min(total, COUNT_CAP), total > COUNT_CAP, items

    async def partners(self, limit: int = 500) -> List[str]:
        """The partner IDs (senders) in the log, sorted: the console's partner filter."""
        async with self._session_maker() as session:
            rows = (await session.execute(
                select(ActivityRow.partner_id).where(ActivityRow.partner_id.is_not(None)).distinct()
                .order_by(ActivityRow.partner_id).limit(limit)
            )).scalars().all()
        return list(rows)

    async def summary(self, hours: int = 24) -> Dict[str, int]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        async with self._session_maker() as session:
            calls = (await session.execute(
                select(func.count()).select_from(ActivityRow).where(ActivityRow.at >= since))).scalar() or 0
            failed = (await session.execute(
                select(func.count()).select_from(ActivityRow)
                .where(ActivityRow.at >= since, ActivityRow.outcome != "success"))).scalar() or 0
        return {"calls": calls, "failed": failed}
