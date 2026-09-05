# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from pathlib import Path

import pytest

import prefs
from menubar import prefs as menubar_prefs


def test_quota_notification_thresholds_default() -> None:
    assert menubar_prefs._quota_notification_thresholds({}) == [50.0, 90.0]


def test_quota_notification_thresholds_filters_invalid_values() -> None:
    prefs = {"quota_notification_thresholds": [95, 0, 120, "x", 50.5]}

    assert menubar_prefs._quota_notification_thresholds(prefs) == [95.0, 50.5]


def test_auto_update_check_enabled_defaults_true() -> None:
    assert menubar_prefs._auto_update_check_enabled({}) is True
    assert menubar_prefs._auto_update_check_enabled({"auto_update_check": False}) is False


@pytest.mark.parametrize(
    "preferences, enabled",
    [
        ({"window_keeper": True}, True),
        ({"agy_window_keeper": True}, True),
        ({}, False),
    ],
)
def test_window_keeper_enabled_uses_a_combined_migration_gate(
    preferences: dict[str, bool], enabled: bool
) -> None:
    assert menubar_prefs._window_keeper_enabled(preferences) is enabled
    assert menubar_prefs._agy_window_keeper_enabled(preferences) is enabled


def test_quota_card_order_validates_preferences() -> None:
    assert menubar_prefs._quota_card_order({"quota_card_order": ["agy", "claude", "codex"]}) == (
        "agy",
        "claude",
        "codex",
        "grok",
        "openrouter",
    )
    invalid_values = (
        None,
        "agy",
        ["agy", "claude", "claude"],
        ["agy", "claude", "unknown"],
    )
    for value in invalid_values:
        assert menubar_prefs._quota_card_order({"quota_card_order": value}) == (
            "claude",
            "codex",
            "agy",
            "grok",
            "openrouter",
        )


def test_quota_card_order_pads_missing_cards_after_upgrade() -> None:
    # Simulates a preference saved before a new quota card existed: the
    # user's existing order is kept and the newcomer is appended, not wiped.
    assert menubar_prefs._quota_card_order({"quota_card_order": ["agy", "claude"]}) == (
        "agy",
        "claude",
        "codex",
        "grok",
        "openrouter",
    )


def test_save_quota_card_order_ignores_invalid_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)

    assert menubar_prefs._save_quota_card_order(["agy", "claude", "codex"]) is True
    assert prefs._load_preferences()["quota_card_order"] == [
        "agy",
        "claude",
        "codex",
        "grok",
        "openrouter",
    ]
    assert menubar_prefs._save_quota_card_order(["agy", "claude", "claude"]) is False
    assert prefs._load_preferences()["quota_card_order"] == [
        "agy",
        "claude",
        "codex",
        "grok",
        "openrouter",
    ]


def test_panel_flavor_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)

    assert menubar_prefs._panel_flavor() == "mocha"
    assert menubar_prefs._save_panel_flavor("latte") is True
    assert menubar_prefs._panel_flavor() == "latte"
    assert prefs._load_preferences()["panel_flavor"] == "latte"


@pytest.mark.parametrize("flavor", ["latte ", "LATTE", 123, None, ["mocha"]])
def test_save_panel_flavor_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flavor: object
) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)

    assert menubar_prefs._save_panel_flavor(flavor) is False
    assert preferences_file.exists() is False
    assert menubar_prefs._panel_flavor() == "mocha"


def test_quota_source_mode_defaults_to_auto_and_validates_provider() -> None:
    assert menubar_prefs._quota_source_mode("claude", {}) == "auto"
    assert menubar_prefs._quota_source_mode(
        "codex", {"quota_source_modes": {"codex": "ocx"}}
    ) == "ocx"
    assert menubar_prefs._quota_source_mode(
        "codex", {"quota_source_modes": {"codex": "invalid"}}
    ) == "auto"
    assert menubar_prefs._quota_source_mode(
        "openrouter", {"quota_source_modes": {"openrouter": "ocx"}}
    ) == "auto"


def test_save_quota_source_mode_round_trips_without_discarding_other_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)
    prefs._save_preferences({"quota_source_modes": {"claude": "native"}})

    assert menubar_prefs._save_quota_source_mode("codex", "ocx") is True
    saved = prefs._load_preferences()
    assert saved["quota_source_modes"] == {"claude": "native", "codex": "ocx"}
    assert menubar_prefs._quota_source_mode("codex") == "ocx"


