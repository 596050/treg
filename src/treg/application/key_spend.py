"""Billed spend per API key, read from the daily rollup (`ApiKeySpendDay`).

The rollup is the ledger's settle entries, attributed to a key through the audit row and folded by
`treg-worker spend rollup` (`application/key_spend_rollup.py`, which explains the attribution). The
pages read only the rollup: never the ledger, never `callrecord`, so a page open costs a few
grouped rows per key and day however much the team spent.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ApiKey, ApiKeySpendCursor, ApiKeySpendDay
from .key_spend_rollup import CURSOR_ID, NOT_ATTRIBUTED

WINDOW_DAYS = 30   # the Team page's "Spent" column: today and the 29 days before it


async def history(db: AsyncSession) -> dict:
    """`{"ready", "history_from"}`. Not ready until the first backfill has caught up once: before
    that the rollup holds part of the window, and a partial number would read as a real one."""
    state = await db.get(ApiKeySpendCursor, CURSOR_ID)
    return {"ready": bool(state and state.caught_up_at), "history_from": state.history_from if state else None}


def first_day(today: date, days: int) -> date:
    """Today (UTC) plus the `days - 1` before it."""
    return today - timedelta(days=days - 1)


async def spend_by_key(db: AsyncSession, org_id: int, since: date, *, membership_id: int | None = None) -> dict:
    """`{"keys": {key_id: {"spent_micro", "charges"}}, "unattributed": {...}}` from `since` (a UTC
    day) to today. Given `membership_id`, only that member's keys, and no unattributed line."""
    query = (select(ApiKeySpendDay.api_key_id, func.sum(ApiKeySpendDay.spent_micro), func.sum(ApiKeySpendDay.charges))
             .where(ApiKeySpendDay.org_id == org_id, ApiKeySpendDay.day >= since.isoformat())
             .group_by(ApiKeySpendDay.api_key_id))
    if membership_id is not None:
        query = query.where(ApiKeySpendDay.api_key_id.in_(
            select(ApiKey.id).where(ApiKey.org_id == org_id, ApiKey.membership_id == membership_id)))
    lines = {int(key_id): {"spent_micro": int(spent or 0), "charges": int(charges or 0)}
             for key_id, spent, charges in (await db.execute(query)).all()}
    none = lines.pop(NOT_ATTRIBUTED, {"spent_micro": 0, "charges": 0})
    return {"keys": lines, "unattributed": none if membership_id is None else None}
