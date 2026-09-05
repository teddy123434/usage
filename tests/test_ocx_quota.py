# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>

from __future__ import annotations

import json
import math
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from loaders.ocx_quota import parse_ocx_quota_payload

_NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
_FIXTURE = Path(__file__).parent / "fixtures" / "ocx_provider_quota.json"


def _payload() -> dict[str, object]:
    value = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _one_report(provider: str, quota: object) -> dict[str, object]:
    return {
        "generatedAt": 1_788_540_000_001,
        "reports": [
            {
                "provider": provider,
                "source": "fixture:quota",
                "quota": quota,
                "updatedAt": 1_788_540_000_000,
            }
        ],
        "statuses": [],
    }


def test_parse_fixture_normalizes_known_providers_and_ignores_unknown() -> None:
    result = parse_ocx_quota_payload(_payload(), _NOW)

    assert tuple(result.providers) == (
        "codex",
        "claude",
        "antigravity",
        "grok",
        "openrouter",
    )
    assert result.generated_at == datetime(2026, 9, 4, 16, 40, 0, 1000, tzinfo=UTC)
    assert result.cache_written_at == _NOW
    assert result.providers["codex"].source == "ocx"
    assert result.providers["codex"].source_label == "OCX"
    assert result.providers["codex"].provenance == "codex:usage"


def test_parse_fixture_preserves_zero_resets_and_custom_windows() -> None:
    result = parse_ocx_quota_payload(_payload(), _NOW)

    antigravity = result.providers["antigravity"]
    assert [(window.label, window.percent) for window in antigravity.windows] == [
        ("five_hour", 0.0),
        ("weekly", 0.0),
        ("Gemini 3.1 Pro", 0.0),
    ]
    assert antigravity.windows[-1].reset_at == datetime(2026, 9, 5, 16, 40, tzinfo=UTC)


def test_parse_fixture_normalizes_openrouter_account_and_key_credits() -> None:
    openrouter = parse_ocx_quota_payload(_payload(), _NOW).providers["openrouter"]

    assert openrouter.account_credits_usd is not None
    assert openrouter.account_credits_usd.total_credits == 100.0
    assert openrouter.account_credits_usd.total_usage == 25.25
    assert openrouter.account_credits_usd.remaining == 74.75
    assert openrouter.key_budget_usd is not None
    assert openrouter.key_budget_usd.limit == 10.0
    assert openrouter.key_budget_usd.remaining == 8.0
    assert openrouter.key_budget_usd.percent == 20.0
    assert openrouter.key_budget_usd.reset_interval == "monthly"


def test_parse_fixture_keeps_only_whitelisted_openai_aggregation_details() -> None:
    payload = _payload()
    reports = payload["reports"]
    assert isinstance(reports, list)
    openai = reports[0]
    assert isinstance(openai, dict)
    openai["accountEmail"] = "must-not-survive@example.test"
    aggregation = openai["aggregation"]
    assert isinstance(aggregation, dict)
    aggregation["accountIds"] = ["must-not-survive"]
    aggregation["currentAccount"]["email"] = "must-not-survive@example.test"  # type: ignore[index]

    details = parse_ocx_quota_payload(payload, _NOW).providers["codex"].details

    assert isinstance(details, MappingProxyType)
    assert set(details) == {"aggregation"}
    assert details["aggregation"] == {
        "kind": "capacity-weighted-v1",
        "scope": "routable-known",
        "includedAccounts": 2,
        "excludedAccounts": 0,
        "unknownPlanAccounts": 0,
        "missingQuotaAccounts": 0,
        "pausedAccounts": 0,
        "reauthAccounts": 0,
        "staleQuotaAccounts": 0,
        "partialWindowAccounts": 0,
        "incomplete": False,
        "presentation": "aggregate",
        "weekly": {
            "usedPercent": 12.0,
            "includedAccounts": 2,
            "excludedAccounts": 0,
            "incomplete": False,
            "updatedAt": datetime(2026, 9, 4, 16, 40, tzinfo=UTC),
            "nextRecoveryAt": datetime(2026, 9, 11, 16, 40, tzinfo=UTC),
            "nextRecoveryPercent": 4.0,
        },
        "currentAccount": {
            "isMain": False,
            "plan": "pro",
            "quota": {
                "fiveHourPercent": 7.0,
                "fiveHourResetAt": datetime(2026, 9, 4, 17, 40, tzinfo=UTC),
                "weeklyPercent": 17.0,
                "weeklyResetAt": datetime(2026, 9, 11, 16, 40, tzinfo=UTC),
                "updatedAt": datetime(2026, 9, 4, 16, 40, tzinfo=UTC),
            },
        },
    }
    assert "must-not-survive" not in repr(details)