@pytest.mark.parametrize(
    ("provider", "mode"),
    [("openrouter", "ocx"), ("codex", "invalid"), ("codex", None)],
)
def test_save_quota_source_mode_rejects_invalid_values_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: object,
    mode: object,
) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)

    assert menubar_prefs._save_quota_source_mode(provider, mode) is False
    assert preferences_file.exists() is False


def test_quota_last_source_uses_saved_selection_before_global_initial_preference() -> None:
    assert menubar_prefs._quota_last_source("codex", {}) is None
    assert menubar_prefs._quota_last_source("codex", {"prefer_ocx": True}) == "ocx"
    assert (
        menubar_prefs._quota_last_source(
            "codex",
            {"prefer_ocx": True, "quota_last_sources": {"codex": "native"}},
        )
        == "native"
    )


def test_quota_last_source_global_preference_does_not_change_explicit_mode() -> None:
    preferences = {
        "prefer_ocx": True,
        "quota_source_modes": {"codex": "native"},
    }

    assert menubar_prefs._quota_source_mode("codex", preferences) == "native"
    assert menubar_prefs._quota_last_source("codex", preferences) == "ocx"


def test_save_quota_last_source_round_trips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)

    assert menubar_prefs._save_quota_last_source("antigravity", "ocx") is True
    assert menubar_prefs._quota_last_source("antigravity") == "ocx"
    assert prefs._load_preferences()["quota_last_sources"] == {"antigravity": "ocx"}


@pytest.mark.parametrize(
    ("provider", "source"),
    [("openrouter", "ocx"), ("grok", "invalid"), ("grok", 1)],
)
def test_save_quota_last_source_rejects_invalid_values_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: object,
    source: object,
) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)

    assert menubar_prefs._save_quota_last_source(provider, source) is False
    assert preferences_file.exists() is False


def test_prefer_ocx_is_strict_boolean_and_defaults_false() -> None:
    assert menubar_prefs._prefer_ocx_enabled({}) is False
    assert menubar_prefs._prefer_ocx_enabled({"prefer_ocx": True}) is True
    assert menubar_prefs._prefer_ocx_enabled({"prefer_ocx": 1}) is False


def test_hide_openrouter_is_strict_boolean_and_defaults_false() -> None:
    assert menubar_prefs._hide_openrouter_enabled({}) is False
    assert menubar_prefs._hide_openrouter_enabled({"hide_openrouter_section": True}) is True
    assert menubar_prefs._hide_openrouter_enabled({"hide_openrouter_section": 1}) is False


def test_save_prefer_ocx_round_trips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)

    assert menubar_prefs._save_prefer_ocx(True) is True
    assert menubar_prefs._prefer_ocx_enabled() is True
    assert menubar_prefs._save_prefer_ocx(False) is True
    assert menubar_prefs._prefer_ocx_enabled() is False
    assert menubar_prefs._save_prefer_ocx(1) is False


class _EqualToEveryString:
    def __eq__(self, other: object) -> bool:
        return isinstance(other, str)

    def __hash__(self) -> int:
        return 0


def test_source_preference_writers_reject_non_string_equality_objects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)
    impostor = _EqualToEveryString()

    assert menubar_prefs._save_quota_source_mode(impostor, "ocx") is False
    assert menubar_prefs._save_quota_source_mode("codex", impostor) is False
    assert menubar_prefs._save_quota_last_source(impostor, "ocx") is False
    assert menubar_prefs._save_quota_last_source("codex", impostor) is False
    assert preferences_file.exists() is False


def test_source_preference_writers_preserve_unknown_string_provider_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    preferences_file = tmp_path / "usage-preferences.json"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", preferences_file)
    prefs._save_preferences(
        {
            "quota_source_modes": {"future-provider": "ocx"},
            "quota_last_sources": {"future-provider": "native"},
        }
    )

    assert menubar_prefs._save_quota_source_mode("codex", "native") is True
    assert menubar_prefs._save_quota_last_source("codex", "ocx") is True
    saved = prefs._load_preferences()
    assert saved["quota_source_modes"] == {
        "future-provider": "ocx",
        "codex": "native",
    }
    assert saved["quota_last_sources"] == {
        "future-provider": "native",
        "codex": "ocx",
    }
    assert menubar_prefs._quota_source_mode("future-provider", saved) == "auto"
    assert menubar_prefs._quota_last_source("future-provider", saved) is None
