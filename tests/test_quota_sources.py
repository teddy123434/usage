# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from loaders.ocx_quota import NormalizedQuotaSnapshot, NormalizedQuotaWindow
from menubar.quota_sources import (
    SourceSelection,
    load_route_facts,
    proven_ocx_route,
    select_quota_source,
)


def _snapshot(
    source: str,
    *,
    freshness: str = "fresh",
    with_quota: bool = True,
) -> NormalizedQuotaSnapshot:
    windows = (
        NormalizedQuotaWindow(label="Weekly", percent=25.0, reset_at=None),
    ) if with_quota else ()
    return NormalizedQuotaSnapshot(
        provider="codex",
        source=source,  # type: ignore[arg-type]
        source_label=source.upper(),
        windows=windows,
        account_credits_usd=None,
        key_budget_usd=None,
        updated_at=datetime(2026, 9, 5, tzinfo=UTC),
        freshness=freshness,  # type: ignore[arg-type]
        provenance=None,
        details=MappingProxyType({}),
    )


@pytest.mark.parametrize("mode", ["native", "ocx"])
def test_explicit_source_wins_without_substitution(mode: str) -> None:
    native = _snapshot("native")
    ocx = _snapshot("ocx", freshness="unavailable")

    selected = select_quota_source(
        mode=mode,  # type: ignore[arg-type]
        native=native,
        ocx=ocx,
        proven_ocx_route=mode == "native",
        last_selected="native" if mode == "ocx" else "ocx",
    )

    expected = native if mode == "native" else None
    assert selected == SourceSelection(expected, mode, "explicit")


def test_explicit_missing_source_is_not_substituted() -> None:
    native = _snapshot("native")

    selected = select_quota_source(
        mode="ocx",
        native=native,
        ocx=None,
        proven_ocx_route=False,
        last_selected="native",
    )

    assert selected == SourceSelection(None, "ocx", "explicit")


def test_proven_route_selects_ocx_before_availability_fallback() -> None:
    native = _snapshot("native")
    ocx = _snapshot("ocx", freshness="reauth_required", with_quota=False)

    selected = select_quota_source(
        mode="auto",
        native=native,
        ocx=ocx,
        proven_ocx_route=True,
        last_selected="native",
    )

    assert selected == SourceSelection(ocx, "ocx", "route")


@pytest.mark.parametrize(
    ("native", "ocx", "expected_source"),
    [
        (_snapshot("native"), None, "native"),
        (None, _snapshot("ocx"), "ocx"),
        (_snapshot("native", freshness="unavailable"), _snapshot("ocx"), "ocx"),
        (_snapshot("native"), _snapshot("ocx", freshness="unavailable"), "native"),
        (
            _snapshot("native"),
            _snapshot("ocx", freshness="reauth_required", with_quota=False),
            "native",
        ),
    ],
)
def test_auto_selects_only_displayable_source(
    native: NormalizedQuotaSnapshot | None,
    ocx: NormalizedQuotaSnapshot | None,
    expected_source: str,
) -> None:
    selected = select_quota_source(
        mode="auto",
        native=native,
        ocx=ocx,
        proven_ocx_route=False,
        last_selected=None,
    )

    assert selected.source == expected_source
    assert selected.reason == "single"


def test_data_bearing_reauth_snapshot_remains_displayable() -> None:
    ocx = _snapshot("ocx", freshness="reauth_required", with_quota=True)

    selected = select_quota_source(
        mode="auto",
        native=None,
        ocx=ocx,
        proven_ocx_route=False,
        last_selected=None,
    )

    assert selected == SourceSelection(ocx, "ocx", "single")


@pytest.mark.parametrize("last_selected", ["native", "ocx"])
def test_auto_ambiguous_retains_last_displayable_source(last_selected: str) -> None:
    native = _snapshot("native", freshness="stale")
    ocx = _snapshot("ocx", freshness="stale")

    selected = select_quota_source(
        mode="auto",
        native=native,
        ocx=ocx,
        proven_ocx_route=False,
        last_selected=last_selected,  # type: ignore[arg-type]
    )

    expected = native if last_selected == "native" else ocx
    assert selected == SourceSelection(expected, last_selected, "retained")


def test_auto_first_ambiguous_selection_is_native() -> None:
    native = _snapshot("native")
    ocx = _snapshot("ocx")

    selected = select_quota_source(
        mode="auto",
        native=native,
        ocx=ocx,
        proven_ocx_route=False,
        last_selected=None,
    )

    assert selected == SourceSelection(native, "native", "native_first")


def test_last_selection_is_not_retained_after_it_becomes_unavailable() -> None:
    native = _snapshot("native")
    ocx = _snapshot("ocx", freshness="unavailable")

    selected = select_quota_source(
        mode="auto",
        native=native,
        ocx=ocx,
        proven_ocx_route=False,
        last_selected="ocx",
    )

    assert selected == SourceSelection(native, "native", "single")


