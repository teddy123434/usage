# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import prefs
from loaders.agy_quota_probe import AgyQuotaGroup, AgyQuotaResult, AgyQuotaWindow
from loaders.grok_quota_probe import GrokQuotaResult
from menubar import agy as menubar_agy
from menubar import app as menubar
from menubar import grok as menubar_grok
from menubar.state import OpenRouterState, ProviderSourceState
from panels.web_panel import UsageScriptBridge, _state_payload


def test_state_payload_includes_agy_card_data() -> None:
    quota = AgyQuotaResult(
        groups=[
            AgyQuotaGroup(
                name="GEMINI MODELS",
                models=["gemini"],
                five_hour=AgyQuotaWindow(
                    remaining_percent=75,
                    resets_in="1h",
                    resets_in_minutes=60,
                ),
                weekly=AgyQuotaWindow(
                    remaining_percent=50,
                    resets_in="1d",
                    resets_in_minutes=1440,
                ),
            )
        ],
        fetched_at="2026-01-01T00:00:00+00:00",
    )
    projection = menubar_agy.project_quota(quota, "en", now=1_767_225_600.0)
    assert projection is not None
    state = menubar._empty_state("en")
    state.agy_session = projection.session
    state.agy_weekly = projection.weekly
    state.agy_group_name = projection.group_name
    state.agy_stale = projection.stale
    state.hide_agy = False

    payload = _state_payload(state)

    assert payload["hideAgy"] is False
    assert payload["hideGrok"] is True
    assert payload["cardOrder"] == ["claude", "codex", "agy", "grok", "openrouter"]
    assert payload["projectsYesterday"] == []
    footer = cast(dict[str, object], payload["footer"])
    assert footer["yesterday"] == "Yesterday: $0.00 (0 tokens)"
    assert payload["agy"] == {
        "session": {
            "percent": 25.0,
            "percentText": "25% used",
            "resetText": "Resets in 1h 0m",
            "warning": False,
            "available": True,
            "title": "Session",
        },
        "weekly": {
            "percent": 50.0,
            "percentText": "50% used",
            "resetText": "Resets in 1d 0h",
            "warning": False,
            "available": True,
            "title": "Weekly",
        },
        "groupName": "GEMINI MODELS",
        "stale": None,
        "source": None,
        "sourceLabel": None,
        "freshness": "unavailable",
        "ageText": None,
    }
    grok_payload = cast(dict[str, object], payload["grok"])
    grok_weekly = cast(dict[str, object], grok_payload["weekly"])
    assert grok_weekly["available"] is False


def test_state_payload_includes_available_grok_card_data() -> None:
    projection = menubar_grok.project_quota(
        GrokQuotaResult(
            used_percent=18.0,
            period_end="2026-09-01T15:50:08+00:00",
            fetched_at="2026-08-26T09:13:58+00:00",
            subscription_tier="SuperGrok Lite",
        ),
        "en",
        now=datetime(2026, 8, 26, 9, 13, 58, tzinfo=UTC).timestamp(),
    )
    assert projection is not None
    state = menubar._empty_state("en")
    state.grok_weekly = projection.weekly
    state.grok_stale = projection.stale
    state.hide_grok = False

    payload = _state_payload(state)

    grok_payload = cast(dict[str, object], payload["grok"])
    grok_weekly = cast(dict[str, object], grok_payload["weekly"])
    assert payload["hideGrok"] is False
    assert grok_weekly["available"] is True
    assert grok_weekly["percent"] == 18.0
    assert grok_weekly.keys() == {
        "percent",
        "percentText",
        "resetText",
        "warning",
        "available",
        "title",
    }


def test_state_payload_includes_stale_grok_card_data() -> None:
    projection = menubar_grok.project_quota(
        GrokQuotaResult(
            used_percent=18.0,
            period_end="2026-09-01T15:50:08+00:00",
            fetched_at="2026-08-26T09:13:58+00:00",
            subscription_tier="SuperGrok Lite",
        ),
        "en",
        now=datetime(2026, 8, 26, 12, 13, 58, tzinfo=UTC).timestamp(),
    )
    assert projection is not None
    state = menubar._empty_state("en")
    state.grok_weekly = projection.weekly
    state.grok_stale = projection.stale
    state.hide_grok = False

    payload = _state_payload(state)

    grok_payload = cast(dict[str, object], payload["grok"])
    assert grok_payload["stale"] == {"ageText": "about 3 hours ago"}


