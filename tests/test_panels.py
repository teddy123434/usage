# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>
#
# Part of "usage". Free software licensed under the GNU Affero General Public
# License v3.0 only; see the LICENSE file for full terms and the warranty disclaimer.

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

import panels
from panels.base import (
    ACTIVE_PANEL_DEFAULTS_KEY,
    load_active_panel_id,
    next_panel_eviction_id,
    save_active_panel_id,
)
from panels.dynamic_height import CONTENT_HEIGHT_SCRIPT
from panels.web_panel import HTMLPanel
from panels.window_drag import WINDOW_DRAG_SCRIPT

CARD_PANEL_FILENAMES = (
    "aquarium.html",
    "black_hole.html",
    "catppuccin.html",
    "classic.html",
    "cloud_observation.html",
    "lepidoptera.html",
    "matrix.html",
    "migration.html",
    "newspaper.html",
    "origami.html",
    "prism_arcade.html",
    "stained_glass.html",
    "win95.html",
)
NON_TALENT_PANEL_FILENAMES = (*CARD_PANEL_FILENAMES, "world_cup.html")


class FakeDefaults:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.synchronized = False

    def stringForKey_(self, key: str) -> str | None:
        return self.values.get(key)

    def setObject_forKey_(self, value: str, key: str) -> None:
        self.values[key] = value

    def synchronize(self) -> None:
        self.synchronized = True


def test_registered_panel_ids_are_unique() -> None:
    ids = panels.panel_ids()

    assert ids == (
        "classic",
        "matrix",
        "win95",
        "newspaper",
        "cloud_observation",
        "aquarium",
        "prism_arcade",
        "stained_glass",
        "migration",
        "origami",
        "black_hole",
        "lepidoptera",
        "world_cup",
        "talent_market",
        "catppuccin",
    )
    assert len(ids) == len(set(ids))


def test_registered_panel_i18n_keys() -> None:
    keys = [panel.i18n_key for panel in panels.all_panels()]

    assert keys == [
        "panel_default_name",
        "panel_matrix",
        "panel_win95",
        "panel_newspaper",
        "panel_cloud_observation",
        "panel_aquarium",
        "panel_prism_arcade",
        "panel_stained_glass",
        "panel_migration",
        "panel_origami",
        "panel_black_hole",
        "panel_lepidoptera",
        "panel_world_cup",
        "panel_talent_market",
        "panel_catppuccin",
    ]


def test_classic_panel_preferred_size() -> None:
    panel = panels.get_panel("classic")

    assert panel.preferred_size() == (364.0, 1132.0)


def test_catppuccin_panel_preferred_size() -> None:
    panel = panels.get_panel("catppuccin")

    assert panel.preferred_size() == (364.0, 1166.0)


def test_catppuccin_flavor_is_resolved_after_cached_html_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import prefs
    from wintray import app as wintray

    monkeypatch.setattr(prefs, "PREFERENCES_FILE", tmp_path / "usage-preferences.json")

    html = wintray.panel_html("catppuccin.html")

    assert "{{PANEL_FLAVOR}}" not in html
    assert 'data-flavor="mocha"' in html


def test_win95_panel_preferred_size() -> None:
    panel = panels.get_panel("win95")

    assert panel.preferred_size() == (364.0, 1183.0)


def test_html_panels_place_analyze_and_cli_in_project_header() -> None:
    panel_dir = Path(__file__).resolve().parent.parent / "assets" / "panels"

    for panel_path in sorted(panel_dir.glob("*.html")):
        # talent_market is a non-quota marketplace panel (talent packs → roles),
        # so it has no project range / analyze / CLI / footer affordances.
        if panel_path.name == "talent_market.html":
            continue
        html = panel_path.read_text(encoding="utf-8")
        project_index = html.index('data-action="toggle-project-range"')
        footer_index = html.index('<section class="footer"')
        analyze_index = html.index('data-action="analyze"')
        cli_index = html.index('data-action="toggle-statusline"')

        assert project_index < analyze_index < footer_index, panel_path.name
        assert project_index < cli_index < footer_index, panel_path.name
        assert html.count('data-action="analyze"') == 1, panel_path.name
        assert "data-cli-panel" not in html
        assert "localStorage" not in html
        assert "renderCliStatus" not in html
        assert "cli-status" not in html
        assert 'class="action" data-action="analyze"' not in html


