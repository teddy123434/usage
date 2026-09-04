# Usage OCX Quota Sources and Scrollable Panels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Usage select Native or OCX quota per provider, add an OCX-only OpenRouter balance card, and replace panel shrink-to-fit with fixed-scale vertical scrolling.

**Architecture:** A single bounded `ocx provider quota --json` bridge produces normalized immutable snapshots for all OCX-backed providers. A pure source selector chooses one snapshot per logical card before presentation. Existing native loaders remain intact. Shared panel injection/window sizing owns overflow behavior so themes do not implement independent scrolling.

**Tech Stack:** Python 3.13, PyObjC/WebKit on macOS, pywebview on Windows, HTML/CSS/JavaScript panels, pytest, mypy, Ruff.

**Spec:** `../specs/2026-09-04-ocx-quota-source-integration-design.md`

## Global Constraints

- Work in `/Users/teddy/Programming/agent-workspace/usage` on the existing `codex/ocx-quota-sources` branch.
- Complete the OCX contract plan first or use redacted fixtures with the exact additive schema it defines.
- Never read `~/.opencodex/config.json`, the OCX admin token, provider keys, Keychain entries, or provider APIs from Usage.
- Invoke a resolved `ocx` executable with a fixed argv list and `shell=False`; cap time, stdout, stderr, and concurrent processes.
- Native loaders and status-line scripts keep their current behavior. They must not start OCX or perform quota subprocess work.
- Unknown/missing data is unavailable, never `0%`. A real returned `0%` remains valid.
- Forced source modes never silently substitute the other source. Auto mode follows the approved deterministic selection order.
- Do not alter token/cost/history aggregation.
- Panel width and CSS scale stay at `1.0`; only vertical viewport height is capped.

---

## File Structure

### Create

- `loaders/ocx_quota.py` — bounded CLI runner, JSON parser, normalized models, last-good cache, freshness classification, and single-flight refresh coordinator.
- `menubar/quota_sources.py` — pure Native/OCX selection policy and conservative route-evidence probes.
- `menubar/openrouter.py` — OpenRouter headline/detail projection.
- `tests/fixtures/ocx_provider_quota.json` — redacted five-provider fixture matching the OCX public contract.
- `tests/test_ocx_quota.py` — parsing, bounds, timeouts, cache, freshness, and concurrency tests.
- `tests/test_quota_sources.py` — explicit/auto/route/single-source/ambiguous source-selection tests.
- `tests/test_menubar_openrouter.py` — OpenRouter display semantics.

### Modify

- `menubar/prefs.py` — per-provider source mode, global `prefer_ocx`, OpenRouter visibility, and append-only card-order migration.
- `menubar/state.py` — normalized selected-source state, source badges, OpenRouter state, aggregate/current-account detail.
- `menubar/refresh.py` — load one OCX snapshot off the UI thread, select sources, and isolate failures.
- `menubar/title.py` — render a compact `OR $balance` segment in the status item without adding another binary icon asset.
- `menubar/menu.py` and `menubar/app.py` — macOS source-mode and OpenRouter visibility controls.
- `wintray/menu.py` and `wintray/app.py` — equivalent Windows controls and refresh integration.
- `panels/payload.py` — serialize source/freshness/OpenRouter fields.
- `assets/panels/panel_core.js` — render source badges, OpenRouter details, ordering, visibility, drag exclusions, and shared scroll state.
- Every card theme in `assets/panels/*.html` except `talent_market.html` — add only the OpenRouter card DOM contract and theme-native variables/styles; do not add overflow rules.
- `i18n.json` — source, stale/reauth, OpenRouter, limit, reset, and menu labels for all existing locales.
- `panels/panel_scale.py` — replace scale calculation with fixed-width viewport capping.
- `panels/dynamic_height.py` — inject shared vertical overflow behavior and scroll preservation.
- `menubar/popover.py` — stop applying CSS zoom and size to capped viewport height.
- `tests/test_menubar_prefs.py`, `tests/test_menubar.py`, `tests/test_menubar_state.py`, `tests/test_web_panel_payload.py`, `tests/test_panels.py`, `tests/test_panel_scale.py`, and `tests/test_wintray.py` — integration/regression coverage.

