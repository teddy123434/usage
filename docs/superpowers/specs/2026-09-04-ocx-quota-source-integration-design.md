# Usage and OpenCodeX quota-source integration

Date: 2026-09-04

## Summary

Extend Usage so Claude, Codex, Antigravity, and Grok can obtain quota from either their existing native source or OpenCodeX (OCX). Add an OCX-only OpenRouter card that shows account credits and the active API key limit. Keep all OCX and OpenRouter credentials inside OCX. Replace Usage's shrink-to-fit behavior with vertical scrolling when a panel is taller than the visible screen.

This is a cross-repository change:

- `teddy123434/opencodex` owns provider authentication, network quota probes, and the public quota JSON contract.
- `teddy123434/usage` owns source selection, local caching, normalized presentation, menu-bar output, and panel scrolling.

## Goals

1. Show current OCX-backed OpenAI, Anthropic, Antigravity, Grok, and OpenRouter quota in Usage.
2. Preserve native Usage support for Claude Code, Codex, Antigravity, and Grok Build.
3. Let each dual-source provider select `auto`, `native`, or `ocx`.
4. Show OpenRouter account credits as the primary value and the active key's configured limit as detail.
5. Keep OCX credentials, management tokens, and provider API keys out of Usage.
6. Keep provider failures isolated and never present missing data as zero.
7. Make over-height panels vertically scrollable without narrowing or scaling their content.

## Non-goals

- Usage will not call provider quota APIs through its OCX adapter.
- Usage will not read OCX's admin token or configuration secrets.
- Usage will not merge quota from different accounts or sources into one percentage.
- This change will not alter Usage's token, cost, or conversation-history loaders.
- This change will not make arbitrary unknown OCX providers visible without presentation metadata.
- The Claude, Antigravity, and Grok status-line scripts will not start OCX or run quota subprocesses.

## Current state

### Usage

- Claude quota comes from the installed Claude Code status-line hook and its local fallbacks.
- Codex quota comes from local Codex session and SQLite events. OCX-routed sessions currently omit compatible `rate_limits` events.
- Antigravity quota is fetched by Usage through Google's metadata endpoint with the native Antigravity credential.
- Grok quota support already exists in current `main`. It reads current-week billing snapshots from `~/.grok/logs/unified.jsonl`; Grok Build need not be contacted.
- Quota cards are fixed logical cards with saved visibility and ordering.
- Panels currently measure natural height and scale the entire web view down to fit the visible screen. This makes added cards fit but reduces legibility.

### OpenCodeX 2.42.0

- `GET /api/provider-quotas` is the source of truth for the OCX GUI.
- `ocx provider quota [--refresh] --json` is an authenticated CLI wrapper around that endpoint.
- Current reports already cover OpenAI, Anthropic, Google Antigravity, xAI Grok, and OpenRouter when configured.
- OpenRouter currently calls `GET /api/v1/key` with the active inference key and encodes the remaining key cap inside a `customWindows[].label` string.
- OpenRouter does not currently expose account-wide `total_credits` and `total_usage` in a structured report.

## Architecture and trust boundary

```text
Usage menu-bar process
  -> ocx provider quota --json
  -> OCX authenticated local runtime request
  -> GET /api/provider-quotas
  -> OCX provider quota service
       -> OpenAI quota metadata
       -> Anthropic quota metadata
       -> Google Antigravity quota metadata
       -> xAI Grok quota metadata
       -> OpenRouter quota metadata
```

Usage treats the OCX CLI as a credential boundary. It invokes the executable with a fixed argument vector, `shell=False`, a bounded timeout, and a bounded output size. OCX performs local management authentication and provider requests. Usage receives quota values, source labels, timestamps, and non-secret status only.

The OCX runtime remains authoritative. No quota calculation or provider token refresh is duplicated in Usage.

## OCX quota contract

Existing `ProviderQuota` fields remain backward compatible. OpenRouter migrates its key cap from a presentation-only string to existing structured `creditsUsd`, while retaining the legacy `customWindows` row for one compatibility cycle.

Add an optional account-credit object:

```ts
interface ProviderQuotaAccountCreditsUsd {
  totalCredits: number;
  totalUsage: number;
  remaining: number;
}

interface ProviderQuotaCreditsUsd {
  used: number;
  limit: number;
  remaining: number;
  percent: number;
  expiresAt?: number;
  unlimited?: boolean;
  resetAt?: number;
  resetInterval?: "daily" | "weekly" | "monthly" | "none";
}

interface ProviderQuota {
  // Existing window fields remain unchanged.
  accountCreditsUsd?: ProviderQuotaAccountCreditsUsd;
  creditsUsd?: ProviderQuotaCreditsUsd;
  updatedAt: number;
}
```

