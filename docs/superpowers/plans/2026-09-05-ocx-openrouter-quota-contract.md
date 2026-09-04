# OCX OpenRouter Quota Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend OpenCodeX's public provider-quota contract so OpenRouter reports structured account credits and active-key limits while keeping the separate management credential secret and non-routable.

**Architecture:** Keep `GET /api/provider-quotas` and `ocx provider quota --json` as the sole quota contract consumed by Usage. Add an OpenRouter-specific parser/fetch module, store the optional management key as a redacted `quotaApiKey` reference, and expose a stdin/hidden-input CLI management surface that never accepts secrets in argv.

**Tech Stack:** Bun 1.4, TypeScript 7 strict ESM, OpenCodeX management API, `@napi-rs/keyring`, Bun test runner.

**Spec:** `../specs/2026-09-04-ocx-quota-source-integration-design.md`

## Global Constraints

- Work in `/Users/teddy/Programming/agent-workspace/opencodex` on `codex/openrouter-quota-contract`, based on `upstream/dev`; do not build this on `main`.
- Do not change the user's installed OCX package or `~/.opencodex/config.json` until the final live-smoke task.
- `quotaApiKey` is never an inference credential and must not enter provider routing, model discovery, request logs, diagnostics, config DTOs, backups, or error text.
- Only send OpenRouter credentials to `https://openrouter.ai/api/v1/key` and `https://openrouter.ai/api/v1/credits`, with redirects disabled and bounded response reads.
- Preserve all existing quota fields. Keep the legacy OpenRouter `customWindows` row for one compatibility cycle.
- Treat 401/403 as terminal credential failures, 408/429/5xx/timeout/malformed bodies as transient, and successful no-limit key responses as authoritative unlimited data.
- Run focused tests after each red/green step. Run repository-wide policy gates only after focused tests pass.

---

## File Structure

### Create

- `src/providers/openrouter-quota.ts` — pure OpenRouter response parsers plus the two canonical-host fetches and merge logic.
- `tests/openrouter-quota.test.ts` — parser, endpoint, partial-failure, canonical-host, timeout, and malformed-response coverage.
- `tests/provider-quota-key.test.ts` — quota-management key storage, redaction, API, and CLI tests.

### Modify

- `src/types/provider.ts` — add the redacted optional `quotaApiKey` provider field.
- `src/providers/quota-types.ts` — add structured account credits and key-reset metadata.
- `src/providers/key-store.ts` — add quota-key resolve/store/remove/status helpers using the existing keychain service and test seam.
- `src/providers/quota.ts` — delegate OpenRouter probing to `openrouter-quota.ts` and preserve the existing provider-level cache contract.
- `src/server/auth-cors.ts` — classify `quotaApiKey` as redacted and expose only `hasQuotaApiKey` plus `quotaApiKeyStore` in safe DTOs.
- `src/server/management/provider-routes.ts` — add authenticated quota-key status/set/remove routes.
- `src/cli/runtime-api.ts` — add a hidden TTY secret reader while retaining bounded piped-stdin support.
- `src/cli/provider-runtime.ts` — add `ocx provider quota-key openrouter <status|set|remove> [--stdin] [--json]` and render structured OpenRouter quota fields.
- `src/cli/capabilities.ts` — declare the new command/routes and secrecy contract.
- `src/cli/registry.ts` — include `quota-key` in provider help.
- `src/cli/account-api.ts` — extend wire DTOs for `creditsUsd` and `accountCreditsUsd`.
- `src/cli/account-extended.ts` — render account balance, key cap, and reset interval in human output.
- `tests/provider-quota.test.ts` — replace legacy-only OpenRouter assertions with aggregator integration assertions.
- `tests/provider-key-store.test.ts` — cover quota-key keychain isolation and cleanup.
- `tests/management-provider-validation.test.ts` — prove config/editor redaction and derived presence metadata.
- `tests/cli-provider.test.ts` — cover command parsing, stdin/TTY behavior, and secret-free output.
- `tests/cli-capabilities.test.ts` and `tests/cli-registry.test.ts` — keep declared CLI/API surfaces complete.

