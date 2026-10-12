"""Several treg-owned accounts behind one provider (`TREG_PLATFORM_ACCOUNTS`, application/call/keyslots.py):
platform calls spread across them, objects stay on the account that created them, and every
account's credential is masked."""

from __future__ import annotations

import json

import httpx
import pytest
from httpx import AsyncClient

from treg import api as A, oauth_providers
from treg.application import asynctasks as task_app
from treg.application.call import keyslots
from treg.application.call.evidence import _secret_renderings
from treg.application.call.types import GatewayFailed, ResolutionFailed, UpstreamResponse
from treg.config import PRIMARY_SLOT, Settings, get_settings
from treg.infra.upstream.limiter import limiter
from treg.models import AsyncTaskRecord, Tool
from treg.timeutil import utcnow_naive

from test_marketplace_call import EP, _dropleads_response, platform_on  # noqa: F401

TIKHUB_B = "PLATFORM-TIKHUB-KEY-B"
ACCOUNTS = {"tikhub": {"b": {"platform_key_tikhub": TIKHUB_B}}}


@pytest.fixture
def two_tikhub_accounts(platform_on, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_ACCOUNTS", json.dumps(ACCOUNTS))
    get_settings.cache_clear()


def _settings(**env) -> Settings:
    return Settings(_env_file=None, **env)


# ---- configuration ------------------------------------------------------------------------------

def test_the_primary_account_comes_first_and_labels_name_the_rest():
    s = _settings(platform_key_tomba="T1", platform_key_tomba_secret="S1", platform_accounts=json.dumps(
        {"tomba": {"b": {"platform_key_tomba": "T2", "platform_key_tomba_secret": "S2"}}}))
    slots = s.platform_slots("tomba")
    assert [x.slot for x in slots] == [PRIMARY_SLOT, "b"]
    assert s.platform_credential("platform_key_tomba_secret", "tomba/b") == "S2"
    assert s.platform_credential("platform_key_tomba_secret", None) == "S1", "the primary is the fields"
    assert s.platform_credential("platform_key_tomba", "tomba/gone") == "", "never another account's"
    assert set(s.platform_credential_values("platform_key_tomba")) == {"T1", "T2"}


def test_extra_accounts_without_a_primary_key_serve_nothing():
    s = _settings(platform_accounts=json.dumps({"tikhub": {"b": {"platform_key_tikhub": "X"}}}))
    assert s.platform_slots("tikhub") == ()


@pytest.mark.parametrize("raw", [
    "[]",
    '{"tikhub": [{"platform_key_tikhub": "x"}]}',
    '{"tikhub": {"primary": {"platform_key_tikhub": "x"}}}',
    '{"tikhub": {"B": {"platform_key_tikhub": "x"}}}',
    '{"tikhub": {"b": {"token": "x"}}}',
    '{"tikhub": {"b": {"platform_key_tikhub": ""}}}',
    '{"tikhub": {"b": {"platform_key_hunter": "x"}}}',
])
def test_a_malformed_accounts_value_refuses_to_start(raw):
    with pytest.raises(ValueError):
        _settings(platform_accounts=raw)


@pytest.mark.parametrize("accounts, refused", [
    ({"tomba": {"b": {"platform_key_tomba": "T2", "platform_key_tomba_secret": "S2"}}}, None),
    ({"tomba": {"b": {"platform_key_tomba": "T2"}}}, "platform_key_tomba_secret"),
    ({"nosuchprovider": {"b": {"platform_key_nosuchprovider": "Y"}}}, "no platform key"),
])
def test_an_account_the_call_path_cannot_use_refuses_to_start(monkeypatch, accounts, refused):
    monkeypatch.setenv("TREG_PLATFORM_ACCOUNTS", json.dumps(accounts))
    get_settings.cache_clear()
    try:
        if refused is None:
            keyslots.check_accounts()
        else:
            with pytest.raises(RuntimeError, match=refused):
                keyslots.check_accounts()
    finally:
        get_settings.cache_clear()


# ---- choosing an account ------------------------------------------------------------------------

def test_an_owner_pins_its_account_and_a_retired_one_is_refused(two_tikhub_accounts):
    def choose(**kw):
        return keyslots.choose("tikhub", EP, **kw)

    assert choose(owners=("b",)) == "b"
    assert choose(owners=(None,)) == PRIMARY_SLOT, "a row from before pools is the primary's"
    with pytest.raises(ResolutionFailed) as gone:
        choose(owners=("c",))
    assert gone.value.status_code == 410 and "nothing was charged" in str(gone.value.detail)
    with pytest.raises(ResolutionFailed):
        choose(owners=("b", None))


def test_an_idempotency_key_always_lands_on_the_same_account(two_tikhub_accounts):
    picks = {keyslots.choose("tikhub", EP, idempotency_key=f"1\x1fk{i}") for i in range(40)}
    assert picks == {PRIMARY_SLOT, "b"}, "keys spread across accounts"
    for i in range(5):
        key = f"1\x1fretry-{i}"
        assert len({keyslots.choose("tikhub", EP, idempotency_key=key) for _ in range(5)}) == 1


def test_the_primary_account_keeps_the_providers_own_binding_and_bucket(two_tikhub_accounts):
    from treg.domain.capacity import view as view_mod
    provider = oauth_providers.get("tikhub")
    assert all("account" not in b for b in oauth_providers.platform_bindings(provider, PRIMARY_SLOT))
    assert view_mod.view.rate_bucket("tikhub", EP, PRIMARY_SLOT)[0] == "tikhub"
    assert view_mod.view.rate_bucket("tikhub", EP, "b")[0] == "tikhub#b"


async def test_platform_calls_spread_across_the_accounts(clients: AsyncClient, two_tikhub_accounts,
                                                         monkeypatch, posthog_events):
    seen: list[str] = []

    def upstream(request):
        seen.append(request.headers["authorization"])
        return _dropleads_response(200, {"comments": []})

    async with AsyncClient(transport=httpx.MockTransport(upstream)) as vendor:
        monkeypatch.setattr(A.app.state, "http", vendor)
        for i in range(4):
            r = await clients.get(f"/call/{EP}?aweme_id={i}")
            assert r.status_code == 200, r.text
    assert sorted(seen) == sorted(["Bearer PLATFORM-TIKHUB-KEY", f"Bearer {TIKHUB_B}"] * 2)
    slots = [e["properties"]["key_slot"] for e in await posthog_events()]
    assert sorted(slots) == sorted([PRIMARY_SLOT, PRIMARY_SLOT, "b", "b"])


async def test_a_busy_account_hands_the_next_call_to_an_idle_one(two_tikhub_accounts):
    from treg.domain.capacity import view as view_mod
    await limiter.acquire(*view_mod.view.rate_bucket("tikhub", EP, PRIMARY_SLOT))
    for _ in range(4):  # the primary's token is spent; "b" has one
        assert keyslots.choose("tikhub", EP) == "b"


# ---- objects stay on their account --------------------------------------------------------------

async def test_an_async_task_is_polled_on_the_account_that_accepted_it(two_tikhub_accounts, monkeypatch):
    seen = {}

    async def fake_relay(request, url, tool, *args, **kwargs):
        seen["account"] = [b.get("account") for b in tool.bindings]

        async def stream():
            yield b'{"status": "Success"}'

        async def close():
            return None

        return UpstreamResponse(200, (), stream(), close)

    monkeypatch.setattr(task_app, "relay", fake_relay)

    def row(slot):
        return AsyncTaskRecord(
            call_id="q-1", org_id=1, provider="tikhub", endpoint_id=EP, task_id="t-1",
            reserved_micro=1, next_check_at=utcnow_naive(), key_slot=slot,
            descriptor={"poll": {"endpoint": EP, "param": {"in": "queryParams", "name": "aweme_id"}}})

    await task_app._poll(row("b"), None)
    assert set(seen["account"]) == {"tikhub/b"}
    await task_app._poll(row(None), None)
    assert set(seen["account"]) == {None}, "the primary injects the deployment's own field"
    with pytest.raises(ResolutionFailed):
        await task_app._poll(row("retired"), None)


async def test_the_relay_refuses_an_account_that_is_not_configured(two_tikhub_accounts):
    from treg.application.call.types import UpstreamRequest
    from treg.infra.upstream.relay import relay
    provider = oauth_providers.get("tikhub")
    tool = Tool(org_id=1, name=EP, owner="t", base_url=provider.base_url, host="api.tikhub.io",
                bindings=[{**b, "account": "tikhub/retired"} for b in oauth_providers.platform_bindings(provider)])

    async def empty():
        if False:
            yield b""

    request = UpstreamRequest(method="GET", raw_headers=(), query_items=(), body_stream=empty,
                              has_body=False)
    with pytest.raises(GatewayFailed) as failed:
        await relay(request, provider.base_url + "/x", tool, [], httpx.AsyncClient())
    assert failed.value.kind == "injection_failed"


def test_error_bodies_mask_every_accounts_credential(two_tikhub_accounts):
    provider = oauth_providers.get("tikhub")
    tool = Tool(org_id=1, name=EP, owner="t", base_url=provider.base_url, host="api.tikhub.io",
                bindings=oauth_providers.platform_bindings(provider, "b"))
    renderings = _secret_renderings(tool, {})
    assert TIKHUB_B in renderings and "PLATFORM-TIKHUB-KEY" in renderings
