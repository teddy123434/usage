# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>
#
# Part of "usage". Free software licensed under the GNU Affero General Public
# License v3.0 only; see the LICENSE file for full terms and the warranty disclaimer.

"""Normalize OCX's public provider-quota contract without retaining raw rows."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal, cast

type LogicalProvider = Literal["claude", "codex", "antigravity", "grok", "openrouter"]
type QuotaSource = Literal["native", "ocx"]
type Freshness = Literal["fresh", "stale", "unavailable", "reauth_required"]
type ResetInterval = Literal["daily", "weekly", "monthly", "none"]


@dataclass(frozen=True, slots=True)
class NormalizedQuotaWindow:
    """One percentage-based quota window from the OCX wire contract."""

    label: str
    percent: float
    reset_at: datetime | None


@dataclass(frozen=True, slots=True)
class AccountCreditsUsd:
    """OpenRouter account-wide purchased and consumed credits."""

    total_credits: float
    total_usage: float
    remaining: float


@dataclass(frozen=True, slots=True)
class KeyBudgetUsd:
    """Budget assigned to the active OpenRouter inference key."""

    used: float
    limit: float
    remaining: float
    percent: float
    expires_at: datetime | None
    unlimited: bool
    reset_at: datetime | None
    reset_interval: ResetInterval | None


@dataclass(frozen=True, slots=True)
class NormalizedQuotaSnapshot:
    """Provider-neutral quota data safe for Usage presentation code."""

    provider: LogicalProvider
    source: QuotaSource
    source_label: str
    windows: Sequence[NormalizedQuotaWindow]
    account_credits_usd: AccountCreditsUsd | None
    key_budget_usd: KeyBudgetUsd | None
    updated_at: datetime
    freshness: Freshness
    provenance: str | None
    details: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class OcxQuotaSnapshot:
    """One immutable, normalized response from the local OCX runtime."""

    providers: Mapping[LogicalProvider, NormalizedQuotaSnapshot]
    generated_at: datetime
    cache_written_at: datetime


_PROVIDER_MAP: Mapping[str, LogicalProvider] = MappingProxyType(
    {
        "openai": "codex",
        "anthropic": "claude",
        "google-antigravity": "antigravity",
        "xai": "grok",
        "openrouter": "openrouter",
    }
)
_PROVENANCE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "openai": frozenset(("chatgpt:wham", "codex:usage")),
        "anthropic": frozenset(("anthropic:oauth-usage", "anthropic:usage")),
        "google-antigravity": frozenset(
            ("google-antigravity:fetchAvailableModels", "antigravity:usage")
        ),
        "xai": frozenset(("xai:grok-billing-credits", "xai:grok-billing", "xai:credits")),
        "openrouter": frozenset(
            (
                "openrouter:credits+key-info",
                "openrouter:credits",
                "openrouter:key-info",
            )
        ),
    }
)
_STANDARD_WINDOWS = (
    ("fiveHourPercent", "fiveHourResetAt", "five_hour"),
    ("weeklyPercent", "weeklyResetAt", "weekly"),
    ("monthlyPercent", "monthlyResetAt", "monthly"),
)
_AGGREGATION_COUNT_KEYS = (
    "includedAccounts",
    "excludedAccounts",
    "unknownPlanAccounts",
    "missingQuotaAccounts",
    "pausedAccounts",
    "reauthAccounts",
    "staleQuotaAccounts",
    "partialWindowAccounts",
)
_AGGREGATION_WINDOW_KEYS = ("fiveHour", "weekly", "monthly")
_QUOTA_PERCENT_KEYS = ("fiveHourPercent", "weeklyPercent", "monthlyPercent")
_QUOTA_RESET_KEYS = ("fiveHourResetAt", "weeklyResetAt", "monthlyResetAt", "updatedAt")

type _ImmutableMap = Mapping[str, object]


def parse_ocx_quota_payload(payload: object, now: datetime) -> OcxQuotaSnapshot:
    """Validate and normalize OCX quota JSON, isolating failures by provider row."""
    if not isinstance(payload, dict) or not isinstance(payload.get("reports"), list):
        raise ValueError("OCX quota payload must contain a reports array")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    generated_at = _timestamp(payload.get("generatedAt"))
    cache_written_at = now.astimezone(UTC)
    reauth = _reauth_providers(payload.get("statuses"))

    providers: dict[LogicalProvider, NormalizedQuotaSnapshot] = {}
    reports = cast(list[object], payload["reports"])
    for report in reports:
        if not isinstance(report, dict):
            continue
        wire_provider = report.get("provider")
        if not isinstance(wire_provider, str) or wire_provider not in _PROVIDER_MAP:
            continue
        provider = _PROVIDER_MAP[wire_provider]
        try:
            snapshot = _parse_provider_report(wire_provider, provider, report)
        except (TypeError, ValueError, OverflowError):
            snapshot = _unavailable(provider, cache_written_at)
        if provider in reauth:
            snapshot = _with_reauth(snapshot, reauth[provider])
        providers[provider] = snapshot

    for provider, observed_at in reauth.items():
        if provider not in providers:
            providers[provider] = _with_reauth(_unavailable(provider, observed_at), observed_at)

    return OcxQuotaSnapshot(
        providers=MappingProxyType(providers),
        generated_at=generated_at,
        cache_written_at=cache_written_at,
    )


def _parse_provider_report(
    wire_provider: str,
    provider: LogicalProvider,
    report: dict[object, object],
) -> NormalizedQuotaSnapshot:
    quota = report.get("quota")
    if not isinstance(quota, dict):
        raise ValueError("quota must be an object")
    updated_at = _timestamp(report.get("updatedAt", quota.get("updatedAt")))
    if "updatedAt" in quota:
        _timestamp(quota["updatedAt"])

    windows = _quota_windows(quota)
    account_credits = _account_credits(quota.get("accountCreditsUsd"))
    key_budget = _key_budget(quota.get("creditsUsd"))
    if not windows and account_credits is None and key_budget is None:
        raise ValueError("quota report contains no displayable quota data")
    provenance_raw = report.get("source")
    provenance = (
        provenance_raw
        if isinstance(provenance_raw, str) and provenance_raw in _PROVENANCE[wire_provider]
        else None
    )
    details = (
        MappingProxyType({"aggregation": _openai_aggregation(report.get("aggregation"))})
        if wire_provider == "openai" and report.get("aggregation") is not None
        else _empty()
    )
    return NormalizedQuotaSnapshot(
        provider=provider,
        source="ocx",
        source_label="OCX",
        windows=windows,
        account_credits_usd=account_credits,
        key_budget_usd=key_budget,
        updated_at=updated_at,
        freshness="fresh",
        provenance=provenance,
        details=details,
    )


def _quota_windows(quota: dict[object, object]) -> tuple[NormalizedQuotaWindow, ...]:
    windows: list[NormalizedQuotaWindow] = []
    for percent_key, reset_key, label in _STANDARD_WINDOWS:
        if percent_key not in quota:
            if reset_key in quota:
                _timestamp(quota[reset_key])
            continue
        reset_at = _timestamp(quota[reset_key]) if reset_key in quota else None
        windows.append(NormalizedQuotaWindow(label, _percent(quota[percent_key]), reset_at))

    custom = quota.get("customWindows")
    if custom is not None:
        if not isinstance(custom, list):
            raise ValueError("customWindows must be an array")
        for item in custom:
            if not isinstance(item, dict):
                raise ValueError("custom window must be an object")
            custom_label = item.get("label")
            if not isinstance(custom_label, str) or not custom_label or len(custom_label) > 160:
                raise ValueError("custom window label is invalid")
            reset_at = _timestamp(item["resetAt"]) if "resetAt" in item else None
            windows.append(
                NormalizedQuotaWindow(custom_label, _percent(item.get("percent")), reset_at)
            )
    return tuple(windows)


def _account_credits(value: object) -> AccountCreditsUsd | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("accountCreditsUsd must be an object")
    total_credits = _number(value.get("totalCredits"), minimum=0)
    total_usage = _number(value.get("totalUsage"), minimum=0)
    remaining = _number(value.get("remaining"))
    return AccountCreditsUsd(total_credits, total_usage, remaining)


def _key_budget(value: object) -> KeyBudgetUsd | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("creditsUsd must be an object")
    unlimited = value.get("unlimited", False)
    if not isinstance(unlimited, bool):
        raise ValueError("unlimited must be a boolean")
    interval = value.get("resetInterval")
    if interval is not None and interval not in ("daily", "weekly", "monthly", "none"):
        raise ValueError("invalid reset interval")
    return KeyBudgetUsd(
        used=_number(value.get("used"), minimum=0),
        limit=_number(value.get("limit"), minimum=0),
        remaining=_number(value.get("remaining"), minimum=0),
        percent=_percent(value.get("percent")),
        expires_at=_timestamp(value["expiresAt"]) if "expiresAt" in value else None,
        unlimited=unlimited,
        reset_at=_timestamp(value["resetAt"]) if "resetAt" in value else None,
        reset_interval=cast(ResetInterval | None, interval),
    )


def _openai_aggregation(value: object) -> _ImmutableMap:
    if not isinstance(value, dict):
        raise ValueError("aggregation must be an object")
    safe: dict[str, object] = {}
    if (kind := value.get("kind")) is not None:
        if kind != "capacity-weighted-v1":
            raise ValueError("invalid aggregation kind")
        safe["kind"] = kind
    if (scope := value.get("scope")) is not None:
        if scope != "routable-known":
            raise ValueError("invalid aggregation scope")
        safe["scope"] = scope
    for key in _AGGREGATION_COUNT_KEYS:
        if key in value:
            safe[key] = _integer(value[key])
    if "incomplete" in value:
        safe["incomplete"] = _boolean(value["incomplete"])
    if "presentation" in value:
        presentation = value["presentation"]
        if presentation not in ("aggregate", "effective-account-fallback", "coverage-only"):
            raise ValueError("invalid aggregation presentation")
        safe["presentation"] = presentation
    for key in _AGGREGATION_WINDOW_KEYS:
        if key in value:
            safe[key] = _aggregation_window(value[key])
    if "customWindows" in value:
        custom = value["customWindows"]
        if not isinstance(custom, list):
            raise ValueError("aggregation customWindows must be an array")
        safe["customWindows"] = tuple(_aggregation_window(item, custom=True) for item in custom)
    if "currentAccount" in value:
        safe["currentAccount"] = _current_account(value["currentAccount"])
    return MappingProxyType(safe)


def _aggregation_window(value: object, *, custom: bool = False) -> _ImmutableMap:
    if not isinstance(value, dict):
        raise ValueError("aggregation window must be an object")
    safe: dict[str, object] = {}
    if custom:
        label = value.get("label")
        if not isinstance(label, str) or not label or len(label) > 160:
            raise ValueError("aggregation window label is invalid")
        safe["label"] = label
    safe["usedPercent"] = _percent(value.get("usedPercent"))
    for key in ("includedAccounts", "excludedAccounts"):
        if key in value:
            safe[key] = _integer(value[key])
    if "incomplete" in value:
        safe["incomplete"] = _boolean(value["incomplete"])
    if "updatedAt" in value:
        safe["updatedAt"] = _timestamp(value["updatedAt"])
    if "nextRecoveryAt" in value:
        safe["nextRecoveryAt"] = _timestamp(value["nextRecoveryAt"])
    if "nextRecoveryPercent" in value:
        safe["nextRecoveryPercent"] = _percent(value["nextRecoveryPercent"])
    return MappingProxyType(safe)


def _current_account(value: object) -> _ImmutableMap:
    if not isinstance(value, dict):
        raise ValueError("currentAccount must be an object")
    safe: dict[str, object] = {}
    if "isMain" in value:
        safe["isMain"] = _boolean(value["isMain"])
    if "plan" in value:
        plan = value["plan"]
        if plan is not None and not isinstance(plan, str):
            raise ValueError("current account plan is invalid")
        safe["plan"] = plan
    if "quota" in value:
        quota = value["quota"]
        safe["quota"] = None if quota is None else _current_account_quota(quota)
    return MappingProxyType(safe)


def _current_account_quota(value: object) -> _ImmutableMap:
    if not isinstance(value, dict):
        raise ValueError("current account quota must be an object")
    safe: dict[str, object] = {}
    for key in _QUOTA_PERCENT_KEYS:
        if key in value:
            safe[key] = _percent(value[key])
    for key in _QUOTA_RESET_KEYS:
        if key in value:
            safe[key] = _timestamp(value[key])
    if "customWindows" in value:
        custom = value["customWindows"]
        if not isinstance(custom, list):
            raise ValueError("current account customWindows must be an array")
        safe["customWindows"] = tuple(_quota_window_detail(item) for item in custom)
    return MappingProxyType(safe)


def _quota_window_detail(value: object) -> _ImmutableMap:
    if not isinstance(value, dict):
        raise ValueError("quota detail window must be an object")
    label = value.get("label")
    if not isinstance(label, str) or not label or len(label) > 160:
        raise ValueError("quota detail window label is invalid")
    safe: dict[str, object] = {"label": label, "percent": _percent(value.get("percent"))}
    if "resetAt" in value:
        safe["resetAt"] = _timestamp(value["resetAt"])
    return MappingProxyType(safe)


def _reauth_providers(value: object) -> Mapping[LogicalProvider, datetime]:
    if not isinstance(value, list):
        return MappingProxyType({})
    providers: dict[LogicalProvider, datetime] = {}
    for status in value:
        if not isinstance(status, dict) or status.get("status") != "reauth_required":
            continue
        wire_provider = status.get("provider")
        if isinstance(wire_provider, str) and wire_provider in _PROVIDER_MAP:
            try:
                observed_at = _timestamp(status.get("observedAt"))
            except (TypeError, ValueError, OverflowError):
                continue
            providers[_PROVIDER_MAP[wire_provider]] = observed_at
    return MappingProxyType(providers)


def _with_reauth(
    snapshot: NormalizedQuotaSnapshot,
    observed_at: datetime,
) -> NormalizedQuotaSnapshot:
    return NormalizedQuotaSnapshot(
        provider=snapshot.provider,
        source=snapshot.source,
        source_label=snapshot.source_label,
        windows=snapshot.windows,
        account_credits_usd=snapshot.account_credits_usd,
        key_budget_usd=snapshot.key_budget_usd,
        updated_at=snapshot.updated_at if snapshot.freshness == "fresh" else observed_at,
        freshness="reauth_required",
        provenance=snapshot.provenance,
        details=snapshot.details,
    )


def _unavailable(provider: LogicalProvider, now: datetime) -> NormalizedQuotaSnapshot:
    return NormalizedQuotaSnapshot(
        provider=provider,
        source="ocx",
        source_label="OCX",
        windows=(),
        account_credits_usd=None,
        key_budget_usd=None,
        updated_at=now,
        freshness="unavailable",
        provenance=None,
        details=_empty(),
    )


def _timestamp(value: object) -> datetime:
    milliseconds = _number(value, minimum=0)
    return datetime.fromtimestamp(milliseconds / 1000, UTC)


def _percent(value: object) -> float:
    return _number(value, minimum=0, maximum=100)


def _number(value: object, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("expected a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("expected a finite number")
    if minimum is not None and number < minimum:
        raise ValueError("number below accepted domain")
    if maximum is not None and number > maximum:
        raise ValueError("number above accepted domain")
    return number


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected a non-negative integer")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("expected a boolean")
    return value


def _empty() -> _ImmutableMap:
    return MappingProxyType({})
