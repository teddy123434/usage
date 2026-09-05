# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>

"""Pure OpenRouter account and key-budget presentation."""

from __future__ import annotations

import time

from i18n import _t
from loaders.ocx_quota import NormalizedQuotaSnapshot
from menubar.state import OpenRouterState, format_human_time


def project_quota(
    snapshot: NormalizedQuotaSnapshot | None,
    language: str,
    *,
    now: float | None = None,
) -> OpenRouterState | None:
    if snapshot is None or snapshot.provider != "openrouter":
        return None
    account = snapshot.account_credits_usd
    key = snapshot.key_budget_usd
    if account is None and key is None:
        return None
    if account is not None:
        headline_value = account.remaining
    else:
        assert key is not None
        headline_value = key.remaining
    current_time = time.time() if now is None else now
    reset_text = None
    if key is not None:
        reset_at = key.reset_at or key.expires_at
        if reset_at is not None:
            reset_text = _t(
                language,
                "reset_in",
                time=format_human_time(max(0.0, reset_at.timestamp() - current_time), language),
            )
    return OpenRouterState(
        headline=_money(headline_value),
        account_remaining=_money(account.remaining) if account is not None else None,
        account_total=_money(account.total_credits) if account is not None else None,
        account_used=_money(account.total_usage) if account is not None else None,
        key_limit=_money(key.limit) if key is not None else None,
        key_remaining=_money(key.remaining) if key is not None else None,
        key_percent=key.percent if key is not None else None,
        reset_text=reset_text,
        key_unlimited=key.unlimited if key is not None else False,
    )


def _money(value: float) -> str:
    return f"${value:.2f}"