---

### Task 1: Establish the branch, baseline, and additive wire model

- [ ] Fetch the upstream branch and create the implementation branch without touching user configuration:

```bash
cd /Users/teddy/Programming/agent-workspace/opencodex
git fetch upstream dev
git switch -c codex/openrouter-quota-contract upstream/dev
git status --short --branch
```

Expected: a clean branch whose upstream point is `upstream/dev`.

- [ ] Run the existing focused quota and redaction tests before changing code:

```bash
bun test tests/provider-quota.test.ts tests/provider-key-store.test.ts tests/management-provider-validation.test.ts tests/cli-provider.test.ts
```

Expected: all selected tests pass. If the upstream baseline is red, record the exact failing test and stop before attributing it to this feature.

- [ ] Add a contract serialization test to `tests/openrouter-quota.test.ts` for the target shape:

```ts
const quota: ProviderQuota = {
  accountCreditsUsd: {
    totalCredits: 100,
    totalUsage: 25.25,
    remaining: 74.75,
  },
  creditsUsd: {
    used: 2,
    limit: 10,
    remaining: 8,
    percent: 20,
    unlimited: false,
    resetInterval: "monthly",
  },
  customWindows: [{ label: "API credits ($8.00 of $10.00 remaining)", percent: 20 }],
  updatedAt: 1_788_538_400_000,
};
expect(JSON.parse(JSON.stringify(quota))).toEqual(quota);
```

- [ ] Run the new test and confirm TypeScript compilation fails because the account-credit and reset fields do not exist:

```bash
bun test tests/openrouter-quota.test.ts
```

- [ ] Add these exact interfaces to `src/providers/quota-types.ts`:

```ts
export interface ProviderQuotaAccountCreditsUsd {
  totalCredits: number;
  totalUsage: number;
  remaining: number;
}

export type ProviderQuotaResetInterval = "daily" | "weekly" | "monthly" | "none";

export interface ProviderQuotaCreditsUsd {
  used: number;
  limit: number;
  remaining: number;
  percent: number;
  expiresAt?: number;
  unlimited?: boolean;
  resetAt?: number;
  resetInterval?: ProviderQuotaResetInterval;
}

export type ProviderQuotaAvailability = "ok" | "unavailable" | "reauth_required";

export interface ProviderQuotaStatus {
  provider: string;
  status: ProviderQuotaAvailability;
  observedAt: number;
}
```

Add `accountCreditsUsd?: ProviderQuotaAccountCreditsUsd` to `ProviderQuota` and `statuses: ProviderQuotaStatus[]` to `ProviderQuotaResponse` without renaming or removing existing fields. `reports` remains the data array; `statuses` is a non-secret per-provider availability array so 401/403 remains distinguishable even when no report exists.

- [ ] Mirror those fields in `ProviderQuotaDto` in `src/cli/account-api.ts`; keep every property optional at the wire boundary so older OCX runtimes remain parseable.

- [ ] Run typecheck and the focused test:

```bash
bun run typecheck
bun test tests/openrouter-quota.test.ts
```

Expected: typecheck and the contract serialization test pass.

- [ ] Commit the contract:

```bash
git add src/providers/quota-types.ts src/cli/account-api.ts tests/openrouter-quota.test.ts
git commit -m "feat(quota): define structured OpenRouter credits"
```

---

### Task 2: Implement pure OpenRouter parsing and endpoint isolation

- [ ] In `tests/openrouter-quota.test.ts`, add table-driven failing tests for:

  - `/credits`: numeric and numeric-string `total_credits`/`total_usage`, including negative `remaining`.
  - `/key`: `limit_remaining` precedence over lifetime `usage`.
  - `/key`: missing/`null` limit maps to `unlimited: true` without inventing a percentage bar.
  - `limit_reset`: only `daily`, `weekly`, `monthly`, or absent/`none` are emitted.
  - malformed, oversized, 401, 403, 408, 429, and 5xx responses.
  - one endpoint succeeding while the other is transient or terminal.
  - a non-canonical configured OpenRouter base URL causes zero fetches.

