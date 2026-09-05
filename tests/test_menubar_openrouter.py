# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from loaders.ocx_quota import NormalizedQuotaSnapshot, parse_ocx_quota_payload
from menubar import openrouter


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
