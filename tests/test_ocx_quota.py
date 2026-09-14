# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>

from __future__ import annotations

import io
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import cast

import pytest

from loaders import cache_quarantine, ocx_quota
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
        ("Gem", 0.0),
        ("Gem (Weekly)", 0.0),
    ]
    assert antigravity.windows[-1].reset_at == datetime(2026, 9, 12, 16, 40, tzinfo=UTC)


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
    current_account = aggregation["currentAccount"]
    assert isinstance(current_account, dict)
    current_account["email"] = "must-not-survive@example.test"

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


@pytest.fixture(autouse=True)
def _isolate_ocx_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ocx_quota, "OCX_CACHE_PATH", tmp_path / "ocx-quota.json")
    monkeypatch.setattr(cache_quarantine, "QUARANTINE_DIR", tmp_path / "quarantine")
    monkeypatch.setattr(ocx_quota, "OCX_EXECUTABLE_PATH", None)
    ocx_quota._reset_runtime_state_for_tests()


def _write_fake_ocx(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o700)
    return path


@pytest.mark.parametrize(
    ("force", "expected_argv", "expected_output"),
    [
        (
            False,
            ("provider", "quota", "--json"),
            b"provider\nquota\n--json\n",
        ),
        (
            True,
            ("provider", "quota", "--refresh", "--json"),
            b"provider\nquota\n--refresh\n--json\n",
        ),
    ],
)
def test_runner_resolves_explicit_path_and_uses_fixed_argv_without_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    force: bool,
    expected_argv: tuple[str, ...],
    expected_output: bytes,
) -> None:
    executable = _write_fake_ocx(
        tmp_path / "ocx explicit",
        "printf '%s\\n' \"$@\"",
    )
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    real_popen = cast(Callable[..., subprocess.Popen[bytes]], subprocess.Popen)

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        calls.append((args, kwargs))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", recording_popen)

    result = ocx_quota._run_ocx_command(executable, force=force, timeout=1.0)

    assert result == expected_output
    argv = calls[0][0][0]
    kwargs = calls[0][1]
    assert argv == (str(executable), *expected_argv)
    assert kwargs["shell"] is False


def test_runner_resolves_from_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    executable = _write_fake_ocx(tmp_path / "ocx", "printf '{}'")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert ocx_quota._resolve_ocx_executable() == executable


def test_load_uses_two_second_timeout_and_forced_refresh_uses_twenty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[tuple[bool, float]] = []

    def fake_run(*, force: bool, timeout: float) -> bytes:
        timeouts.append((force, timeout))
        return json.dumps(_payload()).encode()

    monkeypatch.setattr(ocx_quota, "_invoke_ocx", fake_run)
    ocx_quota.load_ocx_quota()
    ocx_quota.request_ocx_refresh()
    assert ocx_quota._wait_for_idle_for_tests(timeout=1)

    assert timeouts == [
        (False, ocx_quota.CACHED_READ_TIMEOUT_S),
        (True, ocx_quota.FORCED_REFRESH_TIMEOUT_S),
    ]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_runner_rejects_a_stream_past_one_mib(tmp_path: Path, stream: str) -> None:
    target = "1" if stream == "stdout" else "2"
    executable = _write_fake_ocx(
        tmp_path / "ocx",
        f"head -c {ocx_quota.MAX_OUTPUT_BYTES + 1} /dev/zero >&{target}; sleep 5",
    )
    started = time.monotonic()

    with pytest.raises(ocx_quota.OcxCommandError):
        ocx_quota._run_ocx_command(executable, force=False, timeout=2.0)

    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize(
    ("body", "timeout"),
    [
        ("exit 7", 1.0),
        ("sleep 5", 0.05),
        ("printf 'not-json'", 1.0),
    ],
)
def test_failed_command_never_replaces_last_good_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str, timeout: float
) -> None:
    old_written_at = _NOW - timedelta(minutes=3)
    cached = parse_ocx_quota_payload(_payload(), old_written_at)
    ocx_quota._write_cache(cached)
    before = ocx_quota.OCX_CACHE_PATH.read_bytes()
    executable = _write_fake_ocx(tmp_path / "ocx", body)
    monkeypatch.setattr(ocx_quota, "OCX_EXECUTABLE_PATH", executable)
    monkeypatch.setattr(ocx_quota, "CACHED_READ_TIMEOUT_S", timeout)

    result = ocx_quota.load_ocx_quota()

    assert ocx_quota.OCX_CACHE_PATH.read_bytes() == before
    assert result.cache_written_at == old_written_at