def test_no_displayable_source_returns_unavailable() -> None:
    selected = select_quota_source(
        mode="auto",
        native=_snapshot("native", freshness="unavailable"),
        ocx=_snapshot("ocx", freshness="reauth_required", with_quota=False),
        proven_ocx_route=False,
        last_selected="ocx",
    )

    assert selected == SourceSelection(None, None, "unavailable")


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        (
            {
                "ocx_owned_marker": True,
                "provider": "opencodex",
                "base_url": "http://127.0.0.1:11500/v1",
            },
            True,
        ),
        (
            {
                "ocx_owned_marker": True,
                "provider": "opencodex",
                "base_url": "http://[::1]:11500/v1",
            },
            True,
        ),
        ({"provider": "opencodex", "base_url": "http://localhost:11500/v1"}, False),
        (
            {
                "ocx_owned_marker": True,
                "provider": "other",
                "base_url": "http://127.0.0.1:11500/v1",
            },
            False,
        ),
        (
            {
                "ocx_owned_marker": True,
                "provider": "opencodex",
                "base_url": "https://proxy.example.test/v1",
            },
            False,
        ),
    ],
)
def test_codex_route_requires_owned_provider_row_and_loopback(
    route: dict[str, object], expected: bool
) -> None:
    assert proven_ocx_route("codex", route=route) is expected


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        ({"managed_block_complete": True, "base_url": "http://localhost:11500/v1"}, True),
        ({"managed_block_complete": False, "base_url": "http://localhost:11500/v1"}, False),
        ({"managed_block_complete": True, "base_url": "https://example.test/v1"}, False),
        ({"base_url": "http://localhost:11500/v1"}, False),
    ],
)
def test_grok_route_requires_complete_managed_block_and_loopback(
    route: dict[str, object], expected: bool
) -> None:
    assert proven_ocx_route("grok", route=route) is expected


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:11500",
                "ANTHROPIC_AUTH_TOKEN": "opencodex-proxy",
            },
            True,
        ),
        ({"ANTHROPIC_BASE_URL": "http://127.0.0.1:11500"}, False),
        (
            {
                "ANTHROPIC_BASE_URL": "https://gateway.example.test",
                "ANTHROPIC_AUTH_TOKEN": "opencodex-proxy",
            },
            False,
        ),
        (
            {
                "ANTHROPIC_BASE_URL": "http://localhost:11500",
                "ANTHROPIC_AUTH_TOKEN": "user-secret",
            },
            False,
        ),
    ],
)
def test_claude_route_requires_exact_ownership_token_and_loopback(
    env: dict[str, str], expected: bool
) -> None:
    assert proven_ocx_route("claude", env=env) is expected


def test_antigravity_route_is_always_ambiguous() -> None:
    assert proven_ocx_route(
        "antigravity",
        route={
            "ocx_owned_marker": True,
            "provider": "opencodex",
            "base_url": "http://127.0.0.1:11500/v1",
        },
        env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:11500",
            "ANTHROPIC_AUTH_TOKEN": "opencodex-proxy",
        },
    ) is False


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:not-a-port/v1",
        "http://127.0.0.1:70000/v1",
        "http://[::1/v1",
        "http://user@127.0.0.1:11500/v1",
        "http://user:password@localhost:11500/v1",
    ],
)
def test_route_rejects_malformed_or_credentialed_loopback_authority(
    base_url: str,
) -> None:
    assert proven_ocx_route(
        "codex",
        route={
            "ocx_owned_marker": True,
            "provider": "opencodex",
            "base_url": base_url,
        },
    ) is False


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:11500/v1",
        "http://localhost:11500/v1",
        "http://[::1]:11500/v1",
    ],
)
def test_route_accepts_well_formed_loopback_authority(base_url: str) -> None:
    assert proven_ocx_route(
        "codex",
        route={
            "ocx_owned_marker": True,
            "provider": "opencodex",
            "base_url": base_url,
        },
    ) is True


def test_load_route_facts_reads_only_exact_native_owned_blocks(tmp_path: Path) -> None:
    codex = tmp_path / "codex.toml"
    grok = tmp_path / "grok.toml"
    codex.write_text(
        'model_provider = "opencodex"\n'
        '# Auto-injected by opencodex\n'
        '[model_providers.opencodex]\n'
        'base_url = "http://127.0.0.1:10100/v1"\n',
        encoding="utf-8",
    )
    grok.write_text(
        '# >>> opencodex managed block — do not edit (removed by `ocx stop`) >>>\n'
        '[model_providers.opencodex]\n'
        'base_url = "http://127.0.0.1:10100/v1"\n'
        '# <<< opencodex managed block <<<\n',
        encoding="utf-8",
    )

    facts = load_route_facts(
        codex_config_path=codex,
        grok_config_path=grok,
        env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:10100",
            "ANTHROPIC_AUTH_TOKEN": "opencodex-proxy",
        },
    )

    assert proven_ocx_route("codex", route=facts["codex"])
    assert proven_ocx_route("grok", route=facts["grok"])
    assert proven_ocx_route("claude", env=facts["claude"])
    assert facts["antigravity"] == {}