def test_html_panel_rows_are_initialized_once() -> None:
    from panels.payload import _load_panel_html

    for filename in CARD_PANEL_FILENAMES:
        html = _load_panel_html(filename)
        assert 'el.dataset.rowReady !== "true"' in html, filename
        assert 'el.dataset.rowReady = "true"' in html, filename


def test_quota_panels_hide_absent_codex_rows_with_dom_api() -> None:
    from panels.payload import _load_panel_html

    for filename in CARD_PANEL_FILENAMES:
        html = _load_panel_html(filename)
        assert 'el.hidden = card === "codex" && !row;' in html, filename


def test_classic_panel_renders_codex_credits_with_dom_api() -> None:
    core_path = Path(__file__).resolve().parent.parent / "assets" / "panels" / "panel_core.js"
    core_script = core_path.read_text(encoding="utf-8")

    assert "function renderCodexCredits(credits)" in core_script
    assert 'document.createElement("div")' in core_script
    assert "if (!credits)" in core_script
    assert "existing.remove()" in core_script
    assert 't("codex_credits_unlimited")' in core_script
    assert 't("codex_credits", { balance: credits.balance || "--" })' in core_script


def test_shared_core_exposes_optional_system_accent_as_css_variable() -> None:
    core_path = Path(__file__).resolve().parent.parent / "assets" / "panels" / "panel_core.js"
    core_script = core_path.read_text(encoding="utf-8")

    assert (
        'root.style.setProperty("--usage-system-accent", state.system_accent_color)'
        in core_script
    )
    assert 'root.style.removeProperty("--usage-system-accent")' in core_script


def test_shared_core_cycles_through_yesterday_and_updates_footer() -> None:
    core_path = Path(__file__).resolve().parent.parent / "assets" / "panels" / "panel_core.js"
    core_script = core_path.read_text(encoding="utf-8")

    assert 'range === "yesterday"' in core_script
    assert 'state.projectsYesterday' in core_script
    assert 'state.footer.yesterday' in core_script
    assert 'window.usageRequestContentHeight()' in core_script


@pytest.mark.parametrize("filename", CARD_PANEL_FILENAMES)
def test_card_panels_use_shared_core_script(filename: str) -> None:
    panel_path = Path(__file__).resolve().parent.parent / "assets" / "panels" / filename
    html = panel_path.read_text(encoding="utf-8")

    assert html.count("{{CORE_SCRIPT}}") == 1
    assert "function renderRow(" not in html
    assert "function usageApplyState(" not in html
    assert "function renderCodexStale(" not in html


@pytest.mark.parametrize("filename", CARD_PANEL_FILENAMES)
def test_card_panels_expose_grok_dom_contract(filename: str) -> None:
    panel_path = Path(__file__).resolve().parent.parent / "assets" / "panels" / filename
    html = panel_path.read_text(encoding="utf-8")

    assert '<section class="card" data-card="grok"' in html
    assert "data-grok-stale" in html
    assert "data-grok-stale-age" in html
    assert "data-grok-stale-tooltip" in html


@pytest.mark.parametrize("filename", NON_TALENT_PANEL_FILENAMES)
def test_card_panels_expose_one_openrouter_dom_contract(filename: str) -> None:
    panel_path = Path(__file__).resolve().parent.parent / "assets" / "panels" / filename
    html = panel_path.read_text(encoding="utf-8")

    elements = re.findall(r'<[^/!][^>]*\bdata-card="openrouter"[^>]*>', html)
    assert len(elements) == 1, filename
    card_markup = re.search(
        r'<[^/!][^>]*\bdata-card="openrouter"[^>]*>.*?</section>',
        html,
        re.DOTALL,
    )
    assert card_markup is not None, filename
    assert 'data-action="refresh"' in card_markup.group(), filename
    for attribute in (
        "data-openrouter-headline",
        "data-openrouter-account-remaining",
        "data-openrouter-account-total",
        "data-openrouter-account-used",
        "data-openrouter-key-limit",
        "data-openrouter-key-remaining",
        "data-openrouter-key-percent",
        "data-openrouter-reset-interval",
        "data-openrouter-reset-at",
    ):
        elements = re.findall(rf'<[^/!][^>]*\b{attribute}(?:[=>\s])[^>]*>', html)
        assert len(elements) == 1, (filename, attribute)



