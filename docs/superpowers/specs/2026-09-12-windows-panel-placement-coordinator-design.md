# Windows panel placement coordinator redesign

Date: 2026-09-12

## Context

Usage Task 7 replaced panel scaling with fixed-width vertical scrolling. The
Windows implementation currently applies the WebView scroll mode off the
WinForms UI thread, then queues native resize and move work. Five scoped fix
rounds left one load-bearing race: an older request can apply JavaScript,
pause before publishing its native placement, then enqueue stale geometry
after a newer request has committed. The document and native window can then
represent different viewports.

Running WebView2 JavaScript inside the WinForms callback is not safe because
it can deadlock. The approved single-owner design therefore uses one off-UI
placement worker for JavaScript and one UI-thread commit path for native
geometry.

## Required behavior

- Preserve panel width and text scale.
- Cap height to the target monitor work area minus 24 logical pixels.
- Enable vertical document scrolling only when capped.
- Derive JavaScript scroll mode and native height from the same resolved work
  area and `PanelViewport`.
- Use the latest window anchor when native geometry commits.
- Coalesce placement bursts; never run two JavaScript placement transitions
  concurrently.
- Never run WebView or WinForms calls while the coordinator state lock is held.
- Preserve the last committed viewport when a newer JavaScript application is
  unavailable.

## State model

The controller owns these values under one short-lived state lock:

- a monotonic ticket sequence;
- the newest desired request and generation;
- the last JavaScript-applied committed viewport;
- whether the placement worker is running.

`_place_window()` only publishes the newest request and ensures one daemon
worker exists. It does not evaluate JavaScript or resize the window.

The worker repeatedly takes the newest pending request, resolves its target
monitor and viewport, then applies the generated JavaScript off the UI thread.
After JavaScript returns:

1. If a newer desired generation exists, discard the completed result and
   process the newest request. Do not enqueue native geometry.
2. If the latest JavaScript application succeeded, publish the rendered
   viewport as committed and enqueue one native UI callback.
3. If the latest JavaScript application was unavailable, restore the last
   committed generation as desired and enqueue that committed native callback
   when one exists.
4. A stale JavaScript result never publishes native geometry.

The worker checks and clears its running flag under the same lock that accepts
new requests, so a request cannot be lost while the worker exits.

## Native commit

The WinForms UI callback checks, before resize or move, that its rendered
generation is both desired and committed. This check applies even when the
current work area and viewport equal the queued values.

The callback resolves the current anchor and work area again. If only the
anchor changed within the same target viewport, it uses the fresh anchor. If
the work area or viewport changed, it publishes a new placement request and
returns without applying stale geometry. The single worker coalesces repeated
monitor changes.

## Error handling

- Missing window, shutdown, or unresolved monitor ends the current attempt
  without native mutation.
- JavaScript exceptions map to unavailable and retain the prior committed
  viewport.
- Dispatch failure leaves the committed state intact; the next content,
  visibility, or monitor event can enqueue it again.
- No retry loop uses sleeps or an unbounded recursive callback chain.

## Tests

Tests must execute real coordinator code with deterministic events. Required
cases:

- old JavaScript succeeds, a newer request arrives before old publish, and old
  native geometry is never queued;
- a burst runs at most one JavaScript transition at a time and converges to the
  newest viewport;
- a newer unavailable transition restores the prior committed native viewport;
- a queued native callback is rejected whenever its generation is no longer
  both desired and committed, including an equal-viewport return path;
- a delayed same-monitor callback uses the fresh anchor;
- a delayed cross-monitor callback requests one new coordinated placement;
- worker exit and concurrent request publication cannot lose the request;
- no WebView or WinForms call occurs while the state lock is held.

Keep the existing macOS path and shared panel injection unchanged. Run focused
Windows/panel tests, full mypy and Ruff gates, then the repository test suite.

## Complexity

Each request stores constant-size state and replaces one pending request:
`O(1)` time and `O(1)` extra memory. A worker cycle performs one monitor lookup,
one JavaScript application, and at most one native dispatch. A burst may cause
`O(K)` completed cycles for `K` requests, but pending state remains bounded and
intermediate requests are coalesced.
