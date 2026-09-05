# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>
#
# Part of "usage". Free software licensed under the GNU Affero General Public
# License v3.0 only; see the LICENSE file for full terms and the warranty disclaimer.

"""Menu bar title rendering."""

from __future__ import annotations

from typing import Any, Protocol

from AppKit import (
    NSAttributedString,
    NSFont,
    NSFontAttributeName,
    NSFontWeightBold,
    NSMutableAttributedString,
)

from i18n import _t
from menubar.chrome import (
    _agy_menubar_icon,
    _claude_menubar_icon,
    _codex_menubar_icon,
    _grok_menubar_icon,
    _menubar_icon_attachment_string,
)
from menubar.state import PopoverState, _format_percent


class _TitleApp(Protocol):
    _menubar_text_cache: dict[str, Any]
    codex_5h_pct: float | None
    _last_button_title_key: tuple[tuple[str, str], ...] | None
    _last_plain_title_key: tuple[str] | None
    status_item: Any


_MENUBAR_FONT: Any = None


def _menubar_font() -> Any:
    # Bold at the menu bar's own point size: the percentages have to carry the
    # strip on their own, against any wallpaper.
    global _MENUBAR_FONT
    if _MENUBAR_FONT is None:
        base = NSFont.menuBarFontOfSize_(0)
        _MENUBAR_FONT = NSFont.systemFontOfSize_weight_(base.pointSize(), NSFontWeightBold)
    return _MENUBAR_FONT


def _menubar_text_string(app: _TitleApp, text: str) -> Any:
    cached = app._menubar_text_cache.get(text)
    if cached is not None:
        return cached
    attributed = NSAttributedString.alloc().initWithString_attributes_(
        text,
        {NSFontAttributeName: _menubar_font()},
    )
    app._menubar_text_cache[text] = attributed
    return attributed


def _menubar_attributed_title(app: _TitleApp, state: PopoverState) -> Any:
    title = NSMutableAttributedString.alloc().init()
    icons = {
        "claude": _claude_menubar_icon,
        "codex": _codex_menubar_icon,
        "agy": _agy_menubar_icon,
        "grok": _grok_menubar_icon,
    }
    for provider, text in _title_segments(app, state):
        if title.length() > 0:
            title.appendAttributedString_(_menubar_text_string(app, "  "))
        icon = icons.get(provider)
        if icon is not None:
            title.appendAttributedString_(_menubar_icon_attachment_string(icon()))
            text = f" {text}"
        title.appendAttributedString_(_menubar_text_string(app, text))
    if title.length() == 0:
        # Both providers hidden: keep a recognizable, clickable status item.
        title.appendAttributedString_(_menubar_icon_attachment_string(_claude_menubar_icon()))
    return title


def _set_button_title(app: _TitleApp, state: PopoverState) -> None:
    title = _compose_title(app, state)
    title_key = tuple(_title_segments(app, state))
    if app._last_button_title_key == title_key:
        return

    button = app.status_item.button()
    plain_key = (title,)
    if app._last_plain_title_key != plain_key:
        button.setTitle_(title)
        app._last_plain_title_key = plain_key
    button.setAttributedTitle_(_menubar_attributed_title(app, state))
    app._last_button_title_key = title_key


def _compose_title(app: _TitleApp, state: PopoverState) -> str:
    parts = [text for _provider, text in _title_segments(app, state)]
    # Both providers hidden: keep a recognizable, clickable status item.
    return " · ".join(parts) if parts else "usage"


def _title_segments(app: _TitleApp, state: PopoverState) -> list[tuple[str, str]]:
    segments: dict[str, str] = {}
    if not state.hide_claude:
        segments["claude"] = (
            "--"
            if state.claude_session.percent is None
            else f"{_format_percent(state.claude_session.percent)}%"
        )
    if not state.hide_codex and (app.codex_5h_pct is not None or state.hide_claude):
        segments["codex"] = (
            "--"
            if app.codex_5h_pct is None
            else f"{_format_percent(float(app.codex_5h_pct))}%"
        )
    if not state.hide_agy and state.agy_session.percent is not None:
        segments["agy"] = f"{_format_percent(state.agy_session.percent)}%"
    if not state.hide_grok and state.grok_weekly.percent is not None:
        segments["grok"] = f"{_format_percent(state.grok_weekly.percent)}%"
    openrouter = _openrouter_title_text(state)
    if openrouter is not None:
        segments["openrouter"] = openrouter
    return [(provider, segments[provider]) for provider in state.card_order if provider in segments]


def _openrouter_title_text(state: PopoverState) -> str | None:
    quota = state.openrouter
    if state.hide_openrouter or quota is None:
        return None
    if quota.account_remaining is not None:
        return f"OR {quota.account_remaining}"
    if quota.key_unlimited:
        return _t(state.language, "openrouter_unlimited_headline")
    if quota.key_remaining is not None:
        return _t(
            state.language,
            "openrouter_key_headline",
            amount=quota.key_remaining,
        )
    return None