def test_talent_market_stays_outside_quota_card_contract() -> None:
    panel_path = (
        Path(__file__).resolve().parent.parent / "assets" / "panels" / "talent_market.html"
    )

    assert 'data-card="openrouter"' not in panel_path.read_text(encoding="utf-8")


def test_shared_core_renders_sources_reauth_and_openrouter_with_text_only_dom() -> None:
    core_path = Path(__file__).resolve().parent.parent / "assets" / "panels" / "panel_core.js"
    core_script = core_path.read_text(encoding="utf-8")

    assert "function renderSourceProvenance(cardName, cardState)" in core_script
    assert 'dataset.freshness = freshness' in core_script
    assert 'freshness === "reauth_required"' in core_script
    assert "function renderOpenRouter(openrouter)" in core_script
    assert 'document.createElement("span")' in core_script
    assert ".textContent =" in core_script
    assert "row.hidden = !present" in core_script
    assert 'valueEl.textContent = present ? `${value}` : "--"' in core_script
    assert ".innerHTML" not in core_script
    assert 'const QUOTA_CARD_IDS = ["claude", "codex", "agy", "grok", "openrouter"]' in core_script
    assert "const knownOrder = order.filter" in core_script
    assert "state.hideOpenRouter" in core_script


def test_theme_html_does_not_implement_scrolling_or_zoom() -> None:
    panel_dir = Path(__file__).resolve().parent.parent / "assets" / "panels"

    for filename in NON_TALENT_PANEL_FILENAMES:
        html = (panel_dir / filename).read_text(encoding="utf-8").lower()
        assert "overflow-y" not in html, filename
        assert "scrollbar" not in html, filename
        assert "zoom:" not in html, filename


def test_shared_viewport_injection_owns_overflow_without_input_blockers() -> None:
    script = CONTENT_HEIGHT_SCRIPT

    assert "window.usageApplyPanelViewport" in script
    assert 'root.style.zoom = "normal"' in script
    assert 'root.style.overflowX = "hidden"' in script
    assert 'root.style.overflowY = scrollable ? "auto" : "hidden"' in script
    assert 'root.style.scrollbarGutter = "auto"' in script
    assert 'if (body) body.style.height = ""' in script
    assert "const y = window.scrollY" in script
    assert "Math.max(0, root.scrollHeight - root.clientHeight)" in script
    assert "window.scrollTo(0, Math.min(y, maxY))" in script
    assert "usageApplyPanelZoom" not in script
    assert 'addEventListener("wheel"' not in script
    assert 'addEventListener("keydown"' not in script
    assert "preventDefault" not in script