def test_state_payload_includes_source_provenance_on_every_quota_card() -> None:
    state = menubar._empty_state("en")
    state.provider_sources = {
        "claude": ProviderSourceState("native", "Native", "fresh", None),
        "codex": ProviderSourceState("ocx", "OCX", "stale", "12 minutes"),
        "antigravity": ProviderSourceState(
            "ocx", "OCX", "reauth_required", "3 hours"
        ),
        "grok": ProviderSourceState("native", "Native", "unavailable", None),
        "openrouter": ProviderSourceState("ocx", "OCX", "fresh", None),
    }

    payload = _state_payload(state)

    expected = {
        "claude": ("native", "Native", "fresh", None),
        "codex": ("ocx", "OCX", "stale", "12 minutes"),
        "agy": ("ocx", "OCX", "reauth_required", "3 hours"),
        "grok": ("native", "Native", "unavailable", None),
        "openrouter": ("ocx", "OCX", "fresh", None),
    }
    for card, values in expected.items():
        card_payload = cast(dict[str, object], payload[card])
        assert (
            card_payload["source"],
            card_payload["sourceLabel"],
            card_payload["freshness"],
            card_payload["ageText"],
        ) == values
        assert "updatedAt" not in card_payload
        assert "details" not in card_payload


def test_state_payload_includes_structured_openrouter_account_and_key_data() -> None:
    state = menubar._empty_state("en")
    state.hide_openrouter = True
    state.openrouter = OpenRouterState(
        headline="$74.75",
        account_remaining="$74.75",
        account_total="$100.00",
        account_used="$25.25",
        key_limit="$10.00",
        key_remaining="$8.00",
        key_percent=20.0,
        reset_text="Resets in 29d 4h",
        key_unlimited=False,
        reset_interval="monthly",
        reset_at=datetime(2026, 10, 4, 16, 40, tzinfo=UTC),
    )

    payload = _state_payload(state)

    assert payload["hideOpenRouter"] is True
    assert payload["cardOrder"] == ["claude", "codex", "agy", "grok", "openrouter"]
    openrouter = cast(dict[str, object], payload["openrouter"])
    assert openrouter == {
        "available": True,
        "headline": "$74.75",
        "accountRemaining": "$74.75",
        "accountTotal": "$100.00",
        "accountUsed": "$25.25",
        "keyLimit": "$10.00",
        "keyRemaining": "$8.00",
        "keyPercent": 20.0,
        "resetText": "Resets in 29d 4h",
        "keyUnlimited": False,
        "resetInterval": "monthly",
        "resetAt": "2026-10-04T16:40:00+00:00",
        "source": None,
        "sourceLabel": None,
        "freshness": "unavailable",
        "ageText": None,
    }


def test_state_payload_preserves_openrouter_unlimited_key_scope() -> None:
    state = menubar._empty_state("en")
    state.openrouter = OpenRouterState(
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

    openrouter = cast(dict[str, object], _state_payload(state)["openrouter"])

    assert openrouter["available"] is True
    assert openrouter["headline"] == "unlimited"
    assert openrouter["keyUnlimited"] is True
    assert openrouter["keyLimit"] is None
    assert openrouter["keyRemaining"] is None


def test_bridge_saves_valid_card_order_and_ignores_invalid_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)
    bridge = UsageScriptBridge.alloc().init()

    bridge.userContentController_didReceiveScriptMessage_(
        None,
        SimpleNamespace(
            body=lambda: (
                '{"action":"set_card_order","order":'
                '["agy","claude","codex","grok","openrouter"]}'
            )
        ),
    )

    assert prefs._load_preferences()["quota_card_order"] == [
        "agy",
        "claude",
        "codex",
        "grok",
        "openrouter",
    ]

    bridge.userContentController_didReceiveScriptMessage_(
        None,
        SimpleNamespace(
            body=lambda: '{"action":"set_card_order","order":["agy","claude","claude"]}'
        ),
    )

    assert prefs._load_preferences()["quota_card_order"] == [
        "agy",
        "claude",
        "codex",
        "grok",
        "openrouter",
    ]


