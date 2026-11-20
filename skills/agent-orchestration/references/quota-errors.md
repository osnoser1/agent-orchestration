# Quota errors → assets update

Detect: "hit your (weekly|usage) limit", "quota reached", "rate limit", "try again at", "resets". Real examples:

| Message | Parse | Write |
|---|---|---|
| `You've hit your weekly limit · resets Nov 23, 3am (America/Bogota)` | absolute date + tz | `status: exhausted until 2026-11-23T03:00-05:00` |
| `Individual quota reached… Resets in 4h29m1s` | relative | now + 4h29m1s |
| `You've hit your usage limit… try again at Nov 26th, 2026 8:01 AM` | absolute | that time (assume user's tz if none) |

No year → next occurrence in the future. No time → end of that day. Unparseable → `exhausted until` now + 1h and note `unparsed: <message>`.
Set `status`, `status_note` (raw message), `updated` (now). Only that provider/instance, not others. Then switch candidate and resend the brief. Optional: `python3 scripts/orch.py exhaust <provider> "<message>"`.
Expired `exhausted until` → treat as `ok` (and say so when you use it).
