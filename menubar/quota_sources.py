# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>
#
# Part of "usage". Free software licensed under the GNU Affero General Public
# License v3.0 only; see the LICENSE file for full terms and the warranty disclaimer.

"""Pure Native/OCX quota selection and conservative route evidence."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from loaders.ocx_quota import NormalizedQuotaSnapshot, QuotaSource

type SourceMode = Literal["auto", "native", "ocx"]
type SelectionReason = Literal[
    "explicit", "route", "single", "retained", "native_first", "unavailable"
]


@dataclass(frozen=True, slots=True)
class SourceSelection:
    selected: NormalizedQuotaSnapshot | None
    source: QuotaSource | None
    reason: SelectionReason


def select_quota_source(
    *,
    mode: SourceMode,
    native: NormalizedQuotaSnapshot | None,
    ocx: NormalizedQuotaSnapshot | None,
    proven_ocx_route: bool,
    last_selected: QuotaSource | None,
) -> SourceSelection:
    """Choose exactly one source without pooling or cross-account substitution."""
    if mode == "native":
        return SourceSelection(_decisive_snapshot(native), "native", "explicit")
    if mode == "ocx":
        return SourceSelection(_decisive_snapshot(ocx), "ocx", "explicit")
    if proven_ocx_route:
        return SourceSelection(_decisive_snapshot(ocx), "ocx", "route")

    displayable = {
        "native": native if _is_displayable(native) else None,
        "ocx": ocx if _is_displayable(ocx) else None,
    }
    available = [source for source, snapshot in displayable.items() if snapshot is not None]
    if len(available) == 1:
        source = available[0]
        return SourceSelection(displayable[source], source, "single")  # type: ignore[arg-type]
    if len(available) == 2:
        if last_selected is not None and displayable[last_selected] is not None:
            return SourceSelection(displayable[last_selected], last_selected, "retained")
        return SourceSelection(native, "native", "native_first")
    return SourceSelection(None, None, "unavailable")


def _is_displayable(snapshot: NormalizedQuotaSnapshot | None) -> bool:
    if snapshot is None or snapshot.freshness == "unavailable":
        return False
    has_quota = bool(snapshot.windows) or snapshot.account_credits_usd is not None
    has_quota = has_quota or snapshot.key_budget_usd is not None
    # A data-empty reauthentication result remains diagnostic state, but is not
    # an available quota source for Auto fallback. Explicit/route selection above
    # still preserves it so presentation can surface the re-auth requirement.
    return has_quota


def _decisive_snapshot(
    snapshot: NormalizedQuotaSnapshot | None,
) -> NormalizedQuotaSnapshot | None:
    if snapshot is None or snapshot.freshness == "unavailable":
        return None
    return snapshot


def proven_ocx_route(
    provider: str,
    *,
    route: Mapping[str, object] | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Classify caller-supplied, sanitized route facts without reading config files.

    The caller reduces a client configuration to the whitelisted keys below. This
    module neither opens the underlying file nor retains environment values.
    """
    facts = route or {}
    if provider == "codex":
        return (
            facts.get("ocx_owned_marker") is True
            and facts.get("provider") == "opencodex"
            and _is_loopback_url(facts.get("base_url"))
        )
    if provider == "grok":
        return (
            facts.get("managed_block_complete") is True
            and _is_loopback_url(facts.get("base_url"))
        )
    if provider == "claude":
        environment = env or {}
        return (
            environment.get("ANTHROPIC_AUTH_TOKEN") == "opencodex-proxy"
            and _is_loopback_url(environment.get("ANTHROPIC_BASE_URL"))
        )
    # Antigravity has no stable route ownership marker in this release.
    return False


def _is_loopback_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        # Accessing ``port`` performs urllib's deferred numeric/range validation.
        # Userinfo has no place in an OCX-owned local endpoint and accepting it
        # would make a visually deceptive authority count as route proof.
        _ = parsed.port
        if parsed.username is not None or parsed.password is not None:
            return False
        hostname = parsed.hostname
        if parsed.scheme not in {"http", "https"} or hostname is None:
            return False
        if hostname.casefold() == "localhost":
            return True
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