def test_bridge_reorders_five_cards_without_dropping_future_card_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)
    prefs._save_preferences(
        {
            "quota_card_order": [
                "future-a",
                "claude",
                "codex",
                "agy",
                "grok",
                "openrouter",
                "future-b",
            ]
        }
    )
    bridge = UsageScriptBridge.alloc().init()
    state = menubar._empty_state("en")
    bridge.delegate = SimpleNamespace(latest_state=state)

    bridge.userContentController_didReceiveScriptMessage_(
        None,
        SimpleNamespace(
            body=lambda: (
                '{"action":"set_card_order","order":'
                '["openrouter","grok","agy","codex","claude"]}'
            )
        ),
    )

    assert prefs._load_preferences()["quota_card_order"] == [
        "future-a",
        "openrouter",
        "grok",
        "agy",
        "codex",
        "claude",
        "future-b",
    ]
    assert state.card_order == tuple(prefs._load_preferences()["quota_card_order"])


@pytest.mark.parametrize(
    "order",
    [
        ["claude", "codex", "agy", "grok"],
        ["claude", "codex", "agy", "grok", "future-card"],
        ["claude", "codex", "agy", "grok", "grok"],
        ["claude", "codex", "agy", "grok", "openrouter"] * 1000,
    ],
)
def test_bridge_rejects_untrusted_rendered_card_orders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, order: list[str]
) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)
    initial_order = ["claude", "codex", "agy", "grok", "openrouter"]
    prefs._save_preferences({"quota_card_order": initial_order})
    bridge = UsageScriptBridge.alloc().init()
    state = menubar._empty_state("en")
    bridge.delegate = SimpleNamespace(latest_state=state)

    bridge.userContentController_didReceiveScriptMessage_(
        None,
        SimpleNamespace(
            body=lambda: json.dumps({"action": "set_card_order", "order": order})
        ),
    )

    assert prefs._load_preferences()["quota_card_order"] == initial_order
    assert state.card_order == tuple(initial_order)


def test_bridge_does_not_change_state_when_rendered_order_save_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import panels.web_panel as web_panel

    bridge = UsageScriptBridge.alloc().init()
    state = menubar._empty_state("en")
    bridge.delegate = SimpleNamespace(latest_state=state)
    monkeypatch.setattr(
        web_panel,
        "_save_rendered_quota_card_order",
        lambda _order: (_ for _ in ()).throw(OSError("disk full")),
    )

    bridge.userContentController_didReceiveScriptMessage_(
        None,
        SimpleNamespace(
            body=lambda: (
                '{"action":"set_card_order","order":'
                '["openrouter","grok","agy","codex","claude"]}'
            )
        ),
    )

    assert state.card_order == ("claude", "codex", "agy", "grok", "openrouter")


def test_bridge_saves_valid_panel_flavor_and_ignores_invalid_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)
    bridge = UsageScriptBridge.alloc().init()

    bridge.userContentController_didReceiveScriptMessage_(
        None,
        SimpleNamespace(
            body=lambda: '{"action":"set_panel_flavor","flavor":"macchiato"}'
        ),
    )
    assert prefs._load_preferences()["panel_flavor"] == "macchiato"

    bridge.userContentController_didReceiveScriptMessage_(
        None,
        SimpleNamespace(body=lambda: '{"action":"set_panel_flavor","flavor":"latte "}'),
    )
    assert prefs._load_preferences()["panel_flavor"] == "macchiato"


def test_bridge_forwards_measured_content_height() -> None:
    calls: list[tuple[object, object]] = []
    web_view = object()
    delegate = SimpleNamespace(
        panelContentHeight_forView_=lambda height, view: calls.append((height, view))
    )
    bridge = UsageScriptBridge.alloc().init()
    bridge.delegate = delegate
    bridge.web_view = web_view

    bridge.userContentController_didReceiveScriptMessage_(
        None,
        SimpleNamespace(body=lambda: '{"action":"content_height","height":612.5}'),
    )

    assert calls == [(612.5, web_view)]


def test_bridge_forwards_window_drag() -> None:
    calls: list[object] = []
    web_view = object()
    delegate = SimpleNamespace(panelBeginWindowDrag_=lambda view: calls.append(view))
    bridge = UsageScriptBridge.alloc().init()
    bridge.delegate = delegate
    bridge.web_view = web_view

    bridge.userContentController_didReceiveScriptMessage_(
        None,
        SimpleNamespace(body=lambda: '{"action":"begin_window_drag"}'),
    )

    assert calls == [web_view]