- [ ] Run the file and verify failures are behavioral rather than fixture syntax errors:

```bash
bun test tests/openrouter-quota.test.ts
```

- [ ] Implement `src/providers/openrouter-quota.ts` with this public boundary:

```ts
export interface OpenRouterQuotaCredentials {
  inferenceApiKey?: string;
  managementApiKey?: string;
}

export function parseOpenRouterCredits(value: unknown): ProviderQuotaAccountCreditsUsd | null;
export function parseOpenRouterKey(value: unknown): ProviderQuotaCreditsUsd | null;

export async function fetchOpenRouterQuotaData(
  provider: string,
  config: OcxProviderConfig,
): Promise<OpenRouterQuotaProbeResult>;

export type OpenRouterQuotaProbeResult =
  | { kind: "data"; source: "openrouter:credits+key-info" | "openrouter:credits" | "openrouter:key-info"; quota: ProviderQuota }
  | { kind: "transient" }
  | { kind: "terminal" }
  | { kind: "reauth_required" };
```

Implementation rules:

  - Resolve `apiKey` and `quotaApiKey` independently through `resolveProviderApiKey`.
  - Run eligible `/key` and `/credits` requests concurrently with `Promise.allSettled`.
  - Reuse `readQuotaJson`, `REQUEST_TIMEOUT_MS`, `toFiniteNumber`, `normalizePercent`, and `normalizeResetAt` from `quota-wire.ts`.
  - Return a quota payload when either endpoint yields valid data; one provider sub-endpoint must not suppress the other.
  - Use the local discriminated result so `quota.ts` can map total terminal failure to its existing sentinel and 401/403 to an explicit status without importing aggregator internals into the OpenRouter module.
  - Preserve the compatibility `customWindows` entry only for finite key limits; do not emit it for unlimited keys.
  - Follow OCX's existing unlimited-credit representation: `{ used: max(0, usage), limit: 0, remaining: 0, percent: 0, unlimited: true }`; consumers must ignore the numeric placeholders whenever `unlimited` is true.
  - Do not clamp `accountCreditsUsd.remaining`.

- [ ] Replace the inline `fetchOpenRouterQuota` in `src/providers/quota.ts` with a thin adapter around `fetchOpenRouterQuotaData`; keep `report()`, provider dispatch order, terminal/transient semantics, and provider-level cache TTL in `quota.ts`. Add a distinct `REAUTH_REQUIRED_QUOTA_FAILURE` sentinel and build `ProviderQuotaResponse.statuses` without provider response bodies or messages.

- [ ] Route every quota probe's HTTP 401/403 through the new reauth sentinel. Keep other terminal 4xx behavior unchanged. Add aggregator tests proving one reauth status does not suppress sibling reports and that cached data may be shown with reauth provenance but is never refreshed as `ok`.

- [ ] Run focused tests:

```bash
bun test tests/openrouter-quota.test.ts tests/provider-quota.test.ts
bun run typecheck
```

Expected: all OpenRouter parser and aggregator tests pass, including legacy `customWindows` compatibility.

- [ ] Commit the parser/fetch split:

```bash
git add src/providers/openrouter-quota.ts src/providers/quota.ts src/providers/quota-wire.ts tests/openrouter-quota.test.ts tests/provider-quota.test.ts
git commit -m "feat(quota): fetch OpenRouter account and key credits"
```

---

### Task 3: Add isolated quota-management key storage

