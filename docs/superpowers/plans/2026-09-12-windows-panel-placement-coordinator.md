# Windows Panel Placement Coordinator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the racy Windows two-phase viewport placement with one bounded latest-request coordinator while preserving fixed-width scrolling and upstream mixed-DPI behavior.

**Architecture:** `_place_window()` becomes a constant-time publisher into one coalescing placement worker. The worker alone resolves placements and applies WebView JavaScript off the WinForms UI thread; native callbacks commit geometry only when their generation is still both desired and committed.

**Tech Stack:** Python 3.11+, `threading`, pywebview/WebView2, WinForms dispatch, pytest, mypy, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-12-windows-panel-placement-coordinator-design.md`

## Global Constraints

- Preserve panel width and text scale; never restore zoom-to-fit.
- Cap height to the target monitor work area minus 24 logical pixels.
- Derive JavaScript scroll mode and native height from the same work area and `PanelViewport`.
- Preserve upstream physical-pixel saved positions, logical DPI conversion, mixed-monitor selection, and queued UI-thread show ordering.
- Never run WebView or WinForms calls while the placement-state lock is held.
- Never run WebView2 JavaScript from the WinForms UI callback.
- Keep pending coordinator state bounded to one newest request.
- Do not touch live preferences, `~/.opencodex`, Keychain, installed apps, or provider endpoints.
- Use `UV_CACHE_DIR=/private/tmp/usage-uv-cache` for every `uv` command.

---

### Task 1: Add a single-flight placement worker

**Files:**
- Modify: `wintray/app.py:128-150`
- Modify: `wintray/app.py:765-785`
- Modify: `wintray/app.py:1040-1145`
- Test: `tests/test_wintray.py:1170-1410`

**Interfaces:**
- Consumes: `_PanelPlacement`, `_RenderedPanelViewport`, `_PanelViewportApplyResult`, `_resolve_panel_placement()`, `_apply_panel_viewport()`, `_dispatch_window_mutation()`.
- Produces: `_PanelPlacementRequest(generation: int, force_default: bool, show_after_commit: bool)`, `_place_window(force_default: bool = False, show_after_commit: bool = False)`, `_run_panel_placement_worker()`, and `_launch_panel_placement_worker()`.

- [ ] **Step 1: Replace the current concurrency test with a successful-old-JavaScript/late-publish RED test**

Add a deterministic test named
`test_newer_request_supersedes_old_applied_viewport_before_native_publish`.
Use two `threading.Event` objects. Let request A enter real
`_apply_panel_viewport()`, return `APPLIED`, then block before coordinator
publication. Publish request B while A is blocked. Release A and run B. Assert:

```python
assert max_active_javascript_calls == 1
assert applied_modes == [True, False]
assert native_viewports == [newest.viewport]
assert controller._panel_placement_committed == newest_rendered
```

The production mutation that this test catches is enqueueing A after B became
the desired generation. Run against current code and confirm it fails because
A can still enqueue stale native geometry.

- [ ] **Step 2: Add RED tests for coalescing and worker exit**

Add:

```python
def test_placement_worker_coalesces_burst_to_newest_pending_request() -> None: ...

def test_request_published_during_worker_exit_is_not_lost() -> None: ...
```

Use real controller state plus a captured launcher. Publish three requests
while the first JavaScript call is held. Assert only the first and newest
placements reach JavaScript, only the newest reaches native dispatch, and no
second worker runs concurrently. At the worker-exit boundary, publish another
request under controlled events and assert either the existing worker consumes
it or exactly one replacement worker starts.

- [ ] **Step 3: Run the new tests and record expected RED output**

Run:

```bash
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run pytest -q \
  tests/test_wintray.py::test_newer_request_supersedes_old_applied_viewport_before_native_publish \
  tests/test_wintray.py::test_placement_worker_coalesces_burst_to_newest_pending_request \
  tests/test_wintray.py::test_request_published_during_worker_exit_is_not_lost
```

Expected: failures from missing coordinator state or stale native dispatch, not
test setup errors.

- [ ] **Step 4: Add coordinator request and state**

Add:

```python
@dataclass(frozen=True, slots=True)
class _PanelPlacementRequest:
    generation: int
    force_default: bool
    show_after_commit: bool = False