def test_missing_binary_never_replaces_last_good_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    cached = parse_ocx_quota_payload(_payload(), _NOW - timedelta(minutes=1))
    ocx_quota._write_cache(cached)
    before = ocx_quota.OCX_CACHE_PATH.read_bytes()
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    result = ocx_quota.load_ocx_quota()

    assert result.cache_written_at == cached.cache_written_at
    assert ocx_quota.OCX_CACHE_PATH.read_bytes() == before


def test_interrupted_process_is_terminated_reaped_and_does_not_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = _write_fake_ocx(tmp_path / "ocx", "sleep 5")
    monkeypatch.setattr(ocx_quota, "OCX_EXECUTABLE_PATH", executable)
    real_monotonic = time.monotonic
    calls = 0

    def interrupt_once() -> float:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise KeyboardInterrupt
        return real_monotonic()

    monkeypatch.setattr(time, "monotonic", interrupt_once)

    with pytest.raises(KeyboardInterrupt):
        ocx_quota.load_ocx_quota()

    assert not ocx_quota.OCX_CACHE_PATH.exists()


def test_cache_write_is_atomic_fsynced_and_private(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = parse_ocx_quota_payload(_payload(), _NOW)
    replacements: list[tuple[str | bytes, str | bytes]] = []
    fsyncs: list[int] = []
    real_replace = os.replace
    real_fsync = os.fsync

    def recording_replace(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        replacements.append((os.fspath(source), os.fspath(destination)))
        real_replace(source, destination)

    def recording_fsync(fd: int) -> None:
        fsyncs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "replace", recording_replace)
    monkeypatch.setattr(os, "fsync", recording_fsync)

    ocx_quota._write_cache(snapshot)

    assert replacements and Path(os.fsdecode(replacements[0][0])).parent == (
        ocx_quota.OCX_CACHE_PATH.parent
    )
    assert os.fsdecode(replacements[0][1]) == str(ocx_quota.OCX_CACHE_PATH)
    assert len(fsyncs) >= 2
    assert ocx_quota.OCX_CACHE_PATH.stat().st_mode & 0o777 == 0o600
    disk = json.loads(ocx_quota.OCX_CACHE_PATH.read_text())
    assert set(disk) == {"schema_version", "generated_at", "cache_written_at", "providers"}
    assert "statuses" not in repr(disk)
    assert "stderr" not in repr(disk)


@pytest.mark.parametrize(
    ("age", "freshness"),
    [
        (timedelta(minutes=10), "fresh"),
        (timedelta(minutes=10, microseconds=1), "stale"),
        (timedelta(hours=24), "stale"),
        (timedelta(hours=24, microseconds=1), "unavailable"),
    ],
)
def test_cache_freshness_boundaries_preserve_persisted_write_time(
    monkeypatch: pytest.MonkeyPatch, age: timedelta, freshness: str
) -> None:
    written_at = _NOW - age
    ocx_quota._write_cache(parse_ocx_quota_payload(_payload(), written_at))
    monkeypatch.setattr(ocx_quota, "_utcnow", lambda: _NOW)

    result = ocx_quota._read_cache()

    assert result is not None
    assert result.cache_written_at == written_at
    assert result.providers["codex"].freshness == freshness


def test_single_flight_ten_callers_share_one_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    entered = threading.Event()
    release = threading.Event()

    def fake_run(*, force: bool, timeout: float) -> bytes:
        nonlocal calls
        assert force is False
        assert timeout == ocx_quota.CACHED_READ_TIMEOUT_S
        calls += 1
        entered.set()
        release.wait(timeout=1)
        return json.dumps(_payload()).encode()

    monkeypatch.setattr(ocx_quota, "_invoke_ocx", fake_run)
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(ocx_quota.load_ocx_quota) for _ in range(10)]
        assert entered.wait(timeout=1)
        release.set()
        results = [future.result(timeout=1) for future in futures]

    assert calls == 1
    assert all(result.providers["codex"].freshness == "fresh" for result in results)


def test_forced_refresh_starts_at_most_once_per_five_monotonic_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    monotonic_now = 100.0

    def fake_run(*, force: bool, timeout: float) -> bytes:
        nonlocal calls
        assert force is True
        calls += 1
        return json.dumps(_payload()).encode()

    monkeypatch.setattr(ocx_quota, "_invoke_ocx", fake_run)
    monkeypatch.setattr(time, "monotonic", lambda: monotonic_now)

    ocx_quota.request_ocx_refresh()
    assert ocx_quota._wait_for_idle_for_tests(timeout=1)
    ocx_quota.request_ocx_refresh()
    monotonic_now += ocx_quota.FORCED_REFRESH_INTERVAL_S
    ocx_quota.request_ocx_refresh()
    assert ocx_quota._wait_for_idle_for_tests(timeout=1)

    assert calls == 2