def test_load_route_facts_rejects_bare_loopback_and_user_authored_marker(tmp_path: Path) -> None:
    codex = tmp_path / "codex.toml"
    grok = tmp_path / "grok.toml"
    codex.write_text(
        'model_provider = "opencodex"\n'
        '# user says opencodex\n'
        '[model_providers.opencodex]\n'
        'base_url = "http://127.0.0.1:10100/v1"\n',
        encoding="utf-8",
    )
    grok.write_text('base_url = "http://127.0.0.1:10100/v1"\n', encoding="utf-8")

    facts = load_route_facts(
        codex_config_path=codex, grok_config_path=grok, env={}
    )

    assert not proven_ocx_route("codex", route=facts["codex"])
    assert not proven_ocx_route("grok", route=facts["grok"])


def test_load_route_facts_rejects_reversed_grok_managed_block(tmp_path: Path) -> None:
    codex = tmp_path / "codex.toml"
    grok = tmp_path / "grok.toml"
    codex.write_text("", encoding="utf-8")
    grok.write_text(
        "# <<< opencodex managed block <<<\n"
        "[model_providers.opencodex]\n"
        'base_url = "http://127.0.0.1:10100/v1"\n'
        "# >>> opencodex managed block — do not edit (removed by `ocx stop`) >>>\n",
        encoding="utf-8",
    )

    facts = load_route_facts(
        codex_config_path=codex, grok_config_path=grok, env={}
    )

    assert facts["grok"] == {}


def test_load_route_facts_requires_grok_provider_table_inside_managed_block(
    tmp_path: Path,
) -> None:
    grok = tmp_path / "grok.toml"
    grok.write_text(
        '[model_providers.opencodex]\n'
        'base_url = "http://127.0.0.1:10100/v1"\n'
        '# >>> opencodex managed block — do not edit (removed by `ocx stop`) >>>\n'
        'marker = "empty"\n'
        '# <<< opencodex managed block <<<\n',
        encoding="utf-8",
    )

    facts = load_route_facts(
        codex_config_path=tmp_path / "missing-codex.toml",
        grok_config_path=grok,
        env={},
    )

    assert facts["grok"] == {}


def test_load_route_facts_requires_grok_base_url_inside_managed_block(
    tmp_path: Path,
) -> None:
    grok = tmp_path / "grok.toml"
    grok.write_text(
        '# >>> opencodex managed block — do not edit (removed by `ocx stop`) >>>\n'
        '[model_providers.opencodex]\n'
        '# <<< opencodex managed block <<<\n'
        'base_url = "http://127.0.0.1:10100/v1"\n',
        encoding="utf-8",
    )

    facts = load_route_facts(
        codex_config_path=tmp_path / "missing-codex.toml",
        grok_config_path=grok,
        env={},
    )

    assert facts["grok"] == {}


def test_load_route_facts_rejects_symlinked_config(tmp_path: Path) -> None:
    target = tmp_path / "target.toml"
    target.write_text(
        'model_provider = "opencodex"\n'
        '# Auto-injected by opencodex\n'
        '[model_providers.opencodex]\n'
        'base_url = "http://127.0.0.1:10100/v1"\n',
        encoding="utf-8",
    )
    codex = tmp_path / "codex.toml"
    codex.symlink_to(target)

    facts = load_route_facts(
        codex_config_path=codex,
        grok_config_path=tmp_path / "missing-grok.toml",
        env={},
    )

    assert facts["codex"] == {}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is not supported")
def test_load_route_facts_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    codex = tmp_path / "codex.fifo"
    os.mkfifo(codex)

    facts = load_route_facts(
        codex_config_path=codex,
        grok_config_path=tmp_path / "missing-grok.toml",
        env={},
    )

    assert facts["codex"] == {}


@pytest.mark.parametrize(
    ("provider", "invalid_prefix"),
    [
        ("codex", "duplicate = 1\nduplicate = 2\n"),
        ("codex", "invalid = [\n"),
        ("grok", "duplicate = 1\nduplicate = 2\n"),
        ("grok", "invalid = [\n"),
    ],
)
def test_load_route_facts_rejects_invalid_toml_anywhere(
    tmp_path: Path, provider: str, invalid_prefix: str
) -> None:
    codex_content = (
        'model_provider = "opencodex"\n'
        '# Auto-injected by opencodex\n'
        '[model_providers.opencodex]\n'
        'base_url = "http://127.0.0.1:10100/v1"\n'
    )
    grok_content = (
        '# >>> opencodex managed block — do not edit (removed by `ocx stop`) >>>\n'
        '[model_providers.opencodex]\n'
        'base_url = "http://127.0.0.1:10100/v1"\n'
        '# <<< opencodex managed block <<<\n'
    )
    codex = tmp_path / "codex.toml"
    grok = tmp_path / "grok.toml"
    codex.write_text(
        invalid_prefix + codex_content if provider == "codex" else codex_content,
        encoding="utf-8",
    )
    grok.write_text(
        invalid_prefix + grok_content if provider == "grok" else grok_content,
        encoding="utf-8",
    )

    facts = load_route_facts(
        codex_config_path=codex, grok_config_path=grok, env={}
    )

    assert facts[provider] == {}