def test_reauth_status_changes_only_that_provider_and_drops_status_text() -> None:
    result = parse_ocx_quota_payload(_payload(), _NOW)

    assert result.providers["claude"].freshness == "reauth_required"
    assert result.providers["codex"].freshness == "fresh"
    assert "redacted fixture text" not in repr(result)


def test_reauth_status_keeps_valid_report_timestamp_windows_and_details() -> None:
    payload = _payload()
    statuses = payload["statuses"]
    assert isinstance(statuses, list)
    openai_status = statuses[0]
    assert isinstance(openai_status, dict)
    openai_status["status"] = "reauth_required"
    openai_status["observedAt"] = 1_788_540_010_000

    codex = parse_ocx_quota_payload(payload, _NOW).providers["codex"]

    assert codex.freshness == "reauth_required"
    assert codex.updated_at == datetime(2026, 9, 4, 16, 40, tzinfo=UTC)
    assert [(window.label, window.percent) for window in codex.windows[:2]] == [
        ("five_hour", 6.0),
        ("weekly", 12.0),
    ]
    assert codex.details["aggregation"]["presentation"] == "aggregate"  # type: ignore[index]


def test_status_only_reauth_creates_immutable_non_displayable_provider_snapshot() -> None:
    payload = {
        "generatedAt": 1_788_540_000_001,
        "reports": [],
        "statuses": [
            {
                "provider": "xai",
                "status": "reauth_required",
                "observedAt": 1_788_540_000_000,
                "body": "must not survive",
            }
        ],
    }

    result = parse_ocx_quota_payload(payload, _NOW)

    assert tuple(result.providers) == ("grok",)
    grok = result.providers["grok"]
    assert grok.freshness == "reauth_required"
    assert grok.updated_at == datetime(2026, 9, 4, 16, 40, tzinfo=UTC)
    assert grok.windows == ()
    assert grok.account_credits_usd is None
    assert grok.key_budget_usd is None
    assert "must not survive" not in repr(result)
    with pytest.raises(TypeError):
        result.providers["codex"] = grok  # type: ignore[index]


def test_explicit_reauth_status_wins_over_a_malformed_provider_report() -> None:
    payload = _one_report(
        "anthropic",
        {"weeklyPercent": True, "updatedAt": 1_788_540_000_000},
    )
    payload["statuses"] = [
        {
            "provider": "anthropic",
            "status": "reauth_required",
            "observedAt": 1_788_540_003_000,
            "message": "must not survive",
        }
    ]

    claude = parse_ocx_quota_payload(payload, _NOW).providers["claude"]

    assert claude.freshness == "reauth_required"
    assert claude.updated_at == datetime(2026, 9, 4, 16, 40, 3, tzinfo=UTC)
    assert claude.windows == ()
    assert "must not survive" not in repr(claude)


def test_updated_at_only_quota_is_unavailable_not_fresh() -> None:
    payload = _one_report("xai", {"updatedAt": 1_788_540_000_000})

    grok = parse_ocx_quota_payload(payload, _NOW).providers["grok"]

    assert grok.freshness == "unavailable"
    assert grok.windows == ()


@pytest.mark.parametrize("payload", [None, [], {"reports": {}}, {"reports": "rows"}])
def test_parse_rejects_invalid_top_level_contract(payload: object) -> None:
    with pytest.raises(ValueError):
        parse_ocx_quota_payload(payload, _NOW)


@pytest.mark.parametrize(
    "bad_value",
    [True, math.nan, math.inf, -math.inf, -0.1, 100.1],
)
def test_bad_percentage_makes_only_its_provider_unavailable(bad_value: object) -> None:
    payload = _payload()
    reports = payload["reports"]
    assert isinstance(reports, list)
    xai = reports[3]
    assert isinstance(xai, dict)
    quota = xai["quota"]
    assert isinstance(quota, dict)
    quota["weeklyPercent"] = bad_value

    result = parse_ocx_quota_payload(payload, _NOW)

    assert result.providers["grok"].freshness == "unavailable"
    assert result.providers["grok"].windows == ()
    assert result.providers["codex"].freshness == "fresh"