- [ ] Add failing tests to `tests/provider-key-store.test.ts` and `tests/provider-quota-key.test.ts` proving:

  - `quotaApiKey` resolves from `${OPENROUTER_MANAGEMENT_KEY}` and `keychain:openrouter/quota`.
  - storing a quota key writes account `openrouter/quota`, verifies read-back, then persists only the reference.
  - the inference key and quota key use different keychain accounts.
  - failed keychain write/read-back leaves config unchanged and removes partial keychain state.
  - removal clears only `quotaApiKey` and its keychain entry.
  - status returns `{ hasQuotaApiKey, quotaApiKeyStore }` and never the secret/reference value.

- [ ] Run the tests and confirm the new API is missing:

```bash
bun test tests/provider-key-store.test.ts tests/provider-quota-key.test.ts
```

- [ ] Add `quotaApiKey?: string` to `OcxProviderConfig` in `src/types/provider.ts`, documented as quota-only and forbidden from inference routing.

- [ ] Extend `src/providers/key-store.ts` with these operations, reusing the existing entry factory and verified-write logic:

```ts
export function resolveProviderQuotaApiKey(provider: OcxProviderConfig): string | undefined;
export function providerQuotaKeyStoreKind(provider: OcxProviderConfig): ProviderKeyStoreKind;
export function storeProviderQuotaKeyInKeychain(
  config: OcxConfig,
  name: string,
  secret: string,
): { ok: true; store: "keychain" } | { ok: false; error: string; status: number };
export function removeProviderQuotaKey(
  config: OcxConfig,
  name: string,
): { ok: true; removed: boolean } | { ok: false; error: string; status: number };
```

`storeProviderQuotaKeyInKeychain` must reject non-OpenRouter providers for this release, trim/validate non-empty input, save only after read-back succeeds, clear resolver caches, and redact thrown errors before returning them.

- [ ] In `src/server/auth-cors.ts`:

  - add `quotaApiKey: "redacted"` to `PROVIDER_CONFIG_FIELD_POLICY`;
  - add derived `hasQuotaApiKey` and `quotaApiKeyStore` fields only to `safeConfigDTO`;
  - ensure `providerEditorConfigDTO` and parse errors never expose or accept the secret field.

- [ ] In `src/server/management/provider-routes.ts`, add authenticated routes:

```text
GET    /api/providers/quota-key?name=openrouter
POST   /api/providers/quota-key?name=openrouter   body: { "key": "..." }
DELETE /api/providers/quota-key?name=openrouter
```

Return status/presence metadata only. Clear the provider quota cache after POST/DELETE so the next refresh uses the new credential state.

- [ ] Run focused security tests:

```bash
bun test tests/provider-key-store.test.ts tests/provider-quota-key.test.ts tests/management-provider-validation.test.ts
bun run privacy:scan
bun run typecheck
```

- [ ] Commit storage and redaction:

```bash
git add src/types/provider.ts src/providers/key-store.ts src/server/auth-cors.ts src/server/management/provider-routes.ts tests/provider-key-store.test.ts tests/provider-quota-key.test.ts tests/management-provider-validation.test.ts
git commit -m "feat(openrouter): store quota key outside inference auth"
```

---

### Task 4: Add secret-safe CLI management and human rendering

- [ ] Add failing CLI tests for these exact invocations:

```text
ocx provider quota-key openrouter status --json
ocx provider quota-key openrouter set --stdin --json
ocx provider quota-key openrouter set --json
ocx provider quota-key openrouter remove --json
ocx provider quota --refresh --json
```

Assertions:

  - `set --stdin` reads one bounded line and never includes it in argv-derived errors.
  - interactive `set` disables terminal echo, restores terminal state on success/error/interrupt, and never prints the key.
  - `set` rejects positional key material with a redacted usage error.
  - JSON and human output contain only presence/store metadata.
  - human quota output prints account remaining/total-used and active-key remaining/limit/reset cadence without parsing `customWindows[].label`.

- [ ] Implement a platform-gated `readHiddenSecretLine` in `src/cli/runtime-api.ts`:

```ts
export async function readHiddenSecretLine(
  deps: RuntimeApiDeps,
  label: string,
): Promise<string>;
```