```

Replace `_panel_placement_generation` with lock-protected state:

```python
self._panel_placement_sequence = 0
self._panel_placement_desired_generation = 0
self._panel_placement_pending: _PanelPlacementRequest | None = None
self._panel_placement_committed: _RenderedPanelViewport | None = None
self._panel_placement_worker_running = False
self._panel_placement_show_pending = False
```

Keep `_panel_placement_lock` short-lived. `_place_window()` increments the
sequence, replaces `_panel_placement_pending`, updates desired generation, and
starts one worker only when `_panel_placement_worker_running` changes from
false to true. A true `show_after_commit` sets
`_panel_placement_show_pending`; ordinary replacement requests never clear it.

- [ ] **Step 5: Implement the worker loop**

`_launch_panel_placement_worker()` starts exactly one daemon thread targeting
`_run_panel_placement_worker()`.

The worker loop must:

```python
while not self.stopping.is_set():
    with self._panel_placement_lock:
        request = self._panel_placement_pending
        self._panel_placement_pending = None
        if request is None:
            self._panel_placement_worker_running = False
            return

    placement = self._resolve_panel_placement(
        force_default=request.force_default
    )
    result = (
        _PanelViewportApplyResult.UNAVAILABLE
        if placement is None
        else self._apply_panel_viewport(
            placement.viewport.scrollable,
            request.generation,
        )
    )

    # Publish or discard under the state lock; dispatch outside it.
```

For `APPLIED`, publish and dispatch only when
`request.generation == _panel_placement_desired_generation`. For an obsolete
request, discard its result and continue. For `UNAVAILABLE` on the newest
request, restore desired state to the last committed viewport and enqueue that
committed callback when present. `STALE` never commits native geometry.

When the accepted request carries `show_after_commit=True`, its native callback
must call `_show_panel_on_ui_thread()` after successful resize/move. More
precisely, the worker snapshots `_panel_placement_show_pending` into the native
callback; only a still-current callback may clear it after resize/move/show.
If JavaScript is unavailable and no committed viewport exists, keep the intent
pending and enqueue nothing. `on_loaded()` or the next height report retries.
The explicit hide path clears `_panel_placement_show_pending`.

Before returning on shutdown, clear `_panel_placement_worker_running` under the
lock. No lock scope may include `_resolve_panel_placement()`,
`_apply_panel_viewport()`, `_dispatch_window_mutation()`, `resize()`, or
`move()`.

- [ ] **Step 6: Run Task 1 tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run pytest -q tests/test_wintray.py \
  -k 'placement_worker or supersedes_old_applied or concurrent_viewport or unavailable_non_scroll'
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run mypy wintray/app.py tests/test_wintray.py
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run ruff check wintray/app.py tests/test_wintray.py
```

Expected: all selected tests, mypy, and Ruff pass.

- [ ] **Step 7: Commit**

```bash
git add wintray/app.py tests/test_wintray.py
git commit -m "fix(panels): coordinate Windows viewport placement"
```

---

### Task 2: Make native geometry a generation-checked commit

**Files:**
- Modify: `wintray/app.py:1095-1150`
- Test: `tests/test_wintray.py:1170-1410`

**Interfaces:**
- Consumes: Task 1 `_panel_placement_desired_generation` and `_panel_placement_committed` state.
- Produces: `_place_window_on_ui_thread(force_default: bool = False, rendered_viewport: _RenderedPanelViewport | None = None, show_after_commit: bool = False)` with unconditional generation ownership checks and monitor-change reconciliation through `_place_window()`.

- [ ] **Step 1: Add an equal-viewport stale-callback RED test**

Add
`test_native_commit_rejects_old_generation_even_when_viewport_matches_again`.
Prepare committed generation B, then invoke queued callback A after the current
monitor returns to A's same work area and `PanelViewport`. Assert no `resize()`
or `move()` occurs. This must fail if generation is checked only inside the
viewport-mismatch branch.

- [ ] **Step 2: Add fresh-anchor and cross-monitor RED coverage through the coordinator**

Adapt existing delayed tests so they wait for the placement worker and drain
the captured WinForms callback. Keep literal assertions:

```python
assert mutations == [("resize", 380, 776), ("move", 1400, 12)]
```

For cross-monitor mismatch, assert the stale callback performs no geometry and
publishes one new coordinated request. After the worker finishes, assert the
new JavaScript mode and native cap use the same new work area.

Add `test_show_request_survives_placement_coalescing`. Publish a
`show_after_commit=True` request, then replace its pending viewport with a
normal height request. Drain the accepted native callback and assert literal
order:

```python
assert calls == ["resize", "move", "show"]
```