def test_request_refresh_returns_before_daemon_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()

    def fake_run(*, force: bool, timeout: float) -> bytes:
        entered.set()
        release.wait(timeout=1)
        return json.dumps(_payload()).encode()

    monkeypatch.setattr(ocx_quota, "_invoke_ocx", fake_run)
    def request_and_mark_returned() -> None:
        ocx_quota.request_ocx_refresh()
        returned.set()

    caller = threading.Thread(target=request_and_mark_returned)
    caller.start()

    assert entered.wait(timeout=1)
    assert returned.wait(timeout=1)
    release.set()
    caller.join(timeout=1)
    assert ocx_quota._wait_for_idle_for_tests(timeout=1)


def test_refresh_request_overlapping_cached_read_is_coalesced_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[bool] = []

    def fake_run(*, force: bool, timeout: float) -> bytes:
        calls.append(force)
        if not force:
            entered.set()
            release.wait(timeout=1)
        return json.dumps(_payload()).encode()

    monkeypatch.setattr(ocx_quota, "_invoke_ocx", fake_run)
    reader = threading.Thread(target=ocx_quota.load_ocx_quota)
    reader.start()
    assert entered.wait(timeout=1)

    ocx_quota.request_ocx_refresh()
    ocx_quota.request_ocx_refresh()
    release.set()
    reader.join(timeout=1)
    assert ocx_quota._wait_for_idle_for_tests(timeout=1)

    assert calls == [False, True]