def test_shared_viewport_preserves_and_clamps_scroll_after_content_changes() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the executable viewport test")
    script = CONTENT_HEIGHT_SCRIPT.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = f"""
const frames = [];
const scrollCalls = [];
const style = {{}};
const body = {{ style: {{ height: "4000px" }} }};
const root = {{ style, scrollHeight: 1400, clientHeight: 500 }};
global.document = {{
  documentElement: root,
  body,
  querySelector() {{ return null; }},
  fonts: null
}};
global.MutationObserver = undefined;
global.ResizeObserver = undefined;
global.requestAnimationFrame = (callback) => {{ frames.push(callback); }};
global.window = {{
  scrollY: 700,
  scrollTo(x, y) {{ scrollCalls.push([x, y]); }},
  usageApplyState() {{ return true; }}
}};
eval({json.dumps(script)});
while (frames.length) frames.shift()();
window.usageApplyPanelViewport(true);
frames.shift()();
root.scrollHeight = 620;
window.scrollY = 700;
window.usageApplyPanelViewport(true);
frames.shift()();
window.scrollY = 120;
window.usageApplyPanelViewport(false);
frames.shift()();
console.log(JSON.stringify({{ style, bodyHeight: body.style.height, scrollCalls }}));
"""
    result = subprocess.run(
        [node, "-e", harness],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual == {
        "style": {
            "zoom": "normal",
            "overflowX": "hidden",
            "overflowY": "hidden",
            "scrollbarGutter": "auto",
        },
        "bodyHeight": "",
        "scrollCalls": [[0, 700], [0, 120], [0, 120]],
    }


def test_footer_remains_in_document_flow_for_shared_scrolling() -> None:
    assert "position" not in CONTENT_HEIGHT_SCRIPT
    panel_dir = Path(__file__).resolve().parent.parent / "assets" / "panels"
    for filename in NON_TALENT_PANEL_FILENAMES:
        html = (panel_dir / filename).read_text(encoding="utf-8")
        assert '<section class="footer"' in html, filename


def test_window_drag_excludes_interactive_and_scrollable_descendants_only() -> None:
    assert (
        'event.target.closest(interactiveSelector) || isScrollable(event.target)'
        in WINDOW_DRAG_SCRIPT
    )


def test_window_drag_runtime_keeps_blank_drag_and_rejects_interactive_scroll_targets() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the executable window-drag test")
    script = WINDOW_DRAG_SCRIPT.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = f"""
const messages = [];
let listener;
class Element {{
  constructor(parentElement = null, closestMatch = false, scrollable = false) {{
    this.parentElement = parentElement;
    this.closestMatch = closestMatch;
    this.scrollHeight = scrollable ? 200 : 100;
    this.clientHeight = 100;
    this.scrollWidth = 100;
    this.clientWidth = 100;
    this.overflowY = scrollable ? "auto" : "visible";
    this.overflowX = "visible";
  }}
  closest() {{ return this.closestMatch ? this : null; }}
}}
const body = new Element();
global.document = {{
  body,
  addEventListener(type, callback) {{ if (type === "mousedown") listener = callback; }}
}};
global.window = {{
  getComputedStyle(element) {{
    return {{ overflowY: element.overflowY, overflowX: element.overflowX }};
  }},
  webkit: {{
    messageHandlers: {{ usage: {{ postMessage(message) {{ messages.push(message); }} }} }}
  }}
}};
eval({json.dumps(script)});
const blank = new Element(body);
const nestedScroller = new Element(body, false, true);
const nestedChild = new Element(nestedScroller);
const button = new Element(body, true);
listener({{ button: 0, target: nestedChild }});
listener({{ button: 0, target: button }});
listener({{ button: 0, target: blank }});
console.log(JSON.stringify(messages));
"""
    result = subprocess.run(
        [node, "-e", harness],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        json.dumps({"action": "begin_window_drag"}, separators=(",", ":"))
    ]
    assert 'current !== document.body' in WINDOW_DRAG_SCRIPT
    assert 'overflowY === "auto" || overflowY === "scroll"' in WINDOW_DRAG_SCRIPT
    assert (
        'bridge.postMessage(JSON.stringify({ action: "begin_window_drag" }))'
        in WINDOW_DRAG_SCRIPT
    )


def test_state_payload_omits_codex_rows_with_empty_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import panels.web_panel as web_panel
    from menubar import agy as menubar_agy
    from menubar import app as menubar

    monkeypatch.setattr(menubar, "_load_preferences", lambda: {})
    monkeypatch.setattr(menubar_agy, "find_agy", lambda: None)
    state = menubar._empty_state()
    state.codex_session.title = ""

    payload = web_panel._state_payload(state)

    codex_payload = cast(dict[str, object], payload["codex"])
    assert "session" not in codex_payload
    assert "weekly" in codex_payload


def test_classic_project_header_expands_for_action_row() -> None:
    panel_path = Path(__file__).resolve().parent.parent / "assets" / "panels" / "classic.html"
    html = panel_path.read_text(encoding="utf-8")
    project_brand_css = html[
        html.index('.card[data-card="projects"] .brand {') :
        html.index('.card[data-card="projects"] .brand-icon {')
    ]

    assert '<div class="project-actions">' in html
    assert "display: grid;" in project_brand_css
    assert "height: auto;" in project_brand_css
    assert "margin-bottom: 10px;" in project_brand_css


def test_missing_panel_id_falls_back_to_classic() -> None:
    panel = panels.get_panel("missing")

    assert panel.id == "classic"


def test_defaults_load_falls_back_to_classic() -> None:
    defaults = FakeDefaults()

    assert load_active_panel_id(defaults) == "classic"


def test_defaults_round_trip() -> None:
    defaults = FakeDefaults()

    save_active_panel_id("classic", defaults)

    assert defaults.values[ACTIVE_PANEL_DEFAULTS_KEY] == "classic"
    assert load_active_panel_id(defaults) == "classic"
    assert defaults.synchronized is False


def test_next_panel_eviction_id_skips_active_and_pending_panels() -> None:
    panel_ids = ["classic", "matrix", "win95"]

    assert next_panel_eviction_id(panel_ids, "matrix", {"classic"}) == "win95"
    assert next_panel_eviction_id(panel_ids, "matrix", {"classic", "win95"}) is None


def test_load_panel_html_caches_assembled_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    import panels.payload as payload

    reads: list[str] = []
    panel_html = "{{CLAUDE_ICON}} {{CODEX_ICON}} {{CORE_SCRIPT}}"
    core_script = "{{I18N_BUNDLE}}"

    def read_text(self: Path, *, encoding: str) -> str:
        reads.append(str(self))
        return core_script if self.name == "panel_core.js" else panel_html

    monkeypatch.setattr(payload, "resolve_resource", lambda name: f"/tmp/{name}")
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(payload, "_data_uri", lambda name: f"data:{name}")
    monkeypatch.setattr(payload, "_load_i18n_bundle", lambda: {"en": {"title": "Usage"}})
    payload._load_panel_html.cache_clear()
    payload._load_core_script.cache_clear()

    try:
        assert payload._load_panel_html("classic.html") == payload._load_panel_html("classic.html")
        assert reads == ["/tmp/panels/classic.html", "/tmp/panels/panel_core.js"]
    finally:
        payload._load_panel_html.cache_clear()
        payload._load_core_script.cache_clear()


def test_html_panel_requires_explicit_card_heights() -> None:
    constructor: Any = HTMLPanel

    with pytest.raises(TypeError):
        constructor("test", "panel_test", "test.html")
    with pytest.raises(TypeError):
        constructor("test", "panel_test", "test.html", codex_card_height=100.0)
    with pytest.raises(TypeError):
        constructor("test", "panel_test", "test.html", claude_card_height=100.0)


def test_html_panel_dynamic_height_defaults_and_talent_market_opt_out() -> None:
    panel = HTMLPanel(
        "test",
        "panel_test",
        "test.html",
        claude_card_height=100.0,
        codex_card_height=100.0,
    )

    assert panel.dynamic_height is True
    assert cast(HTMLPanel, panels.get_panel("talent_market")).dynamic_height is False


@pytest.mark.parametrize("dynamic_height", [True, False])
def test_build_view_injects_content_height_script_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    dynamic_height: bool,
) -> None:
    import panels.web_panel as web_panel

    class FakeConfiguration:
        @classmethod
        def alloc(cls) -> FakeConfiguration:
            return cls()

        def init(self) -> FakeConfiguration:
            return self

        def setUserContentController_(self, controller: object) -> None:
            pass

    class FakeController:
        @classmethod
        def alloc(cls) -> FakeController:
            return cls()

        def init(self) -> FakeController:
            return self

        def addScriptMessageHandler_name_(self, bridge: object, name: str) -> None:
            pass

    class FakeWebView:
        @classmethod
        def alloc(cls) -> FakeWebView:
            return cls()

        def initWithFrame_configuration_delegate_(
            self,
            frame: object,
            configuration: object,
            delegate: object,
        ) -> FakeWebView:
            return self

        def setBridge_(self, bridge: object) -> None:
            pass

        def loadHTMLString_baseURL_(self, html: str, base_url: object | None) -> None:
            self.loaded_html = html

        def performSelector_withObject_afterDelay_(
            self,
            selector: str,
            value: object | None,
            delay: float,
        ) -> None:
            pass

    class FakeBridge:
        @classmethod
        def alloc(cls) -> FakeBridge:
            return cls()

        def initWithDelegate_webView_(
            self,
            delegate: object,
            web_view: object,
        ) -> FakeBridge:
            return self

    monkeypatch.setattr(web_panel, "WKWebViewConfiguration", FakeConfiguration)
    monkeypatch.setattr(web_panel, "WKUserContentController", FakeController)
    monkeypatch.setattr(web_panel, "WebPanelView", FakeWebView)
    monkeypatch.setattr(web_panel, "UsageScriptBridge", FakeBridge)
    monkeypatch.setattr(web_panel, "_panel_flavor", lambda: "mocha")
    monkeypatch.setattr(
        web_panel,
        "_load_panel_html",
        lambda filename: (
            '<html data-flavor="{{PANEL_FLAVOR}}"><body><script>'
            "window.usageApplyState = function() {};"
            "</script></body></html>"
        ),
    )
    panel = HTMLPanel(
        "test",
        "panel_test",
        "test.html",
        claude_card_height=100.0,
        codex_card_height=100.0,
        dynamic_height=dynamic_height,
    )

    view = panel.build_view(object())

    assert (CONTENT_HEIGHT_SCRIPT in view.loaded_html) is dynamic_height
    assert WINDOW_DRAG_SCRIPT in view.loaded_html
    assert "{{PANEL_FLAVOR}}" not in view.loaded_html
    assert 'data-flavor="mocha"' in view.loaded_html