---

### Task 1: Freeze the OCX fixture and normalized model

- [ ] Add `tests/fixtures/ocx_provider_quota.json` with redacted reports for:

```text
openai -> codex
anthropic -> claude
google-antigravity -> antigravity
xai -> grok
openrouter -> openrouter
```

The fixture must include `0%`, reset timestamps, `customWindows`, OpenAI aggregate/current-account metadata already emitted by OCX, OpenRouter `accountCreditsUsd`/`creditsUsd`, and a non-secret `statuses` array containing an isolated `reauth_required` row. Add one unknown provider to prove it is ignored.

- [ ] Write failing parser tests in `tests/test_ocx_quota.py` around these immutable models:

```python
type LogicalProvider = Literal["claude", "codex", "antigravity", "grok", "openrouter"]
type QuotaSource = Literal["native", "ocx"]
type Freshness = Literal["fresh", "stale", "unavailable", "reauth_required"]

@dataclass(frozen=True, slots=True)
class NormalizedQuotaSnapshot:
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
    providers: Mapping[LogicalProvider, NormalizedQuotaSnapshot]
    generated_at: datetime
    cache_written_at: datetime
```

Keep `details` restricted to a whitelisted, non-secret subset required for OpenAI aggregate/current-account presentation; do not retain raw OCX report objects.

- [ ] Run and confirm failure:

```bash
uv run pytest -q tests/test_ocx_quota.py
```

- [ ] Implement the exact pure boundary `parse_ocx_quota_payload(payload: object, now: datetime) -> OcxQuotaSnapshot` in `loaders/ocx_quota.py`.

Rules:

  - require a top-level object and a list-like `reports` field;
  - whitelist the five provider IDs and ignore unknown rows;
  - reject booleans, NaN, infinities, malformed timestamps, and percentages outside the accepted OCX wire domain;
  - preserve valid zero values;
  - map each bad provider row to unavailable without discarding valid siblings;
  - classify OCX `statuses[].status == "reauth_required"` as `reauth_required` without retaining bodies/messages.

- [ ] Run parser tests and static checks:

```bash
uv run pytest -q tests/test_ocx_quota.py
uv run mypy loaders/ocx_quota.py
uv run ruff check loaders/ocx_quota.py tests/test_ocx_quota.py
```

- [ ] Commit normalized parsing:

```bash
git add loaders/ocx_quota.py tests/fixtures/ocx_provider_quota.json tests/test_ocx_quota.py
git commit -m "feat(quota): normalize OCX provider reports"
```

---

### Task 2: Build the bounded OCX runner, cache, and single-flight refresh

- [ ] Add failing tests to `tests/test_ocx_quota.py` for:

  - executable resolution from an explicit test path and `PATH`, with no shell.
  - exact argv `("ocx", "provider", "quota", "--json")` and forced-refresh argv including `--refresh`.
  - 2-second cached-read timeout and 20-second forced-refresh timeout.
  - 1 MiB stdout/stderr cap, including a process that streams past the cap.
  - non-zero exit, timeout, malformed JSON, missing binary, and interrupted process.
  - a failed run never replacing the last-good file.
  - atomic write via a sibling temporary file, `fsync`, `os.replace`, and mode `0o600`.
  - age boundaries: `<=10m` fresh, `>10m && <=24h` stale, `>24h` unavailable.
  - ten callers sharing one in-flight subprocess.
  - forced refresh starting at most once per five-minute monotonic interval.

- [ ] Run and confirm the process/cache tests fail:

```bash
uv run pytest -q tests/test_ocx_quota.py -k "runner or cache or single_flight or freshness"
```

- [ ] Implement this public boundary in `loaders/ocx_quota.py`:

```python
OCX_CACHE_PATH = Path.home() / ".usage" / "ocx-quota.json"
MAX_OUTPUT_BYTES = 1024 * 1024
CACHED_READ_TIMEOUT_S = 2.0
FORCED_REFRESH_TIMEOUT_S = 20.0
FORCED_REFRESH_INTERVAL_S = 300.0
```

