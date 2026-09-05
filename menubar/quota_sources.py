# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>
#
# Part of "usage". Free software licensed under the GNU Affero General Public
# License v3.0 only; see the LICENSE file for full terms and the warranty disclaimer.

"""Pure Native/OCX quota selection and conservative route evidence."""

from __future__ import annotations

import ipaddress
import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from loaders.codex_paths import codex_home
from loaders.ocx_quota import NormalizedQuotaSnapshot, QuotaSource

type SourceMode = Literal["auto", "native", "ocx"]
type SelectionReason = Literal[
    "explicit", "route", "single", "retained", "native_first", "unavailable"
]

_MAX_ROUTE_CONFIG_BYTES = 1024 * 1024
_CODEX_MARKER = "# Auto-injected by opencodex"
_GROK_BEGIN = "# >>> opencodex managed block — do not edit (removed by `ocx stop`) >>>"
_GROK_END = "# <<< opencodex managed block <<<"


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


def load_route_facts(
    *,
    codex_config_path: Path | None = None,
    grok_config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, dict[str, object]]:
    """Read only native-client routing files and return an allowlisted fact set."""
    environment = os.environ if env is None else env
    codex_path = codex_config_path or codex_home() / "config.toml"
    grok_path = grok_config_path or Path(
        environment.get("GROK_HOME", os.path.expanduser("~/.grok"))
    ) / "config.toml"
    return {
        "claude": _claude_route_facts(environment),
        "codex": _codex_route_facts(_read_route_config(codex_path)),
        "antigravity": {},
        "grok": _grok_route_facts(_read_route_config(grok_path)),
    }


def _read_route_config(path: Path) -> str:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    if nofollow is None or nonblock is None:
        return ""
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow | nonblock)
        try:
            file_info = os.fstat(descriptor)
            if not stat.S_ISREG(file_info.st_mode):
                return ""
            if file_info.st_size > _MAX_ROUTE_CONFIG_BYTES:
                return ""
            chunks: list[bytes] = []
            remaining = _MAX_ROUTE_CONFIG_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > _MAX_ROUTE_CONFIG_BYTES:
                return ""
            return data.decode("utf-8")
        finally:
            os.close(descriptor)
    except (OSError, UnicodeDecodeError):
        return ""


def _claude_route_facts(environment: Mapping[str, str]) -> dict[str, object]:
    base_url = environment.get("ANTHROPIC_BASE_URL")
    if (
        environment.get("ANTHROPIC_AUTH_TOKEN") != "opencodex-proxy"
        or not _is_loopback_url(base_url)
    ):
        return {}
    return {
        "ANTHROPIC_AUTH_TOKEN": "opencodex-proxy",
        "ANTHROPIC_BASE_URL": base_url,
    }


def _codex_route_facts(content: str) -> dict[str, object]:
    document = _parse_route_toml(content)
    if document is None:
        return {}
    lines = content.splitlines()
    table_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip() == "[model_providers.opencodex]"
        ),
        -1,
    )
    marker_owned = table_index > 0 and lines[table_index - 1] == _CODEX_MARKER
    provider = document.get("model_provider")
    providers = document.get("model_providers")
    provider_row = providers.get("opencodex") if isinstance(providers, dict) else None
    base_url = provider_row.get("base_url") if isinstance(provider_row, dict) else None
    if provider != "opencodex" or not marker_owned or not _is_loopback_url(base_url):
        return {}
    return {"ocx_owned_marker": True, "provider": provider, "base_url": base_url}


def _grok_route_facts(content: str) -> dict[str, object]:
    if _parse_route_toml(content) is None:
        return {}
    lines = content.splitlines()
    begin_rows = [index for index, line in enumerate(lines) if line == _GROK_BEGIN]
    end_rows = [index for index, line in enumerate(lines) if line == _GROK_END]
    if len(begin_rows) != 1 or len(end_rows) != 1:
        return {}
    begin = begin_rows[0]
    end = end_rows[0]
    if end <= begin:
        return {}
    region = lines[begin + 1 : end]
    table_index = next(
        (
            index
            for index, line in enumerate(region)
            if line.strip() == "[model_providers.opencodex]"
        ),
        -1,
    )
    if table_index < 0:
        return {}
    region_document = _parse_route_toml("\n".join(region))
    if region_document is None:
        return {}
    providers = region_document.get("model_providers")
    provider_row = providers.get("opencodex") if isinstance(providers, dict) else None
    base_url = provider_row.get("base_url") if isinstance(provider_row, dict) else None
    if not _is_loopback_url(base_url):
        return {}
    return {"managed_block_complete": True, "base_url": base_url}


def _parse_route_toml(content: str) -> dict[str, object] | None:
    try:
        return tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return None


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