Add `test_first_show_waits_for_loaded_viewport_before_becoming_visible`.
Make the first JavaScript application return unavailable, then make the loaded
document request succeed. Assert the first drain performs no mutation and the
second drain is exactly:

```python
assert calls == ["resize", "move", "show"]
```

- [ ] **Step 3: Run the tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run pytest -q \
  tests/test_wintray.py::test_native_commit_rejects_old_generation_even_when_viewport_matches_again \
  tests/test_wintray.py::test_delayed_ui_placement_uses_fresh_anchor_with_same_target_viewport \
  tests/test_wintray.py::test_delayed_cross_monitor_drag_reconciles_scroll_mode_without_new_event
```

Expected: current callback path accepts stale equal-viewport geometry or old
tests expose their synchronous assumptions.

- [ ] **Step 4: Enforce ownership before native geometry**

At the start of `_place_window_on_ui_thread()`, after window/shutdown checks and
before resolving geometry, read state under `_panel_placement_lock` and return
unless a supplied rendered viewport satisfies both:

```python
rendered_viewport.generation == self._panel_placement_desired_generation
and rendered_viewport == self._panel_placement_committed
```

Resolve the current placement again. Use its fresh anchor when work area and
viewport still match the committed values. When either differs, call
`_place_window(force_default=force_default, show_after_commit=show_after_commit)`
once and return; do not start a second ad-hoc reconciliation thread. The
placement worker already bounds and coalesces retries.

Keep the `rendered_viewport is None` path only for geometry-only tests or
startup fallback that has no WebView contract. It must not overwrite a newer
committed viewport.

Change `show_panel()` to call `_place_window(show_after_commit=True)` and remove
its separate `_dispatch_window_mutation(self._show_panel_on_ui_thread)` call.
The guarded show method remains unchanged. The hide branch clears the pending
show intent under `_panel_placement_lock` before hiding the native window.

- [ ] **Step 5: Verify native commit behavior**

Run:

```bash
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run pytest -q tests/test_wintray.py \
  -k 'placement or viewport or monitor or dpi or show_panel'
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run mypy wintray/app.py tests/test_wintray.py
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run ruff check wintray/app.py tests/test_wintray.py
```

Expected: all selected tests, mypy, and Ruff pass.

- [ ] **Step 6: Commit**

```bash
git add wintray/app.py tests/test_wintray.py
git commit -m "fix(panels): commit only current Windows geometry"
```

---

### Task 3: Remove legacy race state and verify Task 7

**Files:**
- Modify: `wintray/app.py`
- Modify: `tests/test_wintray.py`
- Test: `tests/test_panel_scale.py`
- Test: `tests/test_panels.py`
- Test: `tests/test_menubar.py`
- Test: `tests/test_panel_window_state.py`

**Interfaces:**
- Consumes: Tasks 1–2 coordinator and native commit paths.
- Produces: one bounded Windows placement state machine with no ad-hoc reconciliation thread or old max-generation publication.

- [ ] **Step 1: Delete superseded state and tests**

Remove `_reconcile_on_mismatch`, max-based `_panel_placement_generation`
publication, ad-hoc reconciliation thread creation, and tests whose only
contract is the deleted implementation order. Keep behavior tests for fixed
width, scrollability, DPI, saved position, fresh anchor, monitor changes,
worker coalescing, and generation ownership.

- [ ] **Step 2: Run focused Task 7 gates**

Run:

```bash
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run pytest -q \
  tests/test_panel_scale.py tests/test_panels.py tests/test_menubar.py \
  tests/test_wintray.py tests/test_panel_window_state.py
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run mypy panels menubar/popover.py wintray/app.py
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run ruff check \
  panels menubar/popover.py wintray/app.py tests/test_panel_scale.py tests/test_wintray.py
```

Expected: all commands exit 0.

- [ ] **Step 3: Run repository gates**

Run:

```bash
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run ruff check .
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run mypy .
UV_CACHE_DIR=/private/tmp/usage-uv-cache uv run pytest -v
git diff --check
git status --short
```

Expected: Ruff and mypy exit 0. Full pytest may retain only the two unchanged
macOS/Python 3.14 `_overlapped` failures already reproduced on pure
`upstream/main`; no Task 7 or quota test may fail.

- [ ] **Step 4: Commit cleanup only when tracked files changed**

```bash
git add wintray/app.py tests/test_wintray.py
git commit -m "test(panels): verify Windows placement coordinator"
```

Do not create an empty commit.