@pytest.mark.parametrize("bad_timestamp", [True, math.nan, math.inf, -1, "tomorrow"])
def test_bad_timestamp_makes_only_its_provider_unavailable(bad_timestamp: object) -> None:
    payload = _payload()
    reports = payload["reports"]
    assert isinstance(reports, list)
    xai = reports[3]
    assert isinstance(xai, dict)
    xai["updatedAt"] = bad_timestamp

    result = parse_ocx_quota_payload(payload, _NOW)

    assert result.providers["grok"].freshness == "unavailable"
    assert result.providers["codex"].freshness == "fresh"


def test_bad_credit_number_is_not_confused_with_zero() -> None:
    payload = _one_report(
        "openrouter",
        {
            "accountCreditsUsd": {"totalCredits": True, "totalUsage": 0, "remaining": 0},
            "updatedAt": 1_788_540_000_000,
        },
    )

    result = parse_ocx_quota_payload(payload, _NOW)

    assert result.providers["openrouter"].freshness == "unavailable"
    assert result.providers["openrouter"].account_credits_usd is None


def test_openrouter_key_budget_preserves_complete_wire_model() -> None:
    payload = _one_report(
        "openrouter",
        {
            "creditsUsd": {
                "used": 3.5,
                "limit": 20,
                "remaining": 16.5,
                "percent": 17.5,
                "unlimited": False,
                "resetAt": 1_791_132_000_000,
                "expiresAt": 1_793_810_400_000,
                "resetInterval": "monthly",
            },
            "updatedAt": 1_788_540_000_000,
        },
    )

    budget = parse_ocx_quota_payload(payload, _NOW).providers["openrouter"].key_budget_usd

    assert budget is not None
    assert budget.used == 3.5
    assert budget.unlimited is False
    assert budget.reset_at == datetime(2026, 10, 4, 16, 40, tzinfo=UTC)
    assert budget.expires_at == datetime(2026, 11, 4, 16, 40, tzinfo=UTC)


def test_openrouter_preserves_negative_account_balance_and_valid_monetary_zeroes() -> None:
    debt_payload = _one_report(
        "openrouter",
        {
            "accountCreditsUsd": {
                "totalCredits": 10,
                "totalUsage": 12.5,
                "remaining": -2.5,
            },
            "updatedAt": 1_788_540_000_000,
        },
    )
    zero_payload = _one_report(
        "openrouter",
        {
            "accountCreditsUsd": {"totalCredits": 0, "totalUsage": 0, "remaining": 0},
            "creditsUsd": {
                "used": 0,
                "limit": 0,
                "remaining": 0,
                "percent": 0,
                "unlimited": True,
                "resetInterval": "none",
            },
            "updatedAt": 1_788_540_000_000,
        },
    )

    debt = parse_ocx_quota_payload(debt_payload, _NOW).providers["openrouter"]
    zero = parse_ocx_quota_payload(zero_payload, _NOW).providers["openrouter"]

    assert debt.account_credits_usd is not None
    assert debt.account_credits_usd.remaining == -2.5
    assert zero.account_credits_usd is not None
    assert zero.account_credits_usd.total_credits == 0.0
    assert zero.account_credits_usd.total_usage == 0.0
    assert zero.account_credits_usd.remaining == 0.0
    assert zero.key_budget_usd is not None
    assert zero.key_budget_usd.used == 0.0
    assert zero.key_budget_usd.limit == 0.0
    assert zero.key_budget_usd.remaining == 0.0
    assert zero.key_budget_usd.percent == 0.0
    assert zero.key_budget_usd.unlimited is True


def test_models_and_nested_collections_are_immutable() -> None:
    result = parse_ocx_quota_payload(_payload(), _NOW)
    codex = result.providers["codex"]

    with pytest.raises(FrozenInstanceError):
        codex.source_label = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.providers["codex"] = codex  # type: ignore[index]
    aggregation = codex.details["aggregation"]
    assert isinstance(aggregation, MappingProxyType)
    with pytest.raises(TypeError):
        aggregation["presentation"] = "changed"  # type: ignore[index]