Expose `load_ocx_quota(*, request_refresh: bool = False) -> OcxQuotaSnapshot` and `request_ocx_refresh() -> None`.

Use `subprocess.Popen` with pipes so readers can terminate a child as soon as either output bound is exceeded. Guard execution with one module lock/condition. `load_ocx_quota()` may synchronously perform the two-second cached read off the UI thread; `request_ocx_refresh()` starts one daemon refresh and returns immediately.

- [ ] Ensure cache JSON contains only the normalized whitelist and a schema version, never raw subprocess stderr or full OCX payload.

- [ ] Run focused tests and static checks:

```bash
uv run pytest -q tests/test_ocx_quota.py
uv run mypy loaders/ocx_quota.py
uv run ruff check loaders/ocx_quota.py tests/test_ocx_quota.py
```

- [ ] Commit process and cache behavior:

```bash
git add loaders/ocx_quota.py tests/test_ocx_quota.py
git commit -m "feat(quota): cache bounded OCX quota snapshots"
```

---

### Task 3: Implement deterministic source selection and preferences

- [ ] Add failing tests in `tests/test_quota_sources.py` and `tests/test_menubar_prefs.py` for this pure selector:

Use `SourceMode = Literal["auto", "native", "ocx"]` and this immutable result:

```python
@dataclass(frozen=True, slots=True)
class SourceSelection:
    selected: NormalizedQuotaSnapshot | None
    source: QuotaSource | None
    reason: Literal["explicit", "route", "single", "retained", "native_first", "unavailable"]
```

Implement `select_quota_source()` with keyword-only `mode`, `native`, `ocx`, `proven_ocx_route`, and `last_selected` parameters, returning `SourceSelection`.

Required matrix:

  - explicit `native`/`ocx` wins and never substitutes;
  - auto + proven OCX route selects OCX;
  - auto + one displayable source selects it;
  - auto + both + ambiguous retains last selected;
  - first ambiguous selection uses native;
  - a stale selected source remains selected until it exceeds 24 hours;
  - `prefer_ocx` supplies the initial last/source preference but does not override an explicit per-provider mode.

- [ ] Add preference APIs to `menubar/prefs.py`:

```python
DUAL_SOURCE_PROVIDERS = ("claude", "codex", "antigravity", "grok")
SOURCE_MODES = ("auto", "native", "ocx")
```

Expose `_quota_source_mode(provider, prefs) -> SourceMode`, `_save_quota_source_mode(provider, mode) -> bool`, `_quota_last_source(provider, prefs) -> QuotaSource | None`, `_save_quota_last_source(provider, source) -> bool`, and `_prefer_ocx_enabled(prefs) -> bool` with the existing optional-preferences convention.

Persist per-provider modes under `quota_source_modes`, last selections under `quota_last_sources`, and the global flag under `prefer_ocx`. Default `prefer_ocx` is false.

- [ ] Implement conservative route evidence in `menubar/quota_sources.py`:

  - Codex: only an OCX-owned marker/provider row plus a loopback base URL is proof.
  - Grok: only the complete OCX managed block plus its loopback `base_url` is proof.
  - Claude: only a loopback `ANTHROPIC_BASE_URL` paired with OCX's `ANTHROPIC_AUTH_TOKEN=opencodex-proxy` ownership marker is proof; absence is ambiguous.
  - Antigravity: no stable native route marker exists in this release, so it remains ambiguous and follows last-selection/native-first/global preference rules.

Never classify a bare user-authored loopback URL as OCX ownership.

- [ ] Run source and preference tests:

```bash
uv run pytest -q tests/test_quota_sources.py tests/test_menubar_prefs.py
uv run mypy menubar/quota_sources.py menubar/prefs.py
uv run ruff check menubar/quota_sources.py menubar/prefs.py tests/test_quota_sources.py tests/test_menubar_prefs.py
```

- [ ] Commit selection policy:

```bash
git add menubar/quota_sources.py menubar/prefs.py tests/test_quota_sources.py tests/test_menubar_prefs.py
git commit -m "feat(quota): select Native or OCX per provider"
```

---

### Task 4: Integrate selected OCX snapshots into refresh/state