OpenRouter mapping:

- `GET /api/v1/credits` maps `total_credits`, `total_usage`, and their arithmetic difference to `accountCreditsUsd`.
- `GET /api/v1/key` maps `limit`, `limit_remaining`, `usage`, and `limit_reset` to `creditsUsd`.
- A missing key limit is authoritative `unlimited: true`, not an error and not zero.
- Negative account balance remains visible; OCX does not clamp it to zero.

### OpenRouter management credential

Account-wide credits require a separate OpenRouter management key. It must never become an inference credential.

- Add an optional provider `quotaApiKey` reference.
- Resolve it through the existing environment/keychain secret resolver.
- Keychain identity is separate from the active inference key, for example `keychain:openrouter/quota`.
- Add an interactive OCX command that stores the management key through hidden input; do not accept it as a positional command-line value.
- Config and API responses expose only secret presence and storage kind.
- Existing key-only installations continue to show active-key limit data. Account credits remain unavailable until a management key is configured.

Because this changes authentication and configuration, OCX implementation requires focused security review, config redaction coverage, and `privacy:scan`.

## Usage normalized source model

Usage adds a source-independent internal model. Provider-specific loaders project into this model before menu-bar or panel code sees the result.

Each snapshot contains:

- logical provider ID;
- source ID (`native` or `ocx`);
- source label shown in the card;
- one or more percentage windows with optional reset timestamps;
- optional USD account credits;
- optional USD key budget;
- provider update timestamp;
- freshness state (`fresh`, `stale`, `unavailable`, or `reauth_required`).

Known OCX provider IDs map as follows:

- `openai` -> `codex`
- `anthropic` -> `claude`
- `google-antigravity` -> `antigravity`
- `xai` -> `grok`
- `openrouter` -> `openrouter`

Unknown provider IDs are ignored safely and logged only at debug level without account data.

## Source selection

Claude, Codex, Antigravity, and Grok support `auto`, `native`, and `ocx`. OpenRouter supports `ocx` only.

Selection order:

1. An explicit provider preference wins.
2. In `auto`, a proven OCX route selects OCX.
3. If only one source is available, select it.
4. If both are available and routing cannot be proven, retain the last selected source.
5. On first ambiguous selection, use native for backward compatibility.

A global `prefer_ocx` preference may set the initial preference for all dual-source providers. The user's installation will enable it, while upstream-compatible default behavior remains native-first.

Native and OCX values are never averaged, pooled, or silently substituted when they may describe different accounts. Forced `ocx` mode uses stale OCX cache or becomes unavailable; it does not fall back to native. Forced `native` ignores OCX. `auto` may fall back only when its route/source rules establish that the other source is valid for the selected logical provider.

For pooled OpenAI accounts, Usage shows OCX's aggregate pool percentage as the headline. Current-account quota belongs in card detail and is labeled as such.

## Refresh and cache behavior

Usage maintains one atomic OCX snapshot cache under `~/.usage/`. All OCX subprocesses run off the UI thread.

- Normal refresh reads `ocx provider quota --json`, which may use OCX's internal cache.
- A background refresh runs `ocx provider quota --refresh --json` at most once every five minutes.
- Only one OCX subprocess may run at a time.
- Cached-read subprocess timeout is two seconds.
- Forced-refresh subprocess timeout is 20 seconds because it may wait for several provider metadata endpoints in parallel.
- Accepted stdout is limited to 1 MiB before JSON parsing.
- A failed command or invalid response never overwrites the last-good cache.
- Up to 10 minutes old: fresh presentation.
- More than 10 minutes and no more than 24 hours old: show last value with a stale marker and age.
- More than 24 hours old: show unavailable; keep cached data only for diagnosis.

Provider failures are isolated. A failed OpenRouter probe must not hide valid Claude, Codex, Antigravity, or Grok reports.

## Presentation

Each logical provider remains a single card. Cards show a `Native` or `OCX` source badge.

A card appears when its selected source has a current or displayable stale result. An OCX result can make Antigravity or Grok visible even when the corresponding native CLI is not installed.

Menu-bar headline examples:

```text
Claude 31%   Codex 6%   OpenRouter $74.75   Antigravity 0%   Grok 0%
```

OpenRouter uses account remaining credits as the headline when available. If only the active-key limit is available, it uses key remaining credits and labels that scope in the panel.

OpenRouter detail includes:

