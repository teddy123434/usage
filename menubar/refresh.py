# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>
#
# Part of "usage". Free software licensed under the GNU Affero General Public
# License v3.0 only; see the LICENSE file for full terms and the warranty disclaimer.

"""Background-safe refresh loading and popover-state construction."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol

import quota.agy_window_keeper as agy_window_keeper
import quota.codex_window_keeper as codex_window_keeper
import quota.window_keeper as window_keeper
import talent_market_bridge
from loaders.history_loader import UsageEntry
from loaders.ocx_quota import (
    OcxQuotaSnapshot,
    load_ocx_quota,
    request_ocx_refresh,
)
from menubar import agy as menubar_agy
from menubar import grok as menubar_grok
from menubar import openrouter as menubar_openrouter
from menubar import state as menubar_state
from menubar.prefs import (
    _hide_agy_enabled,
    _hide_claude_enabled,
    _hide_codex_enabled,
    _hide_grok_enabled,
    _quota_card_order,
    _quota_last_source,
    _quota_source_mode,
    _save_quota_last_source,
)
from menubar.quota_sources import load_route_facts, proven_ocx_route, select_quota_source
from quota.burn_rate import BurnRateTracker
from quota.usage_rate import UsageRateTracker
from service_status import CLAUDE_STATUS, CODEX_STATUS, get_service_status
from usage_client import PollOutcome, PollState

logger = logging.getLogger(__name__)

type ProjectRows = list[tuple[str, int, float | None]]


class _RefreshApp(Protocol):
    language: str
    mock: bool
    tracker: UsageRateTracker
    codex_tracker: UsageRateTracker
    agy_tracker: UsageRateTracker
    burn_rate_trackers: dict[str, BurnRateTracker]
    latest_state: menubar_state.PopoverState
    active_panel: Any
    _history_load_error_key: str | None

    def _fetch(self) -> Coroutine[Any, Any, PollOutcome]: ...

    def _load_codex_refresh_result(self) -> dict[str, Any]: ...

    def _load_history_entries(
        self, *, scan: menubar_state.HistorySourceScan | None = None
    ) -> list[UsageEntry]: ...

    def _project_rows(
        self, hours_back: int = 24, entries: list[UsageEntry] | None = None
    ) -> ProjectRows: ...

    def _statusline_setup_available(self) -> bool: ...


@dataclass(slots=True)
class RefreshSources:
    codex_result: dict[str, Any]
    history_scan: menubar_state.HistorySourceScan | None
    agy_result: menubar_agy.AgyRefreshResult
    agy_projection: menubar_agy.AgyQuotaProjection
    grok_result: menubar_grok.GrokRefreshResult
    grok_projection: menubar_grok.GrokQuotaProjection
    debug_timing: bool
    ocx_snapshot: OcxQuotaSnapshot | None = None
    route_facts: dict[str, dict[str, object]] | None = None

    def measure(self, stage: str, started_at: float) -> None:
        if self.debug_timing:
            elapsed_ms = (time.monotonic() - started_at) * 1000
            logger.debug("refresh_timing stage=%s elapsed_ms=%.1f", stage, elapsed_ms)


def load_sources(app: _RefreshApp) -> RefreshSources:
    debug_timing = os.environ.get("USAGE_DEBUG") == "1"
    started_at = time.monotonic() if debug_timing else 0.0
    codex_result = app._load_codex_refresh_result()
    history_scan = codex_result.get("_history_scan")
    if debug_timing:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        logger.debug("refresh_timing stage=%s elapsed_ms=%.1f", "codex_load", elapsed_ms)
    started_at = time.monotonic() if debug_timing else 0.0
    agy_result = menubar_agy.load_refresh_result(app.language, app.burn_rate_trackers)
    if debug_timing:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        logger.debug("refresh_timing stage=%s elapsed_ms=%.1f", "agy_load", elapsed_ms)
    agy_projection = agy_result.projection or menubar_agy.fallback_projection(app.language)
    started_at = time.monotonic() if debug_timing else 0.0
    grok_result = menubar_grok.load_refresh_result(app.language)
    if debug_timing:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        logger.debug("refresh_timing stage=%s elapsed_ms=%.1f", "grok_load", elapsed_ms)
    grok_projection = grok_result.projection or menubar_grok.fallback_projection(app.language)
    try:
        route_facts = load_route_facts()
    except Exception:
        route_facts = {}
    started_at = time.monotonic() if debug_timing else 0.0
    try:
        ocx_snapshot = load_ocx_quota()
    except Exception:
        now = datetime.now(UTC)
        ocx_snapshot = OcxQuotaSnapshot(MappingProxyType({}), now, now)
        if os.environ.get("USAGE_DEBUG") == "1":
            logger.warning("OCX quota load failed", exc_info=True)
    if debug_timing:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        logger.debug("refresh_timing stage=%s elapsed_ms=%.1f", "ocx_load", elapsed_ms)
    return RefreshSources(
        codex_result=codex_result,
        history_scan=(
            history_scan if isinstance(history_scan, menubar_state.HistorySourceScan) else None
        ),
        agy_result=agy_result,
        agy_projection=agy_projection,
        grok_result=grok_result,
        grok_projection=grok_projection,
        ocx_snapshot=ocx_snapshot,
        route_facts=route_facts,
        debug_timing=debug_timing,
    )


def build_result(app: _RefreshApp, sources: RefreshSources) -> dict[str, Any]:
    codex_result = sources.codex_result
    agy_result = sources.agy_result
    agy_projection = sources.agy_projection
    grok_result = sources.grok_result
    grok_projection = sources.grok_projection
    fallback_state = getattr(app, "latest_state", menubar_state._empty_state(app.language))
    project_rows = list(fallback_state.projects)
    project_rows_yesterday = list(fallback_state.projects_yesterday)
    project_rows_7d = list(fallback_state.projects_7d)
    project_rows_30d = list(fallback_state.projects_30d)
    project_rows_all = list(fallback_state.projects_all)
    today_text = fallback_state.today_text
    yesterday_text = fallback_state.yesterday_text
    statusline = fallback_state.statusline
    hide_claude = fallback_state.hide_claude
    hide_codex = fallback_state.hide_codex
    hide_agy = agy_result.hide_agy or _hide_agy_enabled()
    hide_grok = grok_result.hide_grok or _hide_grok_enabled()
    card_order = _quota_card_order()
    outcome: PollOutcome | None = None
    try:
        started_at = time.monotonic() if sources.debug_timing else 0.0
        if sources.history_scan is not None:
            all_entries = app._load_history_entries(scan=sources.history_scan)
        else:
            all_entries = app._load_history_entries()
        sources.measure("history_load", started_at)
        started_at = time.monotonic() if sources.debug_timing else 0.0
        if app.mock:
            project_rows = app._project_rows(hours_back=24, entries=all_entries)
            project_rows_yesterday = project_rows
            project_rows_7d = app._project_rows(hours_back=168, entries=all_entries)
            project_rows_30d = app._project_rows(hours_back=720, entries=all_entries)
            project_rows_all = app._project_rows(hours_back=0, entries=all_entries)
        else:
            (
                project_rows,
                project_rows_yesterday,
                project_rows_7d,
                project_rows_30d,
                project_rows_all,
            ) = menubar_state.project_rows_for_windows(all_entries)
        sources.measure("project_rows_windows", started_at)
        today_text = menubar_state._today_title(app.mock, app.language, entries=all_entries)
        yesterday_text = menubar_state._yesterday_title(
            app.mock, app.language, entries=all_entries
        )
        statusline = menubar_state._statusline_payload(app.language)
        hide_claude = _hide_claude_enabled()
        hide_codex = _hide_codex_enabled()
        hide_agy = agy_result.hide_agy or _hide_agy_enabled()
        hide_grok = grok_result.hide_grok or _hide_grok_enabled()
    except Exception:
        if os.environ.get("USAGE_DEBUG") == "1":
            logger.warning("local usage refresh failed", exc_info=True)
    try:
        started_at = time.monotonic() if sources.debug_timing else 0.0
        outcome = asyncio.run(app._fetch())
        sources.measure("fetch", started_at)
        codex_rows = codex_result["codex_rows"]
        codex_5h_pct = codex_result["codex_5h_pct"]
        codex_model = codex_result.get("codex_model", "unknown")
        codex_stale = codex_result.get("codex_stale")
        codex_credits = codex_result.get("codex_credits")
        show_install_button = (
            not hide_claude
            and outcome.state == PollState.TOKEN_ERROR
            and app._statusline_setup_available()
        )
        service_statuses = (
            get_service_status(CLAUDE_STATUS),
            get_service_status(CODEX_STATUS),
        )
        group = int(app.tracker.group())
        state = menubar_state.build_popover_state(
            outcome=outcome,
            codex_rows=codex_rows,
            agy_rows=(agy_projection.session, agy_projection.weekly),
            agy_group_name=agy_projection.group_name,
            grok_row=grok_projection.weekly,
            projects=project_rows,
            projects_yesterday=project_rows_yesterday,
            projects_7d=project_rows_7d,
            projects_30d=project_rows_30d,
            projects_all=project_rows_all,
            language=app.language,
            group=group,
            burn_rate_trackers=app.burn_rate_trackers,
            today_text=today_text,
            yesterday_text=yesterday_text,
            statusline=statusline,
            show_install_button=show_install_button,
            hide_claude=hide_claude,
            hide_codex=hide_codex,
            hide_agy=hide_agy,
            hide_grok=hide_grok,
            codex_stale=codex_stale,
            codex_credits=codex_credits,
            agy_stale=agy_projection.stale,
            grok_stale=grok_projection.stale,
            card_order=card_order,
            history_error=menubar_state.history_load_error_state(
                app._history_load_error_key, app.language
            ),
            service_statuses=service_statuses,
        )
        if outcome.snapshot is not None:
            window_keeper.maybe_ping(
                outcome.snapshot.current_reset_at,
                outcome.snapshot.current_percent,
                outcome.snapshot.data_source,
                app.mock,
            )
        agy_window_keeper.maybe_ping(agy_result, app.mock)
        codex_window_keeper.maybe_ping(app.mock)
    except Exception as exc:
        if os.environ.get("USAGE_DEBUG") == "1":
            logger.warning("refresh failed", exc_info=True)
        codex_rows = codex_result["codex_rows"]
        codex_5h_pct = codex_result["codex_5h_pct"]
        codex_model = codex_result.get("codex_model", "unknown")
        state = menubar_state._error_state(type(exc).__name__, app.mock, app.language)
        state.codex_session = codex_rows[0]
        state.codex_weekly = codex_rows[1]
        state.codex_stale = codex_result.get("codex_stale")
        state.agy_session = agy_projection.session
        state.agy_weekly = agy_projection.weekly
        state.agy_group_name = agy_projection.group_name
        state.agy_stale = agy_projection.stale
        state.grok_weekly = grok_projection.weekly
        state.grok_stale = grok_projection.stale
        state.history_error = menubar_state.history_load_error_state(
            app._history_load_error_key, app.language
        )
        state.projects = project_rows
        state.projects_yesterday = project_rows_yesterday
        state.projects_7d = project_rows_7d
        state.projects_30d = project_rows_30d
        state.projects_all = project_rows_all
        state.today_text = today_text
        state.yesterday_text = yesterday_text
        state.statusline = statusline
        state.hide_claude = hide_claude
        state.hide_codex = hide_codex
        state.hide_agy = hide_agy
        state.hide_grok = hide_grok
        state.card_order = card_order

    _project_selected_quota_sources(app, sources, state, outcome)
    codex_5h_pct = (
        state.codex_session.percent if state.codex_session.available else state.codex_weekly.percent
    )
    try:
        request_ocx_refresh()
    except Exception:
        if os.environ.get("USAGE_DEBUG") == "1":
            logger.warning("OCX refresh request failed", exc_info=True)

    # Talent-market data is panel-local (no quota numbers). Fetch it
    # only when that panel is active so classic/matrix users never pay
    # the subprocess cost. list_state already swallows CLI errors and
    # returns {ok:False,...}, so the panel shows its empty state.
    active_panel = getattr(app, "active_panel", None)
    if active_panel is not None and active_panel.id == "talent_market":
        try:
            state.talent = talent_market_bridge.list_state(app.language)
        except Exception:
            if os.environ.get("USAGE_DEBUG") == "1":
                logger.warning("talent market state load failed", exc_info=True)

    return {
        "state": state,
        "codex_5h_pct": codex_5h_pct,
        "codex_model": codex_model,
    }


def _project_selected_quota_sources(
    app: _RefreshApp,
    sources: RefreshSources,
    state: menubar_state.PopoverState,
    outcome: PollOutcome | None,
) -> None:
    """Select and project providers independently from one immutable OCX read."""
    now = time.time()
    previous = getattr(app, "latest_state", menubar_state._empty_state(app.language))
    state.provider_sources = dict(previous.provider_sources)
    ocx_providers = sources.ocx_snapshot.providers if sources.ocx_snapshot is not None else {}
    native = {
        "claude": menubar_state.native_claude_snapshot(
            outcome, (state.claude_session, state.claude_weekly)
        ),
        "codex": menubar_state.native_codex_snapshot(sources.codex_result),
        "antigravity": menubar_agy.native_snapshot(sources.agy_result.projection),
        "grok": menubar_grok.native_snapshot(sources.grok_result.projection),
    }
    for provider in ("claude", "codex", "antigravity", "grok"):
        ocx = ocx_providers.get(provider)
        last = None
        try:
            mode = _quota_source_mode(provider)
            last = _quota_last_source(provider)
            selected = select_quota_source(
                mode=mode,
                native=native[provider],
                ocx=ocx,
                proven_ocx_route=_route_is_ocx(sources, provider),
                last_selected=last,
            )
            if selected.selected is None:
                previous_metadata = previous.provider_sources.get(provider)
                intended_source = selected.source
                if mode == "auto" and intended_source is None and previous_metadata is not None:
                    intended_source = previous_metadata.source
                _preserve_or_clear_provider(
                    state,
                    previous,
                    provider,
                    intended_source,
                    preserve_previous=True,
                    now=now,
                )
                continue
            if selected.source == "ocx":
                projection = menubar_state.project_ocx_quota(
                    selected.selected, app.language, _provider_color(provider)
                )
                _apply_ocx_projection(state, provider, projection)
            else:
                state.provider_sources[provider] = menubar_state.provider_source_state(
                    selected.selected, app.language, now=now
                )
            if selected.source is not None and selected.source != last:
                try:
                    _save_quota_last_source(provider, selected.source)
                except Exception:
                    if os.environ.get("USAGE_DEBUG") == "1":
                        logger.warning(
                            "quota source preference save failed: %s",
                            provider,
                            exc_info=True,
                        )
        except Exception:
            if os.environ.get("USAGE_DEBUG") == "1":
                logger.warning("quota source projection failed: %s", provider, exc_info=True)
            _preserve_or_clear_provider(state, previous, provider, last, now=now)

    try:
        state.openrouter = menubar_openrouter.project_quota(
            ocx_providers.get("openrouter"), app.language
        )
        openrouter_snapshot = ocx_providers.get("openrouter")
        if state.openrouter is not None and openrouter_snapshot is not None:
            state.provider_sources["openrouter"] = menubar_state.provider_source_state(
                openrouter_snapshot, app.language, now=now
            )
        elif (
            openrouter_snapshot is not None
            and openrouter_snapshot.freshness == "reauth_required"
        ):
            state.openrouter = previous.openrouter or menubar_openrouter.unavailable_state()
            state.provider_sources["openrouter"] = menubar_state.provider_source_state(
                openrouter_snapshot, app.language, now=now
            )
        elif openrouter_snapshot is None and previous.openrouter is not None:
            state.openrouter = previous.openrouter
            _mark_previous_source_stale(state, previous, "openrouter", now=now)
        else:
            state.provider_sources.pop("openrouter", None)
    except Exception:
        state.openrouter = previous.openrouter


def _route_is_ocx(sources: RefreshSources, provider: str) -> bool:
    facts_by_provider = sources.route_facts or {}
    facts = facts_by_provider.get(provider)
    return proven_ocx_route(
        provider,
        route=facts,
        env=_route_env(facts) if provider == "claude" else None,
    )


def _route_env(facts: dict[str, object] | None) -> dict[str, str] | None:
    if facts is None or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in facts.items()
    ):
        return None
    return {key: value for key, value in facts.items() if isinstance(value, str)}


def _provider_color(provider: str) -> tuple[float, float, float]:
    return {
        "claude": menubar_state.CLAUDE_COLOR,
        "codex": menubar_state.CODEX_COLOR,
        "antigravity": menubar_state.AGY_COLOR,
        "grok": menubar_state.GROK_COLOR,
    }[provider]


def _apply_ocx_projection(
    state: menubar_state.PopoverState,
    provider: str,
    projection: menubar_state.OcxQuotaProjection,
) -> None:
    state.provider_sources = state.provider_sources or {}
    state.provider_sources[provider] = projection.source
    _apply_legacy_ocx_stale(state, provider, projection.source)
    rows = projection.rows
    if provider == "claude":
        state.claude_session, state.claude_weekly = _two_rows(
            rows, state.claude_session, state.claude_weekly
        )
        state.show_install_button = False
        state.status_long = projection.source.freshness != "fresh"
        state.hide_claude = _hide_claude_enabled()
    elif provider == "codex":
        state.codex_session, state.codex_weekly = _two_rows(
            rows, state.codex_session, state.codex_weekly
        )
        state.codex_credits = None
        state.codex_current_account = projection.current_account
        state.hide_codex = _hide_codex_enabled()
    elif provider == "antigravity":
        state.agy_session, state.agy_weekly = _two_rows(rows, state.agy_session, state.agy_weekly)
        state.agy_group_name = ""
        state.hide_agy = _hide_agy_enabled()
    elif provider == "grok":
        if rows:
            state.grok_weekly = rows[-1]
        state.hide_grok = _hide_grok_enabled()


def _two_rows(
    rows: tuple[menubar_state.QuotaRowState, ...],
    first: menubar_state.QuotaRowState,
    second: menubar_state.QuotaRowState,
) -> tuple[menubar_state.QuotaRowState, menubar_state.QuotaRowState]:
    return (
        rows[0] if rows else first,
        rows[1] if len(rows) > 1 else second,
    )


def _preserve_or_clear_provider(
    state: menubar_state.PopoverState,
    previous: menubar_state.PopoverState,
    provider: str,
    intended_source: str | None,
    *,
    preserve_previous: bool = True,
    now: float | None = None,
) -> None:
    previous_sources = previous.provider_sources
    metadata = previous_sources.get(provider)
    if (
        preserve_previous
        and metadata is not None
        and metadata.source == intended_source
        and _source_is_within_stale_window(metadata, now)
    ):
        _mark_previous_source_stale(state, previous, provider, now=now)
        if provider == "claude":
            state.claude_session, state.claude_weekly = (
                previous.claude_session,
                previous.claude_weekly,
            )
        elif provider == "codex":
            state.codex_session, state.codex_weekly = (
                previous.codex_session,
                previous.codex_weekly,
            )
            state.codex_current_account = previous.codex_current_account
        elif provider == "antigravity":
            state.agy_session, state.agy_weekly = previous.agy_session, previous.agy_weekly
            state.hide_agy = previous.hide_agy
        elif provider == "grok":
            state.grok_weekly = previous.grok_weekly
            state.hide_grok = previous.hide_grok
        retained = state.provider_sources.get(provider)
        if retained is not None:
            _apply_legacy_ocx_stale(state, provider, retained)
        return
    state.provider_sources.pop(provider, None)
    if intended_source == "ocx":
        missing = menubar_state._missing_row
        if provider == "claude":
            state.claude_session = missing("Session", menubar_state.CLAUDE_COLOR, state.language)
            state.claude_weekly = missing("Weekly", menubar_state.CLAUDE_COLOR, state.language)
        elif provider == "codex":
            state.codex_session = missing("Session", menubar_state.CODEX_COLOR, state.language)
            state.codex_weekly = missing("Weekly", menubar_state.CODEX_COLOR, state.language)
        elif provider == "antigravity":
            state.hide_agy = True
        elif provider == "grok":
            state.hide_grok = True


def _mark_previous_source_stale(
    state: menubar_state.PopoverState,
    previous: menubar_state.PopoverState,
    provider: str,
    *,
    now: float | None = None,
) -> None:
    metadata = previous.provider_sources.get(provider)
    if metadata is None:
        return
    current_time = time.time() if now is None else now
    age_text = (
        menubar_state.format_human_time(
            max(0.0, current_time - metadata.updated_at.timestamp()), state.language
        )
        if metadata.updated_at is not None
        else None
    )
    state.provider_sources[provider] = menubar_state.ProviderSourceState(
        source=metadata.source,
        label=metadata.label,
        freshness=(
            metadata.freshness if metadata.freshness == "reauth_required" else "stale"
        ),
        age_text=age_text,
        updated_at=metadata.updated_at,
    )


def _source_is_within_stale_window(
    metadata: menubar_state.ProviderSourceState,
    now: float | None,
) -> bool:
    if metadata.updated_at is None:
        return False
    current_time = time.time() if now is None else now
    return current_time - metadata.updated_at.timestamp() <= 24 * 60 * 60


def _apply_legacy_ocx_stale(
    state: menubar_state.PopoverState,
    provider: str,
    source: menubar_state.ProviderSourceState,
) -> None:
    age_text = (
        source.age_text
        if source.freshness == "stale" and source.age_text is not None
        else None
    )
    if provider == "codex":
        state.codex_stale = {"ageText": age_text} if age_text is not None else None
    elif provider == "antigravity":
        state.agy_stale = {"ageText": age_text} if age_text is not None else None
    elif provider == "grok":
        state.grok_stale = {"ageText": age_text} if age_text is not None else None
