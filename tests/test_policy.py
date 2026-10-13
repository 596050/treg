"""Policy extension contract: loading, failure handling, and the signup_credit point end to end."""
import asyncio
import logging
from types import SimpleNamespace

import pytest
from sqlmodel import select

from conftest import verified_identity
from treg import policy
from treg.bootstrap import create_app
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import LedgerEntry, SignupPromoClaim, User

CTX = policy.SignupCreditContext(user_id=1, org_id=2, email="a.b+x@gmail.com", canonical_inbox="ab@gmail.com")


def _registry(handler) -> policy.PolicyRegistry:
    registry = policy.PolicyRegistry()
    registry.on_signup_credit(handler)
    return registry


async def test_no_handler_grants():
    assert await policy.PolicyRegistry().signup_credit(CTX) is policy.CreditDecision.GRANT


@pytest.mark.parametrize("decision", list(policy.CreditDecision))
async def test_handler_decides(decision):
    async def handler(ctx):
        assert ctx == CTX
        return decision
    assert await _registry(handler).signup_credit(CTX) is decision


async def test_slow_handler_defers(monkeypatch, caplog):
    monkeypatch.setattr(policy, "SIGNUP_CREDIT_BUDGET_S", 0.01)

    async def handler(ctx):
        await asyncio.sleep(1)
        return policy.CreditDecision.GRANT
    with caplog.at_level(logging.WARNING, logger="treg"):
        assert await _registry(handler).signup_credit(CTX) is policy.CreditDecision.DEFER
    assert "event=policy_failed point=signup_credit error=TimeoutError" in caplog.text


async def test_failing_handler_defers(caplog):
    async def handler(ctx):
        raise ValueError("boom")
    with caplog.at_level(logging.WARNING, logger="treg"):
        assert await _registry(handler).signup_credit(CTX) is policy.CreditDecision.DEFER
    assert "error=ValueError" in caplog.text
    assert "boom" not in caplog.text  # an extension's message may name its rule


async def test_unknown_outcome_defers():
    async def handler(ctx):
        return "grant"  # a plain string is not a CreditDecision
    assert await _registry(handler).signup_credit(CTX) is policy.CreditDecision.DEFER


def test_one_handler_per_point():
    async def handler(ctx):
        return policy.CreditDecision.GRANT
    registry = _registry(handler)
    with pytest.raises(RuntimeError, match="already registered"):
        registry.on_signup_credit(handler)


def _entry_points(monkeypatch, **extensions):
    points = [SimpleNamespace(name=name, load=lambda ext=ext: ext) for name, ext in extensions.items()]
    monkeypatch.setattr(policy, "entry_points",
                        lambda group: points if group == policy.ENTRY_POINT_GROUP else [])


class _Deferring:
    api_version = policy.POLICY_API_VERSION

    def __init__(self):
        self.app = None

    def install(self, app, registry):
        self.app = app

        async def handler(ctx):
            return policy.CreditDecision.DEFER
        registry.on_signup_credit(handler)


async def test_load_installs_extensions_into_a_fresh_registry(monkeypatch):
    monkeypatch.setattr(policy, "registry", policy.registry)
    extension = _Deferring()
    _entry_points(monkeypatch, guard=extension)
    app = object()
    assert policy.load_extensions(app, " guard ,") == ["guard"]
    assert extension.app is app
    assert await policy.registry.signup_credit(CTX) is policy.CreditDecision.DEFER
    _entry_points(monkeypatch)
    assert policy.load_extensions(app) == []
    assert await policy.registry.signup_credit(CTX) is policy.CreditDecision.GRANT


def test_load_refuses_another_api_version(monkeypatch):
    monkeypatch.setattr(policy, "registry", policy.registry)
    stale = _Deferring()
    stale.api_version = policy.POLICY_API_VERSION + 1
    _entry_points(monkeypatch, guard=stale)
    with pytest.raises(RuntimeError, match="targets policy API"):
        policy.load_extensions(object())


def test_create_app_refuses_a_missing_required_extension(monkeypatch):
    monkeypatch.setattr(policy, "registry", policy.registry)
    _entry_points(monkeypatch)
    monkeypatch.setenv("TREG_REQUIRED_EXTENSIONS", "guard")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="required extensions not installed: guard"):
            create_app("all")
        _entry_points(monkeypatch, guard=_Deferring())
        assert create_app("all").state.extensions == ["guard"]
    finally:
        get_settings.cache_clear()


async def _team(client, token, name):
    r = await client.post("/orgs", json={"name": name}, headers={"X-Treg-Token": token})
    assert r.status_code == 200, r.text
    return r.json()["org_id"]


async def _grants(org_id):
    async with session_maker() as db:
        return (await db.execute(select(LedgerEntry).where(
            LedgerEntry.org_id == org_id, LedgerEntry.kind == "grant"))).scalars().all()


async def test_deferred_credit_stays_claimable(clients, monkeypatch, caplog):
    seen = []
    decision = policy.CreditDecision.DEFER

    async def handler(ctx):
        seen.append(ctx)
        return decision
    monkeypatch.setattr(policy, "registry", _registry(handler))
    token = await verified_identity(clients, "Farm.Er+one@GMail.com")

    with caplog.at_level(logging.INFO, logger="treg"):
        first = await _team(clients, token, "deferred")
    assert await _grants(first) == []
    assert "event=signup_credit_deferred" in caplog.text
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "farm.er+one@gmail.com"))).scalar_one()
        assert user.signup_promo_available
        assert (await db.execute(select(SignupPromoClaim).where(
            SignupPromoClaim.user_id == user.id))).scalars().all() == []
    assert seen == [policy.SignupCreditContext(
        user_id=user.id, org_id=first, email="farm.er+one@gmail.com", canonical_inbox="farmer@gmail.com")]

    decision = policy.CreditDecision.GRANT
    second = await _team(clients, token, "granted")
    assert len(await _grants(second)) == 1
    # Once claimed, the point is never asked again for this user.
    await _team(clients, token, "third")
    assert len(seen) == 2


async def test_ineligible_users_never_reach_the_point(clients, monkeypatch):
    seen = []

    async def handler(ctx):
        seen.append(ctx)
        return policy.CreditDecision.GRANT
    monkeypatch.setattr(policy, "registry", _registry(handler))
    legacy = await clients.post("/users", json={"email": "legacy@example.org"})
    assert legacy.status_code == 200
    await _team(clients, legacy.json()["token"], "legacy-team")
    assert seen == []


async def test_failing_extension_defers_and_signup_still_succeeds(clients, monkeypatch):
    async def handler(ctx):
        raise RuntimeError("down")
    monkeypatch.setattr(policy, "registry", _registry(handler))
    token = await verified_identity(clients, "down@example.org")
    assert await _grants(await _team(clients, token, "still-works")) == []