- account credits remaining;
- total credits purchased;
- total account usage;
- active key limit;
- active key remaining amount;
- active key used percentage;
- limit reset interval and computed/reset timestamp when available.

A returned Grok `0%` means zero measured use in the reported window. It does not prove a paid subscription. Missing plan data is displayed as unknown.

All quota cards remain hideable and reorderable. Adding OpenRouter must append it to existing saved card order instead of resetting user preferences.

## Panel overflow

Usage will stop shrinking an over-height panel. Its width and text scale remain unchanged.

- When natural height fits, preserve current layout with no scrollbar.
- When natural height exceeds `NSScreen.visibleFrame.height - 24`, cap the window at that height and enable vertical document scrolling.
- Disable horizontal scrolling.
- Use the platform overlay scrollbar.
- Support trackpad, mouse wheel, Page Up, and Page Down.
- Refreshing data preserves scroll position unless content removal makes it invalid.
- Cached theme views preserve their current scroll position while switching themes.
- Footer content scrolls with the cards; no theme-specific sticky layout is introduced.
- Existing window-drag logic continues to treat scrollable descendants as interactive.

The change belongs in the shared panel injection/window layer. Theme HTML files must not receive independent overflow implementations. Windows uses the equivalent visible-work-area cap and WebView scrolling behavior.

## Error semantics

- Missing quota is unknown, never zero.
- HTTP 401/403 maps to `reauth_required` without leaking provider response bodies.
- Timeout, malformed JSON, oversize output, or missing OCX maps to stale/unavailable according to cache age.
- An absent key limit maps to unlimited.
- An absent management key leaves account credits unavailable while preserving active-key quota.
- OCX labels reverse-engineered quota sources as it does today; Usage retains that provenance.
- No provider failure may crash the menu-bar refresh thread or hide unrelated providers.

## Security requirements

- Usage never reads OCX admin token, provider credentials, or OCX config secrets.
- Usage invokes a resolved `ocx` executable using a fixed argument vector and no shell.
- OCX sends credentials only to canonical provider hosts with redirects disabled.
- OpenRouter management key never enters inference routing, JSON responses, logs, diagnostics, backups, or error strings.
- OCX management/config endpoints report only presence and storage kind for `quotaApiKey`.
- Tests never read real Keychain entries, `~/.claude`, `~/.codex`, `~/.grok`, or Antigravity credentials.

## Verification

### OCX focused tests

- Parse OpenRouter account credits and key budget.
- Handle no-limit keys as unlimited.
- Preserve key-only behavior when no management key exists.
- Isolate 401, 403, timeout, malformed, and partial provider failures.
- Reject non-canonical OpenRouter hosts and redirects.
- Verify management-key storage, resolution, config validation, and redaction.
- Verify CLI and management API schemas.
- Run focused Bun tests, `bun run typecheck`, `bun run test:changed`, and `bun run privacy:scan`.
- Before a review-ready PR, run full `bun run test` per OCX policy.

### Usage focused tests

- Parse every supported OCX provider report and ignore unknown reports.
- Enforce timeout, output bound, malformed JSON, and single-flight refresh.
- Verify cache age boundaries and atomic last-good preservation.
- Verify source selection for explicit, route-proven, single-source, and ambiguous cases.
- Verify OpenAI aggregate headline and current-account detail.
- Verify OpenRouter balance and key-limit presentation.
- Verify new card ordering does not reset saved preferences.
- Verify panels scroll only after exceeding available height, retain width/scale, preserve scroll position, and work across themes.
- Run focused pytest files and the repository's normal lint/type/test gates.

### Live smoke test

After automated checks pass:

1. Run the forked OCX against the user's existing OpenAI, Anthropic, Antigravity, xAI, and OpenRouter configuration without changing provider credentials.
2. Confirm `ocx provider quota --refresh --json` returns expected structured reports.
3. Run the forked Usage build and compare every displayed value against OCX output.
4. Confirm Native and OCX source switching for Grok and Antigravity.
5. Confirm a five-card panel scrolls on a constrained display without scaling or clipping.
6. Restore the currently installed release build if the smoke test fails.

## Delivery sequence

1. Implement and verify OCX schema/credential changes on a branch based on `upstream/dev`.
2. Implement and verify the Usage OCX adapter and source model against saved OCX JSON fixtures.
3. Add OpenRouter presentation and card preferences.
4. Replace shrink-to-fit with shared scrolling.
5. Run cross-repository live smoke tests.
6. Keep OCX and Usage commits separate so each fork can track and rebase its upstream independently.
