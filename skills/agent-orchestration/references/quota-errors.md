# Quota probes, thresholds, and error parsing

## Automatic probe (`scripts/orch.py probe`)
Run `python3 scripts/orch.py probe` during discovery to refresh `used_percent`, `window`, `resets_at`, and per-group `groups` in `assets.yaml`:
- **Probe sources**:
  - **Antigravity**: `agy -p "/usage" --output-format json < /dev/null` (zero-token cost; requires one-time sign-in in the interactive `agy` TUI). Parses `command.data.groups[]` (`used_percent = 100 - remaining_fraction * 100`), ignores `disabled` buckets and expired `reset_time` windows, takes each group's most constraining active bucket, maps display names to catalog ids (unmatched kept as-is and reported once), persists `groups`, and sets provider `status` to the best group (`ok` when any group is usable).
  - **Claude**: `GET https://api.anthropic.com/api/oauth/usage` using local OAuth tokens from `<config_dir>/.credentials.json` (sent only to that endpoint, omitted from stdout, logs, and `assets.yaml`). Missing credentials or HTTP 401 report `unverified` and continue.
  - **Codex**: latest `token_count` event in today + yesterday `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` (`payload.rate_limits.{primary,secondary}`), skipping expired windows.
- **Thresholds**: `used_percent >= 85` marks `status: low` (override via `preferences.low_threshold` or `--low-threshold`); `used_percent >= 98` marks `status: exhausted` with `exhausted_until: <resets_at>` (override via `preferences.exhausted_threshold` or `--exhausted-threshold`; `0 < low_threshold <= exhausted_threshold <= 100`; applied per group when a provider reports `groups`).
- **Precedence and merge with error-derived lockouts**: `probe` merges probed usage into existing provider and `groups` entries rather than overwriting them. When `probe` and an error lockout disagree, an unexpired `exhausted_until` from `orch.py exhaust` (at provider or group level) takes precedence over a `probe` reporting `< exhausted_threshold` unless the probe shows that same scope recovered (usage at `0%` / `reset`, lower `used_percent` than before the lockout, or a later `resets_at` for the same window). Conversely, `used_percent >= exhausted_threshold` in `probe` marks that scope `exhausted` immediately.
- **Auto-recovery**: any `exhausted` provider or group whose `exhausted_until` or `resets_at` has passed clears to `status: ok` once per expired scope, persists to `assets.yaml`, and prints one recovery line. `probe` also surfaces `followups` per `references/selection.md` (`due` only when `--available` includes a usable match, otherwise `pending`).
- **Exit codes (`orch.py`)**: `0` ok (including a `STAGE` plan or provisional selection under `preferences.below_tier`) · `1` validation/usage error · `2` no tier match (no tier-meeting model offered) · `3` blocked by pre-flight threshold or policy gate (`low` ≥`low_threshold`, gated, or `ui_fallback: defer|ask`) · `4` quota hit (`exhausted` / ≥`exhausted_threshold`, or `select` waiting on exhausted tier candidates).

## Reactive error parsing (`scripts/orch.py exhaust`)
Trigger on messages containing `"hit your (weekly|usage) limit"`, `"quota reached"`, `"rate limit"`, `"try again at"`, or `"resets"`:

| Message | Parse | Write |
|---|---|---|
| `You've hit your weekly limit · resets Nov 23, 3am (America/New_York)` | absolute date + IANA zone | `exhausted_until: 2026-11-23T03:00:00-05:00` |
| `Quota limit reached · resets Nov 23, 3am (America/Argentina/Buenos_Aires)` | multi-segment IANA zone | `exhausted_until: 2026-11-23T03:00:00-03:00` |
| `Individual quota reached… Resets in 4h29m1s` / `Resets in 2 hours 30 minutes` | relative duration | `now + duration` |
| `You've hit your usage limit… try again at Nov 26th, 2026 8:01 AM` | absolute (local tz if omitted) | `2026-11-26T08:01:00<tz>` |
| `Weekly limit resets Dec 1` / `resets 2026-12-01` | date-only | end of that day (`23:59:59`) |

Rules:
- Missing year resolves to the next future occurrence; missing time resolves to `23:59:59` of that day; unparseable messages default to `now + 1h` with `unparsed: true`.
- Scope: when a provider uses quota `groups`, pass `--group <name>` or `--model <id>` (mapped to its group via `groups[].models` and `catalog.yaml`) so `exhaust` marks only that group (`status: exhausted`, `exhausted_until`, `status_note`) and recomputes the provider's summary `status` across groups. Without `--group` or `--model`, `exhaust` updates the provider entry directly. Update top-level `updated`, then resume work on the next candidate per `references/harnesses.md`.
