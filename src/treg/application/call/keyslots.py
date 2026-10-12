"""Which treg-owned account serves one platform call.

A provider can sit behind several treg-owned accounts (`TREG_PLATFORM_ACCOUNTS`, one
`config.PlatformSlot` each). The rules, in order:

1. A call on something an account owns (an async task, its result, a managed resource) uses that
   account: the object exists nowhere else. An owner row without a slot predates pools and lives on
   the primary account.
2. A call carrying an Idempotency-Key uses the account the key hashes to, so a retry that reaches
   the provider again lands where the provider can recognise it. Adding or removing an account can
   move a key; a retry across that change is a new request to the provider.
3. Otherwise the account whose rate bucket would make the call wait least; ties rotate.

A provider with one account always uses it, and its bucket keeps the provider's own name.
Sync and I/O-free: resolution calls this while it still holds its session.
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass

from ...config import PlatformSlot, get_settings
from ...domain.capacity.view import view as capacity_view
from ...infra.upstream.limiter import limiter
from .types import ResolutionFailed

_rotation = itertools.count()


@dataclass(frozen=True)
class KeyChoice:
    slot: str
    pooled: bool  # the provider has several accounts: smoothing keeps one bucket per account


def accounts(provider) -> tuple[PlatformSlot, ...]:
    """The accounts that can serve `provider` (an `oauth_providers` entry), the primary first. An
    extra account that does not name the provider's paired credential is left out: injecting the
    primary's half beside another account's key is a pair the provider rejects."""
    slots = get_settings().platform_slots(provider.service)
    paired = provider.platform_extra_setting if provider.needs_extra_credential else ""
    if not paired:
        return slots
    return slots[:1] + tuple(s for s in slots[1:] if s.value(paired))


def choose(slots: tuple[PlatformSlot, ...], *, service: str, endpoint_id: str,
           owners: tuple[str | None, ...] = (), idempotency_key: str = "") -> KeyChoice:
    """One account of `slots` (non-empty) for this call; `owners` are the key slots of the rows the
    call acts on."""
    if not slots:
        raise ResolutionFailed("platform_account_retired", status_code=410, detail={
            "error": "platform_account_retired",
            "message": f"treg holds no {service} account that can serve this; nothing was charged",
        })
    pooled = len(slots) > 1
    if owners:
        wanted = {owner or slots[0].slot for owner in owners}
        match = [s for s in slots if s.slot in wanted]
        if len(wanted) > 1 or not match:
            raise ResolutionFailed("platform_account_retired", status_code=410, detail={
                "error": "platform_account_retired",
                "message": (f"this {service} object was created on a treg account that can no longer "
                            "serve it; nothing was charged"),
            })
        return KeyChoice(match[0].slot, pooled)
    if not pooled:
        return KeyChoice(slots[0].slot, False)
    if idempotency_key:
        ordered = sorted(slots, key=lambda s: s.slot)
        digest = int(hashlib.sha256(idempotency_key.encode()).hexdigest()[:8], 16)
        return KeyChoice(ordered[digest % len(ordered)].slot, True)
    start = next(_rotation)
    waits = []
    for i in range(len(slots)):
        slot = slots[(start + i) % len(slots)]
        bucket = capacity_view.rate_bucket(service, endpoint_id, slot.slot)
        waits.append((limiter.pending_wait_s(*bucket) if bucket else 0.0, i, slot.slot))
    return KeyChoice(min(waits)[2], True)