def test_evaluate_javascript_completion_handler_block_signature() -> None:
    # Regression: when WebKit is pulled in via objc.loadBundle (no framework
    # wrapper, e.g. a background/non-app launch), evaluateJavaScript:
    # completionHandler: used to raise "Argument 3 is a block, but no signature
    # available" the moment a Python completion handler was passed, crashing the
    # panel popover. web_panel registers the block metadata to prevent this.
    import panels.web_panel as web_panel

    WKWebView = getattr(web_panel, "WKWebView", None)
    WKWebViewConfiguration = getattr(web_panel, "WKWebViewConfiguration", None)
    if WKWebView is None or WKWebViewConfiguration is None:
        pytest.skip("WebKit unavailable in this environment")

    # Instantiating WKWebView needs a window-server connection; without one (headless
    # CI, SSH, a non-GUI agent) the process aborts rather than raising. CGMainDisplayID
    # returns 0 when no display is attached — a safe read that lets us skip first.
    from Quartz import CGMainDisplayID

    if CGMainDisplayID() == 0:
        pytest.skip("no window server (headless) — cannot instantiate WKWebView")

    from AppKit import NSMakeRect

    config = WKWebViewConfiguration.alloc().init()
    view = WKWebView.alloc().initWithFrame_configuration_(
        NSMakeRect(0, 0, 10, 10), config
    )
    # Must not raise TypeError about the missing block signature.
    view.evaluateJavaScript_completionHandler_("1+1", lambda value, error: None)


