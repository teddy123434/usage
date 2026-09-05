# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>
#
# Part of "usage". Free software licensed under the GNU Affero General Public
# License v3.0 only; see the LICENSE file for full terms and the warranty disclaimer.

"""Normalize OCX's public provider-quota contract without retaining raw rows."""

from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import IO, Literal, cast

from loaders import cache_quarantine

OCX_CACHE_PATH = Path.home() / ".usage" / "ocx-quota.json"
OCX_EXECUTABLE_PATH: Path | None = None
MAX_OUTPUT_BYTES = 1024 * 1024
CACHED_READ_TIMEOUT_S = 2.0
FORCED_REFRESH_TIMEOUT_S = 20.0
FORCED_REFRESH_INTERVAL_S = 300.0

_CACHE_SCHEMA_VERSION = 1
_FRESH_FOR = timedelta(minutes=10)
_STALE_FOR = timedelta(hours=24)
_RUN_CONDITION = threading.Condition()
_flight_generation = 0
_active_flight: _Flight | None = None
_pending_forced_refresh = False
_last_forced_started: float | None = None


class OcxCommandError(RuntimeError):
    """An OCX subprocess failed without exposing command output."""


class OcxUnreapedProcessError(RuntimeError):
    """A failed child could not be confirmed reaped; execution must stay blocked."""

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


@dataclass(slots=True)
class _Flight:
    generation: int
    force: bool
    complete: bool = False
    reaped: bool = True
    result: OcxQuotaSnapshot | None = None


def load_ocx_quota(*, request_refresh: bool = False) -> OcxQuotaSnapshot:
    """Load a normalized OCX quota snapshot without exposing OCX credentials."""
    if request_refresh:
        request_ocx_refresh()
        return _read_cache() or _empty_snapshot()

    with _RUN_CONDITION:
        if _active_flight is not None:
            flight = _active_flight
            if not flight.complete:
                _wait_for_flight_locked(flight)
            return _flight_result(flight)
        flight = _new_flight_locked(force=False)
    try:
        refreshed = _refresh_cache(force=False)
    except OcxUnreapedProcessError:
        result = _fallback_snapshot()
        _finish_flight(flight, result=result, reaped=False)
        return result
    except BaseException:
        _finish_flight(flight, result=_fallback_snapshot(), reaped=True)
        raise
    result = refreshed or _fallback_snapshot()
    _finish_flight(flight, result=result, reaped=True)
    return result


def request_ocx_refresh() -> None:
    """Start or coalesce one rate-limited forced refresh and return immediately.

    The 300-second interval begins when the forced subprocess is scheduled, even
    when that attempt later fails. A request overlapping a normal read is kept as
    one pending refresh and starts immediately after that read is fully reaped.
    """
    now = time.monotonic()
    with _RUN_CONDITION:
        global _pending_forced_refresh, _last_forced_started
        if _active_flight is not None:
            if not _active_flight.reaped or _active_flight.force:
                return
            if _forced_refresh_throttled(now):
                return
            _pending_forced_refresh = True
            return
        if _forced_refresh_throttled(now):
            return
        flight = _new_flight_locked(force=True)
        _last_forced_started = now
        _pending_forced_refresh = False

    _start_forced_thread(flight, propagate=True)


def _new_flight_locked(*, force: bool) -> _Flight:
    global _active_flight, _flight_generation
    _flight_generation += 1
    flight = _Flight(generation=_flight_generation, force=force)
    _active_flight = flight
    return flight


def _forced_refresh_throttled(now: float) -> bool:
    return (
        _last_forced_started is not None
        and now - _last_forced_started < FORCED_REFRESH_INTERVAL_S
    )


def _start_forced_thread(flight: _Flight, *, propagate: bool) -> None:
    try:
        thread = threading.Thread(
            target=_forced_refresh_worker,
            args=(flight,),
            name="usage-ocx-refresh",
            daemon=True,
        )
    except BaseException:
        _finish_flight(flight, result=_fallback_snapshot(), reaped=True)
        raise
    try:
        thread.start()
    except Exception:
        if _thread_definitely_not_started(thread):
            _finish_flight(flight, result=_fallback_snapshot(), reaped=True)
        if propagate:
            raise
    except BaseException:
        # Thread.start() can be interrupted after the worker has actually begun.
        # Its state is unknowable here, so only the worker may complete the flight.
        raise


