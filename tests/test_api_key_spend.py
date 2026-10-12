"""The Team page's "Spent last 30 days" column: `GET /orgs/{id}/api-keys/spend`, read from the rollup.

What these pin: an admin sees every key and the team's not-attributed line; a member sees only
their own keys; another team's spend never shows; nothing shows before the rollup has caught up;
and a page open is a small bounded read of the rollup, never the ledger or the audit table, however
large the team.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import event

from treg import crypto
from treg.application import key_spend_rollup as rollup
from treg.infra import db as infra_db
from treg.infra.db import session_maker
from treg.models import ApiKeySpendCursor, ApiKeySpendDay, Membership, Org, User
from treg.timeutil import utcnow_naive


def _h(token: str) -> dict[str, str]:
    return {"X-Treg-Token": token}


def _day(days_ago: int) -> str:
    return (utcnow_naive() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _row(org_id: int, days_ago: int, key_id: int, micro: int, endpoint: str = "exa.search", charges: int = 1):
    return ApiKeySpendDay(org_id=org_id, day=_day(days_ago), api_key_id=key_id, endpoint_id=endpoint,
                          spent_micro=micro, charges=charges, updated_at=utcnow_naive())


async def _add(*rows) -> None:
    async with session_maker() as db:
        db.add_all(rows)
        await db.commit()


async def _caught_up() -> None:
    now = utcnow_naive()
    await _add(ApiKeySpendCursor(id=rollup.CURSOR_ID, watermark_at=now, history_from=_day(30), caught_up_at=now))


async def _member(org_id: int, email: str, role: str = "member") -> str:
    token = crypto.new_token()
    async with session_maker() as db:
        user = User(email=email)
        db.add(user)
        await db.flush()
        db.add(Membership(user_id=user.id, org_id=org_id, role=role, token_hash=crypto.hash_token(token)))
        await db.commit()
    return token


@contextmanager
def _statements():
    seen: list[str] = []

    def record(conn, cursor, statement, *args):
        seen.append(statement.lower())
    engines = list({id(e.sync_engine): e.sync_engine for e in infra_db._engines}.values())
    for engine in engines:
        event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", record)


async def test_admin_sees_every_key_and_member_only_their_own(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    admin_key = (await clients.post(f"/orgs/{org_id}/api-keys", json={"name": "Admin CI"})).json()["id"]
    member_token = await _member(org_id, "spender@example.dev")
    member_key = (await clients.post(f"/orgs/{org_id}/api-keys", headers=_h(member_token),
                                     json={"name": "Member CI"})).json()["id"]
    async with session_maker() as db:
        other = Org(name="Other", slug="other-spend")
        db.add(other)
        await db.commit()
    await _add(
        _row(org_id, 0, admin_key, 2_000), _row(org_id, 3, admin_key, 500, "tavily.search"),
        _row(org_id, 1, member_key, 3_000, charges=2),
        _row(org_id, 2, rollup.NOT_ATTRIBUTED, 40),
        _row(org_id, 31, admin_key, 7_000),                # before the 30-day window
        _row(other.id, 0, admin_key, 100_000),             # another team's row under a colliding id
        _row(other.id, 0, rollup.NOT_ATTRIBUTED, 100_000),
    )

    before = (await clients.get(f"/orgs/{org_id}/api-keys/spend")).json()
    assert before["ready"] is False and before["keys"] == [] and before["unattributed"] is None

    await _caught_up()
    seen = (await clients.get(f"/orgs/{org_id}/api-keys/spend")).json()
    assert seen["ready"] and seen["days"] == 30 and seen["since"] == _day(29)
    assert seen["keys"] == [{"id": member_key, "spent_micro": 3_000, "charges": 2},
                            {"id": admin_key, "spent_micro": 2_500, "charges": 2}]
    assert seen["unattributed"] == {"spent_micro": 40, "charges": 1}

    own = (await clients.get(f"/orgs/{org_id}/api-keys/spend", headers=_h(member_token))).json()
    assert own["keys"] == [{"id": member_key, "spent_micro": 3_000, "charges": 2}]
    assert own["unattributed"] is None

    assert (await clients.get(f"/orgs/{other.id}/api-keys/spend")).status_code == 403


async def test_a_large_team_is_a_small_bounded_read_of_the_rollup(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    await _caught_up()
    keys, tools, days = 120, 6, 31
    await _add(*[_row(org_id, d, k, 10 + k, f"tool{t}.x")
                 for k in range(1, keys + 1) for t in range(tools) for d in range(days)])

    with _statements() as seen:
        got = (await clients.get(f"/orgs/{org_id}/api-keys/spend")).json()

    assert len(got["keys"]) == keys
    assert got["keys"][0] == {"id": keys, "spent_micro": (10 + keys) * tools * 30, "charges": tools * 30}
    touched = [s for s in seen if "apikeyspend" in s]
    assert len(touched) == 2      # the cursor, then one grouped read of the rollup
    assert not [s for s in seen if "ledgerentry" in s or "callrecord" in s]
