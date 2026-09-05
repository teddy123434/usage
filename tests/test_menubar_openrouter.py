# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from loaders.ocx_quota import KeyBudgetUsd, NormalizedQuotaSnapshot, parse_ocx_quota_payload
from menubar import openrouter
from menubar import state as menubar_state
from menubar import title as menubar_title


def _title_app() -> menubar_title._TitleApp:
    return cast(menubar_title._TitleApp, SimpleNamespace(codex_5h_pct=None))


def _snapshot() -> NormalizedQuotaSnapshot:
    fixture = Path(__file__).parent / "fixtures" / "ocx_provider_quota.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    return parse_ocx_quota_payload(payload, datetime(2026, 9, 5, tzinfo=UTC)).providers[
        "openrouter"
    ]


def test_project_openrouter_prefers_account_remaining_and_keeps_key_details() -> None:
    state = openrouter.project_quota(_snapshot(), "en")

    assert state is not None
    assert state.headline == "$74.75"
    assert state.account_remaining == "$74.75"
    assert state.account_total == "$100.00"
    assert state.account_used == "$25.25"
    assert state.key_limit == "$10.00"
    assert state.key_remaining == "$8.00"
    assert state.key_percent == 20.0
    assert state.key_unlimited is False
    assert state.reset_text is not None


def test_project_openrouter_uses_key_remaining_when_account_is_missing() -> None:
    snapshot = _snapshot()
    without_account = NormalizedQuotaSnapshot(
        provider=snapshot.provider,
        source=snapshot.source,
        source_label=snapshot.source_label,
        windows=snapshot.windows,
        account_credits_usd=None,
        key_budget_usd=snapshot.key_budget_usd,
        updated_at=snapshot.updated_at,
        freshness=snapshot.freshness,
        provenance=snapshot.provenance,
        details=snapshot.details,
    )

    state = openrouter.project_quota(without_account, "en")

    assert state is not None
    assert state.headline == "$8.00"
    assert state.account_remaining is None


def test_project_openrouter_uses_explicit_unlimited_headline_without_zero_placeholders() -> None:
    snapshot = _snapshot()
    unlimited = NormalizedQuotaSnapshot(
        provider="openrouter",
        source="ocx",
        source_label="OCX",
        windows=(),
        account_credits_usd=None,
        key_budget_usd=KeyBudgetUsd(0, 0, 0, 0, None, True, None, "none"),
        updated_at=snapshot.updated_at,
        freshness="fresh",
        provenance=snapshot.provenance,
        details={},
    )

    state = openrouter.project_quota(unlimited, "en")

    assert state is not None
    assert state.headline == "unlimited"
    assert state.key_unlimited is True
    assert state.key_limit is None
    assert state.key_remaining is None
    assert state.key_percent is None
    assert state.reset_interval == "none"
    assert state.reset_text is None


def test_project_openrouter_keeps_cadence_when_no_reset_timestamp_exists() -> None:
    snapshot = _snapshot()
    cadence_only = NormalizedQuotaSnapshot(
        provider="openrouter",
        source="ocx",
        source_label="OCX",
        windows=(),
        account_credits_usd=None,
        key_budget_usd=KeyBudgetUsd(2, 10, 8, 20, None, False, None, "monthly"),
        updated_at=snapshot.updated_at,
        freshness="fresh",
        provenance=snapshot.provenance,
        details={},
    )

    state = openrouter.project_quota(cadence_only, "en")

    assert state is not None
    assert state.reset_interval == "monthly"
    assert state.reset_at is None
    assert state.reset_text is None


def test_project_openrouter_uses_reset_timestamp_before_expiry() -> None:
    snapshot = _snapshot()
    reset_at = datetime(2026, 9, 6, tzinfo=UTC)
    expires_at = reset_at + timedelta(days=30)
    with_both = NormalizedQuotaSnapshot(
        provider="openrouter",
        source="ocx",
        source_label="OCX",
        windows=(),
        account_credits_usd=None,
        key_budget_usd=KeyBudgetUsd(
            2, 10, 8, 20, expires_at, False, reset_at, "monthly"
        ),
        updated_at=snapshot.updated_at,
        freshness="fresh",
        provenance=snapshot.provenance,
        details={},
    )

    state = openrouter.project_quota(
        with_both, "en", now=datetime(2026, 9, 5, tzinfo=UTC).timestamp()
    )

    assert state is not None
    assert state.reset_at == reset_at
    assert state.reset_text == "Resets in 1d 0h"


def _title_state(openrouter_state: menubar_state.OpenRouterState) -> menubar_state.PopoverState:
    state = menubar_state._empty_state("en")
    state.hide_claude = True
    state.hide_codex = True
    state.hide_agy = True
    state.hide_grok = True
    state.hide_openrouter = False
    state.openrouter = openrouter_state
    state.card_order = ("openrouter", "claude", "codex", "agy", "grok")
    return state


def test_openrouter_title_prefers_account_and_preserves_valid_zero() -> None:
    state = _title_state(
        menubar_state.OpenRouterState(
            headline="$0.00",
            account_remaining="$0.00",
            account_total="$25.00",
            account_used="$25.00",
            key_limit="$10.00",
            key_remaining="$8.00",
            key_percent=20.0,
            reset_text=None,
            key_unlimited=False,
        )
    )

    assert menubar_title._compose_title(_title_app(), state) == "OR $0.00"


def test_openrouter_title_labels_key_scope_and_respects_card_order() -> None:
    state = _title_state(
        menubar_state.OpenRouterState(
            headline="$8.00",
            account_remaining=None,
            account_total=None,
            account_used=None,
            key_limit="$10.00",
            key_remaining="$8.00",
            key_percent=20.0,
            reset_text=None,
            key_unlimited=False,
        )
    )
    state.hide_claude = False
    state.claude_session.percent = 31.0

    assert menubar_title._compose_title(_title_app(), state) == "OR Key $8.00 · 31%"


def test_openrouter_title_omits_unavailable_and_localizes_unlimited() -> None:
    unavailable = _title_state(openrouter.unavailable_state())
    assert menubar_title._compose_title(_title_app(), unavailable) == "usage"

    unlimited = _title_state(
        menubar_state.OpenRouterState(
            headline="unlimited",
            account_remaining=None,
            account_total=None,
            account_used=None,
            key_limit=None,
            key_remaining=None,
            key_percent=None,
            reset_text=None,
            key_unlimited=True,
        )
    )
    assert menubar_title._compose_title(_title_app(), unlimited) == "OR Unlimited"