def _thread_definitely_not_started(thread: threading.Thread) -> bool:
    try:
        return thread.ident is None and not thread.is_alive()
    except Exception:
        return False


def _forced_refresh_worker(flight: _Flight) -> None:
    try:
        refreshed = _refresh_cache(force=True)
    except OcxUnreapedProcessError:
        _finish_flight(flight, result=_fallback_snapshot(), reaped=False)
        return
    except BaseException:
        _finish_flight(flight, result=_fallback_snapshot(), reaped=True)
        raise
    result = refreshed or _fallback_snapshot()
    _finish_flight(flight, result=result, reaped=True)


def _finish_flight(
    flight: _Flight,
    *,
    result: OcxQuotaSnapshot,
    reaped: bool,
) -> None:
    followup: _Flight | None = None
    clock_error: BaseException | None = None
    with _RUN_CONDITION:
        global _active_flight, _pending_forced_refresh, _last_forced_started
        flight.result = result
        flight.reaped = reaped
        flight.complete = True
        if _active_flight is flight and reaped:
            _active_flight = None
            if not flight.force and _pending_forced_refresh:
                try:
                    forced_started = time.monotonic()
                except BaseException as exc:
                    clock_error = exc
                else:
                    followup = _new_flight_locked(force=True)
                    _last_forced_started = forced_started
                    _pending_forced_refresh = False
        _RUN_CONDITION.notify_all()
    if followup is not None:
        _start_forced_thread(followup, propagate=False)
    if clock_error is not None:
        raise clock_error


def _flight_result(flight: _Flight) -> OcxQuotaSnapshot:
    if not flight.complete or flight.result is None:
        raise RuntimeError("OCX flight result requested before completion")
    return flight.result


def _wait_for_flight_locked(flight: _Flight) -> None:
    _RUN_CONDITION.wait_for(lambda: flight.complete)


def _fallback_snapshot() -> OcxQuotaSnapshot:
    return _read_cache() or _empty_snapshot()