def test_web_panel_context_menu_removes_navigation_items() -> None:
    import panels.web_panel as web_panel

    class FakeMenuItem:
        def __init__(self, identifier: str | None) -> None:
            self.identifier = identifier

        def itemIdentifier(self) -> str | None:
            return self.identifier

    class FakeMenu:
        def __init__(self) -> None:
            self.reload = FakeMenuItem("WKMenuItemIdentifierReload")
            self.copy = FakeMenuItem("WKMenuItemIdentifierCopy")
            self.open_link = FakeMenuItem("WKMenuItemIdentifierOpenLinkInNewWindow")
            self.no_identifier = FakeMenuItem(None)
            self.items = [
                self.reload,
                self.copy,
                self.open_link,
                self.no_identifier,
            ]

        def itemArray(self) -> list[FakeMenuItem]:
            return self.items

        def removeItem_(self, item: FakeMenuItem) -> None:
            self.items.remove(item)

    menu = FakeMenu()

    web_panel._remove_navigation_menu_items(menu)

    assert menu.items == [menu.copy, menu.no_identifier]


def test_build_view_falls_back_to_error_panel_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the WKWebView panel can't be built/loaded, build_view must return a
    # native ErrorPanelView instead of degrading to a silent grey popover, and
    # that view must satisfy the injectState_/teardown surface the controller
    # drives it with. Failing inside _load_panel_html (before the WKWebView is
    # instantiated) keeps this independent of a window server.
    from Quartz import CGMainDisplayID

    import panels.web_panel as web_panel
    from panels.web_panel import ErrorPanelView

    if CGMainDisplayID() == 0:
        pytest.skip("no window server (headless) — cannot instantiate ErrorPanelView")

    def boom(_filename: str) -> str:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(web_panel, "_load_panel_html", boom)

    panel = HTMLPanel(
        "test",
        "panel_default_name",
        "test.html",
        claude_card_height=100.0,
        codex_card_height=100.0,
    )

    class Delegate:
        language = "en"

    view = panel.build_view(Delegate())
    assert isinstance(view, ErrorPanelView)
    view.injectState_({})  # no-op, must not raise
    view.teardown()  # no-op, must not raise


