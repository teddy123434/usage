# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>

"""Preference-first actions shared by the macOS quota-source selectors."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from menubar.prefs import (
    _hide_openrouter_enabled,
    _prefer_ocx_enabled,
    _save_prefer_ocx,
    _save_quota_source_mode,
)

logger = logging.getLogger(__name__)


class _SourceControlApp(Protocol):
    def _mark_switch_menu_action(self) -> None: ...

    def _toggle_hide_section(
        self,
        sender: Any,
        pref_key: str,
        state_attr: str,
        hide_enabled_fn: Callable[[Mapping[str, Any]], bool],
    ) -> None: ...

    def refreshNow_(self, sender: Any) -> None: ...


def toggle_openrouter_visibility(app: _SourceControlApp, sender: Any) -> None:
    app._toggle_hide_section(
        sender,
        "hide_openrouter_section",
        "hide_openrouter",
        _hide_openrouter_enabled,
    )


def select_quota_source(app: _SourceControlApp, sender: Any) -> None:
    app._mark_switch_menu_action()
    selection = sender.representedObject()
    if not isinstance(selection, str):
        return
    provider, separator, mode = selection.partition(":")
    if not separator:
        return
    try:
        saved = _save_quota_source_mode(provider, mode)
    except Exception:
        _debug_log("quota source preference save failed")
        return
    if saved:
        app.refreshNow_(None)


def toggle_prefer_ocx(app: _SourceControlApp, sender: Any) -> None:
    app._mark_switch_menu_action()
    enabled = not _prefer_ocx_enabled()
    try:
        saved = _save_prefer_ocx(enabled)
    except Exception:
        _debug_log("prefer OCX preference save failed")
        return
    if saved:
        if hasattr(sender, "setState_"):
            sender.setState_(1 if enabled else 0)
        app.refreshNow_(None)


def _debug_log(message: str) -> None:
    if os.environ.get("USAGE_DEBUG") == "1":
        logger.warning(message, exc_info=True)