def _refresh_cache(*, force: bool) -> OcxQuotaSnapshot | None:
    try:
        output = _invoke_ocx(
            force=force,
            timeout=FORCED_REFRESH_TIMEOUT_S if force else CACHED_READ_TIMEOUT_S,
        )
        payload = json.loads(output.decode("utf-8"))
        snapshot = parse_ocx_quota_payload(payload, _utcnow())
        _write_cache(snapshot)
        return snapshot
    except (OcxCommandError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


def _resolve_ocx_executable(explicit_path: Path | None = None) -> Path:
    candidate = explicit_path if explicit_path is not None else OCX_EXECUTABLE_PATH
    if candidate is not None:
        resolved = candidate.expanduser()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
        raise OcxCommandError("configured OCX executable is unavailable")
    discovered = shutil.which("ocx")
    if discovered is None:
        raise OcxCommandError("OCX executable is unavailable")
    return Path(discovered)


def _invoke_ocx(*, force: bool, timeout: float) -> bytes:
    return _run_ocx_command(_resolve_ocx_executable(), force=force, timeout=timeout)


def _run_ocx_command(executable: Path, *, force: bool, timeout: float) -> bytes:
    argv = (
        (str(executable), "provider", "quota", "--refresh", "--json")
        if force
        else (str(executable), "provider", "quota", "--json")
    )
    try:
        process = subprocess.Popen(  # noqa: S603 - executable is resolved, argv is fixed
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except OSError as exc:
        raise OcxCommandError("failed to start OCX") from exc
    stdout: IO[bytes] | None = None
    stderr: IO[bytes] | None = None
    readers: list[threading.Thread] = []
    try:
        stdout = process.stdout
        stderr = process.stderr
        if stdout is None or stderr is None:
            raise OcxCommandError("failed to capture OCX output")
        exceeded = threading.Event()
        read_failed = threading.Event()
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        candidates = (
            threading.Thread(
                target=_read_bounded_stream,
                args=(stdout, stdout_parts, exceeded, read_failed),
                daemon=True,
            ),
            threading.Thread(
                target=_read_bounded_stream,
                args=(stderr, stderr_parts, exceeded, read_failed),
                daemon=True,
            ),
        )
        for reader in candidates:
            reader.start()
            readers.append(reader)

        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if exceeded.is_set():
                raise OcxCommandError("OCX output exceeded the accepted size")
            if read_failed.is_set():
                raise OcxCommandError("failed to read OCX output")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OcxCommandError("OCX command timed out")
            exceeded.wait(min(0.01, remaining))
        try:
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OcxCommandError("failed to reap OCX command") from exc
        for reader in readers:
            reader.join(timeout=0.5)
        if any(reader.is_alive() for reader in readers):
            raise OcxCommandError("OCX output streams did not close")
        if exceeded.is_set():
            raise OcxCommandError("OCX output exceeded the accepted size")
        if read_failed.is_set():
            raise OcxCommandError("failed to read OCX output")
        if process.returncode != 0:
            raise OcxCommandError("OCX command failed")
        return b"".join(stdout_parts)
    except BaseException as exc:
        reaped = _stop_process(process)
        _close_process_pipes(stdout, stderr)
        for reader in readers:
            with contextlib.suppress(BaseException):
                reader.join(timeout=0.5)
        if not reaped:
            raise OcxUnreapedProcessError("OCX child could not be reaped") from exc
        raise
    finally:
        _close_process_pipes(stdout, stderr)


def _read_bounded_stream(
    stream: IO[bytes],
    parts: list[bytes],
    exceeded: threading.Event,
    read_failed: threading.Event,
) -> None:
    try:
        total = 0
        while not exceeded.is_set():
            chunk = stream.read(min(64 * 1024, MAX_OUTPUT_BYTES - total + 1))
            if not chunk:
                return
            total += len(chunk)
            if total > MAX_OUTPUT_BYTES:
                exceeded.set()
                return
            parts.append(chunk)
    except (OSError, ValueError):
        read_failed.set()


def _stop_process(process: subprocess.Popen[bytes]) -> bool:
    try:
        running = process.poll() is None
    except BaseException:
        running = True
    if running:
        with contextlib.suppress(BaseException):
            process.terminate()
    try:
        process.wait(timeout=0.5)
        return True
    except BaseException:
        with contextlib.suppress(BaseException):
            process.kill()
        try:
            process.wait(timeout=0.5)
            return True
        except BaseException:
            return False


def _close_process_pipes(stdout: IO[bytes] | None, stderr: IO[bytes] | None) -> None:
    for stream in (stdout, stderr):
        if stream is not None:
            with contextlib.suppress(BaseException):
                stream.close()


def _write_cache(snapshot: OcxQuotaSnapshot) -> None:
    payload = _serialize_cache(snapshot)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    path = OCX_CACHE_PATH
    tmp_path: str | None = None
    fd: int | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        os.fchmod(fd, 0o600)
        file = os.fdopen(fd, "wb")
        fd = None
        with file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _read_cache() -> OcxQuotaSnapshot | None:
    path = OCX_CACHE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("cache is not an object")
        if payload.get("schema_version") != _CACHE_SCHEMA_VERSION:
            return None
        snapshot = _deserialize_cache(payload)
    except FileNotFoundError:
        return None
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        OverflowError,
        TypeError,
        ValueError,
        KeyError,
    ):
        cache_quarantine.quarantine(path, "invalid-ocx-quota")
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    return _apply_cache_age(snapshot, _utcnow())


def _serialize_cache(snapshot: OcxQuotaSnapshot) -> dict[str, object]:
    return {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "generated_at": _milliseconds(snapshot.generated_at),
        "cache_written_at": _milliseconds(snapshot.cache_written_at),
        "providers": {
            provider: _serialize_provider(value) for provider, value in snapshot.providers.items()
        },
    }


def _serialize_provider(snapshot: NormalizedQuotaSnapshot) -> dict[str, object]:
    return {
        "source": snapshot.source,
        "source_label": snapshot.source_label,
        "windows": [
            {
                "label": window.label,
                "percent": window.percent,
                "reset_at": _milliseconds(window.reset_at) if window.reset_at else None,
            }
            for window in snapshot.windows
        ],
        "account_credits_usd": (
            {
                "total_credits": snapshot.account_credits_usd.total_credits,
                "total_usage": snapshot.account_credits_usd.total_usage,
                "remaining": snapshot.account_credits_usd.remaining,
            }
            if snapshot.account_credits_usd
            else None
        ),
        "key_budget_usd": (
            {
                "used": snapshot.key_budget_usd.used,
                "limit": snapshot.key_budget_usd.limit,
                "remaining": snapshot.key_budget_usd.remaining,
                "percent": snapshot.key_budget_usd.percent,
                "expires_at": (
                    _milliseconds(snapshot.key_budget_usd.expires_at)
                    if snapshot.key_budget_usd.expires_at
                    else None
                ),
                "unlimited": snapshot.key_budget_usd.unlimited,
                "reset_at": (
                    _milliseconds(snapshot.key_budget_usd.reset_at)
                    if snapshot.key_budget_usd.reset_at
                    else None
                ),
                "reset_interval": snapshot.key_budget_usd.reset_interval,
            }
            if snapshot.key_budget_usd
            else None
        ),
        "updated_at": _milliseconds(snapshot.updated_at),
        "freshness": snapshot.freshness,
        "provenance": snapshot.provenance,
        "details": _serialize_detail(snapshot.details),
    }


def _serialize_detail(value: object) -> object:
    if isinstance(value, datetime):
        return _milliseconds(value)
    if isinstance(value, Mapping):
        return {str(key): _serialize_detail(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_serialize_detail(item) for item in value]
    if value is None or isinstance(value, str | bool | int | float):
        return value
    raise TypeError("unsupported normalized cache value")


def _deserialize_cache(payload: dict[str, object]) -> OcxQuotaSnapshot:
    providers_raw = payload["providers"]
    if not isinstance(providers_raw, dict):
        raise ValueError("providers must be an object")
    providers: dict[LogicalProvider, NormalizedQuotaSnapshot] = {}
    for provider, value in providers_raw.items():
        if provider not in _PROVIDER_MAP.values() or not isinstance(value, dict):
            raise ValueError("invalid cached provider")
        logical = cast(LogicalProvider, provider)
        providers[logical] = _deserialize_provider(logical, value)
    return OcxQuotaSnapshot(
        providers=MappingProxyType(providers),
        generated_at=_timestamp(payload["generated_at"]),
        cache_written_at=_timestamp(payload["cache_written_at"]),
    )


def _deserialize_provider(
    provider: LogicalProvider, value: dict[object, object]
) -> NormalizedQuotaSnapshot:
    if value.get("source") != "ocx" or value.get("source_label") != "OCX":
        raise ValueError("invalid cached source")
    windows_raw = value.get("windows")
    if not isinstance(windows_raw, list):
        raise ValueError("invalid cached windows")
    windows: list[NormalizedQuotaWindow] = []
    for window in windows_raw:
        if not isinstance(window, dict):
            raise ValueError("invalid cached window")
        label = window.get("label")
        if not isinstance(label, str) or not label or len(label) > 160:
            raise ValueError("invalid cached window label")
        reset_raw = window.get("reset_at")
        windows.append(
            NormalizedQuotaWindow(
                label=label,
                percent=_percent(window.get("percent")),
                reset_at=_timestamp(reset_raw) if reset_raw is not None else None,
            )
        )
    account = _deserialize_account_credits(value.get("account_credits_usd"))
    key_budget = _deserialize_key_budget(value.get("key_budget_usd"))
    freshness = value.get("freshness")
    if freshness not in ("fresh", "stale", "unavailable", "reauth_required"):
        raise ValueError("invalid cached freshness")
    provenance = value.get("provenance")
    if provenance is not None and not isinstance(provenance, str):
        raise ValueError("invalid cached provenance")
    details_raw = value.get("details")
    if not isinstance(details_raw, dict):
        raise ValueError("invalid cached details")
    details: Mapping[str, object] = _empty()
    if details_raw:
        if provider != "codex" or set(details_raw) != {"aggregation"}:
            raise ValueError("invalid cached details")
        details = MappingProxyType(
            {"aggregation": _openai_aggregation(details_raw["aggregation"])}
        )
    return NormalizedQuotaSnapshot(
        provider=provider,
        source="ocx",
        source_label="OCX",
        windows=tuple(windows),
        account_credits_usd=account,
        key_budget_usd=key_budget,
        updated_at=_timestamp(value.get("updated_at")),
        freshness=freshness,
        provenance=provenance,
        details=details,
    )


def _deserialize_account_credits(value: object) -> AccountCreditsUsd | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid cached account credits")
    return AccountCreditsUsd(
        total_credits=_number(value.get("total_credits"), minimum=0),
        total_usage=_number(value.get("total_usage"), minimum=0),
        remaining=_number(value.get("remaining")),
    )


def _deserialize_key_budget(value: object) -> KeyBudgetUsd | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid cached key budget")
    unlimited = value.get("unlimited")
    interval = value.get("reset_interval")
    if not isinstance(unlimited, bool):
        raise ValueError("invalid cached unlimited flag")
    if interval is not None and interval not in ("daily", "weekly", "monthly", "none"):
        raise ValueError("invalid cached reset interval")
    expires_at = value.get("expires_at")
    reset_at = value.get("reset_at")
    return KeyBudgetUsd(
        used=_number(value.get("used"), minimum=0),
        limit=_number(value.get("limit"), minimum=0),
        remaining=_number(value.get("remaining"), minimum=0),
        percent=_percent(value.get("percent")),
        expires_at=_timestamp(expires_at) if expires_at is not None else None,
        unlimited=unlimited,
        reset_at=_timestamp(reset_at) if reset_at is not None else None,
        reset_interval=cast(ResetInterval | None, interval),
    )


def _apply_cache_age(snapshot: OcxQuotaSnapshot, now: datetime) -> OcxQuotaSnapshot:
    age = now.astimezone(UTC) - snapshot.cache_written_at
    if age <= _FRESH_FOR:
        return snapshot
    providers: dict[LogicalProvider, NormalizedQuotaSnapshot] = {}
    for provider, value in snapshot.providers.items():
        if age <= _STALE_FOR:
            providers[provider] = _with_freshness(
                value,
                "stale" if value.freshness == "fresh" else value.freshness,
            )
        else:
            providers[provider] = _unavailable(provider, value.updated_at)
    return OcxQuotaSnapshot(
        providers=MappingProxyType(providers),
        generated_at=snapshot.generated_at,
        cache_written_at=snapshot.cache_written_at,
    )


def _with_freshness(
    snapshot: NormalizedQuotaSnapshot, freshness: Freshness
) -> NormalizedQuotaSnapshot:
    return NormalizedQuotaSnapshot(
        provider=snapshot.provider,
        source=snapshot.source,
        source_label=snapshot.source_label,
        windows=snapshot.windows,
        account_credits_usd=snapshot.account_credits_usd,
        key_budget_usd=snapshot.key_budget_usd,
        updated_at=snapshot.updated_at,
        freshness=freshness,
        provenance=snapshot.provenance,
        details=snapshot.details,
    )


def _empty_snapshot() -> OcxQuotaSnapshot:
    now = _utcnow()
    return OcxQuotaSnapshot(MappingProxyType({}), now, now)


def _milliseconds(value: datetime) -> float:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cache timestamps must be timezone-aware")
    return value.timestamp() * 1000


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _reset_runtime_state_for_tests() -> None:
    global _active_flight, _flight_generation, _last_forced_started, _pending_forced_refresh
    with _RUN_CONDITION:
        _active_flight = None
        _flight_generation = 0
        _last_forced_started = None
        _pending_forced_refresh = False
        _RUN_CONDITION.notify_all()


def _wait_for_idle_for_tests(timeout: float) -> bool:
    with _RUN_CONDITION:
        return _RUN_CONDITION.wait_for(lambda: _active_flight is None, timeout=timeout)


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
