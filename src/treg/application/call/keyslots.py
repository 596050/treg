"""Which treg-owned account serves one platform call.

A provider can sit behind several treg-owned accounts (`TREG_PLATFORM_ACCOUNTS`, one
`config.PlatformSlot` each). The rules, in order:

1. A call on something an account owns (an async task, its result, a managed resource) uses that
   account: the object exists nowhere else. An owner row without a slot predates pools and lives on
   the primary account.
2. A call carrying an Idempotency-Key uses the account the key hashes to, so a retry that reaches
   the provider again lands where the provider can recognise it. Adding or removing an account can
   move a key; a retry across that change is a new request to the provider.
3. Otherwise the account whose rate bucket would make the call wait least; ties rotate. This one
   is chosen right before the relay, not during resolution, so the choice and the token it takes
   happen in one step.

The primary account's credentials are the deployment's own fields, so a call on it carries no
account and smooths in the provider's own bucket: a provider with one account behaves as before.
Sync and I/O-free: resolution calls `pinned` while it still holds its session.
"""

from __future__ import annotations

import hashlib
import itertools

from ... import oauth_providers
from ...config import PRIMARY_SLOT, get_settings, platform_setting_name
from ...domain.capacity.view import view as capacity_view
from ...infra.upstream.limiter import limiter
from .types import ResolutionFailed

_rotation = itertools.count()


def check_accounts() -> None:
    """Refuse to start on an account the call path could not use: one behind a provider with no
    primary key (the primary is the serving switch, so the provider is off), a provider without a
    pasted platform key, or an account missing its provider's paired credential (pairing it with
    the primary's half is a pair the provider rejects). Every process that serves or polls platform
    calls runs this: the API at app creation, `treg-worker` before any command."""
    settings = get_settings()
    for service, accounts in settings.platform_extra_accounts().items():
        provider = oauth_providers.get(service)
        if provider is None or not provider.uses_pasted_secret:
            raise RuntimeError(f"TREG_PLATFORM_ACCOUNTS names {service!r}, which has no platform key")
        if not settings.platform_slots(service):
            raise RuntimeError(f"TREG_PLATFORM_ACCOUNTS adds {service} accounts, but "
                               f"TREG_{platform_setting_name(service).upper()} is not set")
        paired = provider.platform_extra_setting if provider.needs_extra_credential else ""
        for slot in accounts:
            if paired and not slot.value(paired):
                raise RuntimeError(f"TREG_PLATFORM_ACCOUNTS {service}/{slot.slot} must name {paired}")


def pinned(service: str, *, owners: tuple[str | None, ...] = (), idempotency_key: str = "") -> str | None:
    """The account this call must use, or None when any account may serve it (`least_loaded`
    picks one right before the relay). `owners` are the key slots of the rows the call acts on. A
    provider with no key at all answers `primary`, and the relay refuses the injection."""
    slots = [s.slot for s in get_settings().platform_slots(service)] or [PRIMARY_SLOT]
    if owners:
        wanted = {owner or PRIMARY_SLOT for owner in owners}
        if len(wanted) > 1:
            raise ResolutionFailed("platform_account_split", status_code=409, detail={
                "error": "platform_account_split",
                "message": (f"this call names {service} objects created on different treg accounts, "
                            "and no one account holds them all; nothing was charged"),
            })
        if not wanted <= set(slots):
            raise ResolutionFailed("platform_account_retired", status_code=410, detail={
                "error": "platform_account_retired",
                "message": (f"this {service} object was created on a treg account that can no longer "
                            "serve it; nothing was charged"),
            })
        return wanted.pop()
    if len(slots) == 1:
        return slots[0]
    if idempotency_key:
        slots.sort()
        return slots[int(hashlib.sha256(idempotency_key.encode()).hexdigest()[:8], 16) % len(slots)]
    return None


def least_loaded(service: str, endpoint_id: str) -> str:
    """The account whose smoothing bucket would wait least, ties rotating. The caller takes that
    bucket's token before it next awaits, so a burst arriving together spreads instead of every
    call reading the same idle account."""
    slots = [s.slot for s in get_settings().platform_slots(service)]
    start = next(_rotation) % len(slots)
    return min(slots[start:] + slots[:start], key=lambda slot: _wait_s(service, endpoint_id, slot))


def rate_bucket(service: str, endpoint_id: str, slot: str | None) -> tuple[str, int, float] | None:
    """The smoothing bucket of this account. The primary smooths in the provider's own bucket."""
    return capacity_view.rate_bucket(service, endpoint_id, None if slot == PRIMARY_SLOT else slot)


def _wait_s(service: str, endpoint_id: str, slot: str) -> float:
    bucket = rate_bucket(service, endpoint_id, slot)
    return limiter.pending_wait_s(*bucket) if bucket else 0.0