def test_forced_refresh_interval_is_measured_from_start_even_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    monotonic_now = 100.0

    def failing_run(*, force: bool, timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        raise ocx_quota.OcxCommandError("fixture failure")

    monkeypatch.setattr(ocx_quota, "_invoke_ocx", failing_run)
    monkeypatch.setattr(time, "monotonic", lambda: monotonic_now)

    ocx_quota.request_ocx_refresh()
    assert ocx_quota._wait_for_idle_for_tests(timeout=1)
    monotonic_now = 399.999
    ocx_quota.request_ocx_refresh()
    assert calls == 1
    monotonic_now = 400.0
    ocx_quota.request_ocx_refresh()
    assert ocx_quota._wait_for_idle_for_tests(timeout=1)

    assert calls == 2


def test_start_keyboard_interrupt_after_worker_started_stays_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_thread = threading.Thread
    worker_entered = threading.Event()
    worker_release = threading.Event()
    calls = 0

    def fake_run(*, force: bool, timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        worker_entered.set()
        worker_release.wait(timeout=1)
        payload = _payload()
        reports = payload["reports"]
        assert isinstance(reports, list)
        report = reports[0]
        assert isinstance(report, dict)
        quota = report["quota"]
        assert isinstance(quota, dict)
        quota["fiveHourPercent"] = 77
        return json.dumps(payload).encode()

    class StartThenInterrupt:
        def __init__(self, **kwargs: object) -> None:
            self._thread = real_thread(**kwargs)  # type: ignore[arg-type]

        @property
        def ident(self) -> int | None:
            return self._thread.ident

        def is_alive(self) -> bool:
            return self._thread.is_alive()

        def start(self) -> None:
            self._thread.start()
            assert worker_entered.wait(timeout=1)
            raise KeyboardInterrupt

    monkeypatch.setattr(ocx_quota, "_invoke_ocx", fake_run)
    monkeypatch.setattr(threading, "Thread", StartThenInterrupt)

    with pytest.raises(KeyboardInterrupt):
        ocx_quota.request_ocx_refresh()
    ocx_quota.request_ocx_refresh()
    assert calls == 1
    worker_release.set()

    assert ocx_quota._wait_for_idle_for_tests(timeout=1)
    cached = ocx_quota._read_cache()
    assert cached is not None
    assert cached.providers["codex"].windows[0].percent == 77


def test_unstarted_thread_exception_can_finish_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NeverStarted:
        ident = None

        def __init__(self, **kwargs: object) -> None:
            return

        def is_alive(self) -> bool:
            return False

        def start(self) -> None:
            raise RuntimeError("fixture start failure")

    monkeypatch.setattr(threading, "Thread", NeverStarted)

    with pytest.raises(RuntimeError, match="fixture start failure"):
        ocx_quota.request_ocx_refresh()

    assert ocx_quota._wait_for_idle_for_tests(timeout=0)


def test_pending_refresh_clock_interrupt_is_propagated_and_not_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normal_entered = threading.Event()
    normal_release = threading.Event()
    owner_done = threading.Event()
    owner_errors: list[BaseException] = []
    calls: list[bool] = []

    def fake_run(*, force: bool, timeout: float) -> bytes:
        calls.append(force)
        if not force:
            normal_entered.set()
            normal_release.wait(timeout=1)
        return json.dumps(_payload()).encode()

    def owner() -> None:
        try:
            ocx_quota.load_ocx_quota()
        except BaseException as exc:
            owner_errors.append(exc)
        finally:
            owner_done.set()

    monotonic_calls = 0

    def interrupted_clock() -> float:
        nonlocal monotonic_calls
        monotonic_calls += 1
        if monotonic_calls == 1:
            return 100.0
        raise KeyboardInterrupt

    monkeypatch.setattr(ocx_quota, "_invoke_ocx", fake_run)
    thread = threading.Thread(target=owner)
    thread.start()
    assert normal_entered.wait(timeout=1)
    monkeypatch.setattr(time, "monotonic", interrupted_clock)
    ocx_quota.request_ocx_refresh()
    normal_release.set()
    assert owner_done.wait(timeout=1)

    assert len(owner_errors) == 1 and isinstance(owner_errors[0], KeyboardInterrupt)
    assert ocx_quota._pending_forced_refresh is True
    monkeypatch.setattr(time, "monotonic", lambda: 500.0)
    ocx_quota.request_ocx_refresh()
    assert ocx_quota._wait_for_idle_for_tests(timeout=1)
    assert calls == [False, True]
    thread.join(timeout=1)


class _LifecycleProcess:
    def __init__(self, *, waits: list[int | BaseException] | None = None) -> None:
        self.stdout = io.BytesIO(b"{}")
        self.stderr = io.BytesIO()
        self.returncode: int | None = None
        self.calls: list[str] = []
        self._waits = list(waits or [0])

    def poll(self) -> int | None:
        self.calls.append("poll")
        return self.returncode

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append(f"wait:{timeout}")
        outcome = self._waits.pop(0) if self._waits else 0
        if isinstance(outcome, BaseException):
            raise outcome
        self.returncode = outcome
        return self.returncode


def test_reader_start_exception_terminates_and_reaps_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _LifecycleProcess()
    starts = 0

    class FakeThread:
        daemon = True

        def __init__(self, *, target: object, args: object, daemon: bool) -> None:
            self.target = target
            self.args = args

        def start(self) -> None:
            nonlocal starts
            starts += 1
            if starts == 2:
                raise RuntimeError("fixture thread start failure")

        def join(self, timeout: float | None = None) -> None:
            return

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(threading, "Thread", FakeThread)

    with pytest.raises(RuntimeError, match="thread start"):
        ocx_quota._run_ocx_command(tmp_path / "ocx", force=False, timeout=1)

    assert "terminate" in process.calls
    assert process.calls[-1].startswith("wait:")
    assert process.returncode == 0
    assert process.stdout.closed and process.stderr.closed


def test_deadline_exception_terminates_and_reaps_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _LifecycleProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        time,
        "monotonic",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        ocx_quota._run_ocx_command(tmp_path / "ocx", force=False, timeout=1)

    assert "terminate" in process.calls
    assert process.returncode == 0
    assert process.stdout.closed and process.stderr.closed


def test_cleanup_escalates_terminate_wait_kill_wait_and_confirms_reap() -> None:
    timeout = subprocess.TimeoutExpired("ocx", 0.5)
    process = _LifecycleProcess(waits=[timeout, 0])

    assert ocx_quota._stop_process(cast(subprocess.Popen[bytes], process)) is True
    assert process.calls == ["poll", "terminate", "wait:0.5", "kill", "wait:0.5"]
    assert process.returncode == 0


def test_cleanup_reports_unreaped_only_after_kill_wait_also_fails() -> None:
    first_timeout = subprocess.TimeoutExpired("ocx", 0.5)
    second_timeout = subprocess.TimeoutExpired("ocx", 0.5)
    process = _LifecycleProcess(waits=[first_timeout, second_timeout])

    assert ocx_quota._stop_process(cast(subprocess.Popen[bytes], process)) is False
    assert process.calls == ["poll", "terminate", "wait:0.5", "kill", "wait:0.5"]


def test_runner_surfaces_unreaped_child_as_fail_closed_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    timeout = subprocess.TimeoutExpired("ocx", 0.5)
    process = _LifecycleProcess(waits=[timeout, timeout])
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        time,
        "monotonic",
        lambda: (_ for _ in ()).throw(RuntimeError("fixture deadline failure")),
    )

    with pytest.raises(ocx_quota.OcxUnreapedProcessError):
        ocx_quota._run_ocx_command(tmp_path / "ocx", force=False, timeout=1)

    assert "kill" in process.calls
    assert process.stdout.closed and process.stderr.closed


def test_unreaped_child_keeps_coordinator_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def unreaped(*, force: bool) -> object:
        nonlocal calls
        calls += 1
        raise ocx_quota.OcxUnreapedProcessError("fixture unreaped")

    monkeypatch.setattr(ocx_quota, "_refresh_cache", unreaped)

    ocx_quota.load_ocx_quota()
    ocx_quota.request_ocx_refresh()

    assert calls == 1
    assert not ocx_quota._wait_for_idle_for_tests(timeout=0.01)


def test_waiter_receives_its_flight_result_when_followup_refresh_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached_entered = threading.Event()
    cached_release = threading.Event()
    forced_entered = threading.Event()
    forced_release = threading.Event()

    def quota_payload(percent: float) -> bytes:
        payload = _payload()
        reports = payload["reports"]
        assert isinstance(reports, list)
        report = reports[0]
        assert isinstance(report, dict)
        quota = report["quota"]
        assert isinstance(quota, dict)
        quota["fiveHourPercent"] = percent
        return json.dumps(payload).encode()

    def fake_run(*, force: bool, timeout: float) -> bytes:
        if force:
            forced_entered.set()
            forced_release.wait(timeout=1)
            return quota_payload(91)
        cached_entered.set()
        cached_release.wait(timeout=1)
        return quota_payload(13)

    monkeypatch.setattr(ocx_quota, "_invoke_ocx", fake_run)
    old_waiter_blocked = threading.Event()
    new_waiter_blocked = threading.Event()
    real_wait_for_flight = ocx_quota._wait_for_flight_locked

    def recording_wait_for_flight(flight: ocx_quota._Flight) -> None:
        if flight.force:
            new_waiter_blocked.set()
        else:
            old_waiter_blocked.set()
        real_wait_for_flight(flight)

    monkeypatch.setattr(ocx_quota, "_wait_for_flight_locked", recording_wait_for_flight)
    owner = threading.Thread(target=ocx_quota.load_ocx_quota)
    owner.start()
    assert cached_entered.wait(timeout=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old_waiter = pool.submit(ocx_quota.load_ocx_quota)
        assert old_waiter_blocked.wait(timeout=1)
        ocx_quota.request_ocx_refresh()
        cached_release.set()
        assert forced_entered.wait(timeout=1)
        old_result = old_waiter.result(timeout=0.5)
        new_waiter = pool.submit(ocx_quota.load_ocx_quota)
        assert new_waiter_blocked.wait(timeout=1)
        forced_release.set()
        new_result = new_waiter.result(timeout=1)

    owner.join(timeout=1)
    assert old_result.providers["codex"].windows[0].percent == 13
    assert new_result.providers["codex"].windows[0].percent == 91


@pytest.mark.parametrize("failure_point", ["fchmod", "fdopen"])
def test_cache_temp_fd_is_closed_and_unlinked_on_setup_failure(
    monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    snapshot = parse_ocx_quota_payload(_payload(), _NOW)
    real_mkstemp = cast(Callable[..., tuple[int, str]], tempfile.mkstemp)
    captured: dict[str, int | str] = {}

    def recording_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, path = real_mkstemp(*args, **kwargs)
        captured.update(fd=fd, path=path)
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", recording_mkstemp)
    if failure_point == "fchmod":
        monkeypatch.setattr(
            os,
            "fchmod",
            lambda *_args: (_ for _ in ()).throw(OSError("fixture fchmod failure")),
        )
    else:
        monkeypatch.setattr(
            os,
            "fdopen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fixture fdopen failure")),
        )

    with pytest.raises(OSError, match="fixture"):
        ocx_quota._write_cache(snapshot)

    descriptor = captured["fd"]
    path = captured["path"]
    assert isinstance(descriptor, int)
    assert isinstance(path, str)
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert not Path(path).exists()