def test_web_panel_reloads_and_reinjects_after_content_termination() -> None:
    import panels.web_panel as web_panel

    class FakeWebView:
        def __init__(self) -> None:
            self._ready = True
            self._pending = None
            self._last_payload = {"codex": {"session": {"percent": 67}}}
            self._last_injected_payload = "previous"
            self._first_paint_done = True
            self._html = "<html>usage</html>"
            self.reloads = 0
            self.loaded_html: list[tuple[str, object | None]] = []

        def reload(self) -> None:
            self.reloads += 1

        def loadHTMLString_baseURL_(self, html: str, base_url: object | None) -> None:
            self.loaded_html.append((html, base_url))

    view = FakeWebView()

    web_panel._reload_web_panel(view)

    assert view._ready is False
    assert view._first_paint_done is False
    assert view._last_injected_payload is None
    assert view._pending == {"codex": {"session": {"percent": 67}}}
    assert view.loaded_html == [("<html>usage</html>", None)]
    assert view.reloads == 0


def test_web_panel_reloads_when_state_injection_fails() -> None:
    import panels.web_panel as web_panel

    class FakeWebView:
        def __init__(self) -> None:
            self._ready = True
            self._pending = None
            self._last_payload = None
            self._html = "<html>usage</html>"
            self.reloads = 0
            self.loaded_html: list[tuple[str, object | None]] = []

        def reload(self) -> None:
            self.reloads += 1

        def loadHTMLString_baseURL_(self, html: str, base_url: object | None) -> None:
            self.loaded_html.append((html, base_url))

    view = FakeWebView()
    payload: dict[str, object] = {"projects": [{"name": "Eric-Tools"}]}

    web_panel._handle_injection_error(view, payload, "boom")

    assert view._ready is False
    assert view._pending == payload
    assert view.loaded_html == [("<html>usage</html>", None)]
    assert view.reloads == 0


def test_web_panel_skips_duplicate_state_serialization_and_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import panels.web_panel as web_panel

    class FakeWebView:
        def __init__(self) -> None:
            self._last_injected_payload = None
            self._last_injected_state = None

    view = FakeWebView()
    payload: dict[str, object] = {"projects": [{"name": "Eric-Tools"}]}
    calls = 0
    original_dumps: Any = json.dumps

    def dumps(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return cast(str, original_dumps(*args, **kwargs))

    monkeypatch.setattr("panels.payload.json.dumps", dumps)

    first = web_panel._new_state_payload(view, payload)
    duplicate = web_panel._new_state_payload(view, payload.copy())

    assert first == '{"projects":[{"name":"Eric-Tools"}]}'
    assert duplicate is None
    assert calls == 1


def test_web_panel_caps_reloads_when_state_injection_keeps_failing() -> None:
    import panels.web_panel as web_panel

    class FakeWebView:
        def __init__(self) -> None:
            self._ready = True
            self._pending = None
            self._last_payload = None
            self._injection_retry_payload = None
            self._injection_reload_count = 0
            self._html = "<html>usage</html>"
            self.reloads = 0
            self.loaded_html: list[tuple[str, object | None]] = []

        def reload(self) -> None:
            self.reloads += 1

        def loadHTMLString_baseURL_(self, html: str, base_url: object | None) -> None:
            self.loaded_html.append((html, base_url))

    view = FakeWebView()
    payload: dict[str, object] = {"projects": [{"name": "Eric-Tools"}]}

    for _ in range(web_panel.MAX_INJECTION_RELOADS + 3):
        web_panel._handle_injection_error(view, payload, "boom")

    assert view.loaded_html == [
        ("<html>usage</html>", None),
        ("<html>usage</html>", None),
    ]
    assert view.reloads == 0
    assert view._pending is None

    web_panel._reload_web_panel(view)

    assert view.loaded_html == [
        ("<html>usage</html>", None),
        ("<html>usage</html>", None),
        ("<html>usage</html>", None),
    ]
    assert view.reloads == 0


def test_web_panel_reload_falls_back_to_reload_without_html() -> None:
    import panels.web_panel as web_panel

    class FakeWebView:
        def __init__(self) -> None:
            self._ready = True
            self._pending = None
            self._last_payload = None
            self._html = None
            self.reloads = 0

        def reload(self) -> None:
            self.reloads += 1

    view = FakeWebView()

    web_panel._reload_web_panel(view)

    assert view._ready is False
    assert view._pending is None
    assert view.reloads == 1