- [ ] Add failing integration tests to `tests/test_menubar.py` and `tests/test_menubar_state.py` proving:

  - one OCX load supplies all providers in a refresh;
  - an OCX exception cannot hide native rows or unrelated OCX rows;
  - OCX can make Antigravity/Grok visible when native executables/logs are absent;
  - OpenAI aggregate pool values are the Codex headline while current-account values stay in labeled detail;
  - selected source/freshness is retained in state;
  - native token/history rows remain unchanged.

- [ ] Extend `PopoverState` with source metadata and OpenRouter state. Keep provider presentation grouped rather than adding parallel scalar fields:

```python
@dataclass(frozen=True, slots=True)
class ProviderSourceState:
    source: QuotaSource
    label: str
    freshness: Freshness
    age_text: str | None

@dataclass(frozen=True, slots=True)
class OpenRouterState:
    headline: str
    account_remaining: str | None
    account_total: str | None
    account_used: str | None
    key_limit: str | None
    key_remaining: str | None
    key_percent: float | None
    reset_text: str | None
    key_unlimited: bool
```

- [ ] Add native-to-normalized adapters without changing native loaders. Keep Claude adaptation beside current poll snapshot handling, Codex adaptation beside `codex_result`, Antigravity in `menubar/agy.py`, and Grok in `menubar/grok.py`.

- [ ] Update `menubar/refresh.py` so `load_sources()` obtains one OCX snapshot after native loads, calls the selector four times, projects OpenRouter, and queues `request_ocx_refresh()` after the visible state is built.

- [ ] Make failure states preserve prior selected source and stale data rather than rebuilding missing quota as zero.

- [ ] Run focused integration tests:

```bash
uv run pytest -q tests/test_menubar.py tests/test_menubar_state.py tests/test_menubar_agy.py tests/test_menubar_grok.py tests/test_menubar_openrouter.py
uv run mypy menubar loaders/ocx_quota.py
```

- [ ] Commit refresh integration:

```bash
git add menubar/state.py menubar/refresh.py menubar/agy.py menubar/grok.py menubar/openrouter.py tests/test_menubar.py tests/test_menubar_state.py tests/test_menubar_agy.py tests/test_menubar_grok.py tests/test_menubar_openrouter.py
git commit -m "feat(menubar): project selected OCX quota sources"
```

---

### Task 5: Add source controls and OpenRouter menu-bar output

- [ ] Add failing preference/menu/title tests for:

  - an OpenRouter hide toggle on macOS and Windows;
  - source submenus for Claude, Codex, Antigravity, and Grok with radio choices Auto/Native/OCX;
  - a global Prefer OCX toggle;
  - selecting a mode persists it and triggers refresh;
  - existing saved card orders append `openrouter` exactly once;
  - OpenRouter headline uses account remaining, otherwise active-key remaining with a key-scope label;
  - unavailable account and key data emits no false `$0.00`.

- [ ] Update the default card order exactly as follows:

```python
DEFAULT_QUOTA_CARD_ORDER = ("claude", "codex", "agy", "grok", "openrouter")
```

Map the logical `antigravity` source preference to the existing `agy` presentation card only at the UI boundary.

- [ ] Add macOS selectors in `menubar/app.py` and menu construction in `menubar/menu.py`; add matching descriptors/actions in `wintray/menu.py` and `wintray/app.py`.

- [ ] Render OpenRouter as a compact text segment such as `OR $74.75` in `menubar/title.py`. Keep provider ordering consistent with `state.card_order`; do not hard-code OpenRouter ahead of user-reordered cards.

- [ ] Add all new strings to every locale already present in `i18n.json`; tests must compare locale key sets.

- [ ] Run focused tests:

```bash
uv run pytest -q tests/test_menubar_prefs.py tests/test_menubar.py tests/test_wintray.py tests/test_menubar_openrouter.py tests/test_i18n_key_parity.py tests/test_i18n_format_fallback.py
uv run mypy menubar wintray
uv run ruff check menubar wintray tests/test_menubar_prefs.py tests/test_menubar_openrouter.py
```

- [ ] Commit controls and title output:

```bash
git add menubar/prefs.py menubar/menu.py menubar/app.py menubar/title.py wintray/menu.py wintray/app.py i18n.json tests/test_menubar_prefs.py tests/test_menubar.py tests/test_wintray.py tests/test_menubar_openrouter.py tests/test_i18n_key_parity.py tests/test_i18n_format_fallback.py
git commit -m "feat(ui): expose quota sources and OpenRouter balance"
```

---

### Task 6: Add OpenRouter card payload and theme contracts

- [ ] Add failing payload/core/theme tests in `tests/test_web_panel_payload.py` and `tests/test_panels.py` for:

  - `source`, `sourceLabel`, `freshness`, and stale age on every quota card;
  - structured OpenRouter account/key fields;
  - `hideOpenRouter` and five-card ordering;
  - source badge and reauth marker DOM;
  - every non-talent theme having one `[data-card="openrouter"]` section;
  - no theme HTML containing `overflow-y`, scrollbar, or zoom implementation.

- [ ] Extend `_state_payload()` in `panels/payload.py` with normalized source objects and OpenRouter data. Do not serialize raw `details` mappings.

- [ ] Update `assets/panels/panel_core.js`:

  - include OpenRouter in card ordering, drag/drop, hide state, and switch-button fallback;
  - create/render account and key detail rows from text-only DOM operations;
  - render source badges and `reauth_required` consistently;
  - preserve unknown/missing values as `--` and hide absent detail rows;
  - keep user strings in `textContent`, never `innerHTML`.

- [ ] Add the OpenRouter card DOM contract to:

```text
aquarium.html
black_hole.html
catppuccin.html
classic.html
cloud_observation.html
lepidoptera.html
matrix.html
migration.html
newspaper.html
origami.html
prism_arcade.html
stained_glass.html
win95.html
world_cup.html
```

Keep each theme's visual language, but use the same `data-card`, `data-openrouter-*`, and action attributes. `talent_market.html` remains outside the quota-card contract.

- [ ] Run panel contract tests:

```bash
uv run pytest -q tests/test_web_panel_payload.py tests/test_panels.py
uv run ruff check panels tests/test_web_panel_payload.py tests/test_panels.py
```

- [ ] Commit panel presentation:

```bash
git add panels/payload.py assets/panels i18n.json tests/test_web_panel_payload.py tests/test_panels.py
git commit -m "feat(panels): render OpenRouter and source provenance"
```

---

### Task 7: Replace shrink-to-fit with fixed-scale vertical scrolling

- [ ] Replace the old scale expectations in `tests/test_panel_scale.py` with failing viewport tests for this exact contract:

```python
@dataclass(frozen=True, slots=True)
class PanelViewport:
    width: float
    height: float
    natural_height: float
    scrollable: bool
```

Implement `fit_panel_viewport(natural_width, natural_height, maximum_height) -> PanelViewport` with the same untrusted-number validation discipline as the current `fit_panel_size()`.

Expected values:

```text
380 x 900, max 921  -> width 380, height 900,  scrollable false
380 x 1183, max 921 -> width 380, height 921,  scrollable true
380 x 4000, max 921 -> width 380, height 921,  scrollable true
```

Invalid/non-positive maxima preserve natural size and disable scrolling. There is no minimum scale because scale is always 1.

- [ ] Add failing JavaScript-injection tests to `tests/test_panels.py`, `tests/test_menubar.py`, and `tests/test_wintray.py` for:

  - vertical scrolling only when `scrollable` is true;
  - `overflow-x: hidden` always;
  - no call to `usageApplyPanelZoom`;
  - trackpad/wheel/native WebView scroll, Page Up, and Page Down remaining enabled;
  - scroll position surviving refresh and cached-theme switches;
  - scroll position clamped after content removal;
  - footer moving with document content;
  - draggable window behavior excluding interactive scroll descendants.

- [ ] Implement `fit_panel_viewport()` in `panels/panel_scale.py` and remove `MIN_PANEL_SCALE`, `fit_scale`, and `fit_panel_size` only after all callers migrate.

- [ ] Replace `usageApplyPanelZoom` in `panels/dynamic_height.py` with:

```js
window.usageApplyPanelViewport = function usageApplyPanelViewport(scrollable) {
  const root = document.documentElement;
  const body = document.body;
  const y = window.scrollY;
  root.style.zoom = "normal";
  root.style.overflowX = "hidden";
  root.style.overflowY = scrollable ? "auto" : "hidden";
  if (body) body.style.height = "";
  requestAnimationFrame(() => {
    const maxY = Math.max(0, root.scrollHeight - root.clientHeight);
    window.scrollTo(0, Math.min(y, maxY));
  });
  return true;
};
```

Use CSS `scrollbar-gutter: auto` and platform overlay scrollbars; do not reserve a permanent horizontal gutter or add theme-specific scrollbar styling.

- [ ] Update macOS sizing in `menubar/popover.py`:

  - compute maximum as `NSScreen.visibleFrame.height - 24`;
  - keep natural width;
  - cap height through `fit_panel_viewport`;
  - call `usageApplyPanelViewport(scrollable)` after content measurement and before showing.

- [ ] Apply the same work-area cap in `wintray/app.py`; keep logical-pixel/DPI conversion and monitor selection unchanged.

- [ ] Run focused cross-platform tests:

```bash
uv run pytest -q tests/test_panel_scale.py tests/test_panels.py tests/test_menubar.py tests/test_wintray.py tests/test_panel_window_state.py
uv run mypy panels menubar/popover.py wintray/app.py
uv run ruff check panels menubar/popover.py wintray/app.py tests/test_panel_scale.py
```

- [ ] Commit shared overflow behavior:

```bash
git add panels/panel_scale.py panels/dynamic_height.py menubar/popover.py wintray/app.py assets/panels/panel_core.js tests/test_panel_scale.py tests/test_panels.py tests/test_menubar.py tests/test_wintray.py tests/test_panel_window_state.py
git commit -m "fix(panels): scroll tall cards without scaling"
```

---

### Task 8: Full verification and live smoke

- [ ] Run the focused feature suite:

```bash
uv run pytest -q tests/test_ocx_quota.py tests/test_quota_sources.py tests/test_menubar_openrouter.py tests/test_menubar_prefs.py tests/test_menubar_state.py tests/test_web_panel_payload.py tests/test_panel_scale.py tests/test_panels.py tests/test_menubar.py tests/test_wintray.py
```

- [ ] Run repository gates:

```bash
uv run ruff check .
uv run mypy .
uv run pytest -v
```

Expected: all commands exit 0.

- [ ] Scan for forbidden credential/config reads and shell invocation:

```bash
rg -n "opencodex.*(config|token)|x-opencodex-api-key|shell=True|openrouter\.ai" loaders menubar panels wintray
```

Expected: no Usage code reads OCX secrets, calls OpenRouter directly, or uses `shell=True`.

- [ ] Back up Usage preferences and the installed app/status-line hooks before the live build. Record current app version and both repository SHAs.

- [ ] Enable `prefer_ocx` only in the user's backed-up test preferences, launch the forked Usage build, and compare every displayed OCX value against:

```bash
ocx provider quota --refresh --json
```

- [ ] Verify these live cases:

  - Claude and Codex switch among Auto/Native/OCX without merging accounts.
  - Antigravity and Grok show OCX data even when their native clients/logs are absent.
  - Grok `0%` displays as measured use, while plan/subscription stays unknown.
  - OpenRouter headline is account remaining; key limit and reset appear in detail.
  - forced OCX mode uses stale cache or unavailable, never native fallback.
  - a constrained five-card panel preserves width/text scale and scrolls through the footer.
  - switching themes and refreshing preserves/clamps the scroll position correctly.

- [ ] On failure, quit the forked build, restore the preference/hook backups, and reopen `/Applications/Usage.app` 0.30.7. Do not overwrite the installed app until the smoke test passes.

- [ ] Inspect final diff and commit only verification artifacts that contain no account data:

```bash
git diff upstream/main...HEAD
git status --short
```

If verification changed tracked tests/docs, commit them as:

```bash
git add tests docs
git commit -m "test: verify OCX quota integration"
```

Do not create an empty commit.