Use `readSecretLine` for non-TTY/`--stdin`. On a real TTY, temporarily disable echo through the terminal API, print only the prompt/newline, cap input length, enforce the existing timeout, and restore echo in `finally`.

- [ ] Add `quotaKey` dispatch in `src/cli/provider-runtime.ts` with the fixed grammar above. Do not add `--key`, `--token`, or positional-secret forms.

- [ ] Extend `providerQuotaLine`/`quotaParts` in `src/cli/account-extended.ts` to render the new structured fields. Keep output stable for providers that do not report credits.

- [ ] Update `src/cli/capabilities.ts` and `src/cli/registry.ts`; add the three quota-key routes to the capabilities route inventory.

- [ ] Run focused tests:

```bash
bun test tests/cli-provider.test.ts tests/cli-capabilities.test.ts tests/cli-registry.test.ts tests/cli-json-contract.test.ts tests/provider-quota-key.test.ts
bun run typecheck
```

- [ ] Commit CLI/API parity:

```bash
git add src/cli/runtime-api.ts src/cli/provider-runtime.ts src/cli/capabilities.ts src/cli/registry.ts src/cli/account-api.ts src/cli/account-extended.ts tests/cli-provider.test.ts tests/cli-capabilities.test.ts tests/cli-registry.test.ts tests/cli-json-contract.test.ts tests/provider-quota-key.test.ts
git commit -m "feat(cli): manage OpenRouter quota credentials safely"
```

---

### Task 5: Verify OCX policy gates and the public contract

- [ ] Run the focused suite first:

```bash
bun test tests/openrouter-quota.test.ts tests/provider-quota.test.ts tests/provider-key-store.test.ts tests/provider-quota-key.test.ts tests/management-provider-validation.test.ts tests/cli-provider.test.ts tests/cli-capabilities.test.ts tests/cli-registry.test.ts tests/cli-json-contract.test.ts
```

- [ ] Run repository policy gates in this order:

```bash
bun run typecheck
bun run test:changed
bun run privacy:scan
bun run test
```

Expected: every command exits 0; privacy scan emits no credential finding.

- [ ] Inspect the diff for secret-field leakage and accidental unrelated changes:

```bash
git diff upstream/dev...HEAD -- src/types/provider.ts src/providers src/server src/cli tests
git status --short
```

- [ ] Run a fixture-only CLI contract check against a test server and save a redacted sample for the Usage plan. The sample must contain all five known providers and these OpenRouter fields:

```json
{
  "accountCreditsUsd": { "totalCredits": 100, "totalUsage": 25.25, "remaining": 74.75 },
  "creditsUsd": { "used": 2, "limit": 10, "remaining": 8, "percent": 20, "unlimited": false, "resetInterval": "monthly" }
}
```

- [ ] If verification required only test/doc fixes, commit them separately:

```bash
git add tests src
git commit -m "test(quota): verify OpenRouter public contract"
```

Do not create an empty commit.

---

### Task 6: Live smoke after the Usage consumer is ready

- [ ] Back up the current OCX config and record the installed version before using the fork:

```bash
cp /Users/teddy/.opencodex/config.json /Users/teddy/.opencodex/config.json.pre-usage-quota-smoke
ocx --version
```

- [ ] Start the fork from `/Users/teddy/Programming/agent-workspace/opencodex` using the existing config. Do not rewrite provider credentials.

- [ ] Configure the OpenRouter management key through hidden input or piped stdin, then verify the stored config contains only `keychain:openrouter/quota` or an environment reference.

- [ ] Run:

```bash
ocx provider quota --refresh --json
```

Expected: OpenAI, Anthropic, Google Antigravity, xAI, and OpenRouter reports remain independently present; OpenRouter has structured account/key credit fields; stdout/stderr contain no secret.

- [ ] On any live failure, stop the forked process, restore the backed-up config atomically, and restart the installed 2.42.0 service before diagnosing further.

- [ ] Record the verified OCX commit SHA in the Usage implementation notes; do not commit the backup or any live output containing account identifiers.
