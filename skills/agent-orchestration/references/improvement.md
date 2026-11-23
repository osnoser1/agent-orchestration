# Improvement loop and retrospectives

Use the local decision ledger to turn orchestration friction observed during retrospectives into upstream fixes on `osnoser1/agent-orchestration`. Route all skill changes upstream and pull them back via `npx skills update` rather than patching installed copies inside consumer repositories.

## Decision ledger (`scripts/orch.py log`)

`orch.py log` appends one JSON line per delegation to `ledger.jsonl` next to `assets.yaml` (`~/.config/agent-orchestration/ledger.jsonl`, or beside `$AGENT_ORCHESTRATION_ASSETS` / `--assets`).

Log twice per delegation:
- **At selection** (Step 3): `python3 scripts/orch.py log --event select --complexity <c> --risk <r> [--ui] [--provisional] --surface subagent|conversation --provider <p> --model <m> --effort <e> [--reviewer <r>] [--role implementer|reviewer|orchestrator] [--rubric <r>] [--requested <p:m@e>] [--effective <p:m@e>] --repo <name>`; populate `--requested` and `--effective` from the post-launch / post-delegation check (`references/orchestrator-launch.md`), and pass `--provisional` only when the work runs below its required tier (`below_tier: proceed` or `ui_fallback: proceed`), never for a tentative plan-named model
- **At completion or failure** (Step 5 / 6): add `--event complete --outcome ok|failed|quota|stall|rework --findings <n>`, and for a failed child `--failure-class quota|infra|context-cap|tool|unknown` (`orch.py failure` prints these flags)

Pass the same `--ref <item>` on every log of an item: `orch.py failure` counts retries from it, and `scripts/orch.py history [--ref <item>] [--last N] [--role implementer|reviewer|orchestrator]` prints one line per item and role (model, reviewer, outcome, findings, provisional, pending follow-ups, non-implementer `role`, and `effective differs` on mismatch) for which-model and which-reviewer questions; `--role` filters to that role. A `select` resets only its own role's attempt, so log reviewers and orchestrators under the item's `--ref` with their `--role`.

Recorded fields: `ts`, `event`, `repo` (basename only — paths are stripped automatically), `surface` (`subagent|conversation`), `complexity`, `risk`, `ui`, `provider`, `model`, `effort`, `reviewer`, `outcome`, `findings`, `role` (`implementer|reviewer|orchestrator`, defaulting to `implementer`; older entries without it count as `implementer`), and optional `provisional`, `ref` (a short item label such as `PR-42`, slugified from `--ref`; never a path), `failure_class` (`quota|infra|context-cap|tool|unknown`; `orch.py failure` counts retries from them, `references/supervision.md`), `rubric` (six-key JSON object from `orch.py classify`), `requested`, `effective` (accepted as `provider:model@effort` or `provider/model @ effort`, stored normalised as `provider:model-id@effort`), and `mismatch` (`true` when normalised `effective` differs from `requested`, also printed as a `warning:` line). Keep prompts, source code, secrets, full paths, and personal data out of the ledger.

## Filing retrospective findings upstream

During a consumer repo's retrospective (`retro` / `retro-dispatch`), inspect recent `ledger.jsonl` lines for that repo and file an upstream issue on `osnoser1/agent-orchestration` using `.github/ISSUE_TEMPLATE/retro-finding.md` for any of these categories:

- **Wrong model/effort choice**: tier floor, effort knee, or ranking picked an underpowered or wasteful worker.
- **Quota surprise**: probe missed a limit, reset parsing failed, or threshold triggered too late.
- **Stalled child**: unhandled harness stall or wakeup failure.
- **Brief gap**: recurring omission in `references/brief-template.md` or repo-policy forwarding.
- **Missing harness quirk**: undocumented CLI flag, option key, or behavior in `references/harnesses.md`.
- **Unrated model**: model missing scores or outdated benchmark data — open a PR updating `skills/agent-orchestration/catalog.yaml` with `python3 skills/agent-orchestration/scripts/orch.py check` passing.

Include the relevant `ledger.jsonl` lines as evidence (still free of secrets, prompts, or private code).

When filing an upstream finding, propose an enforceable fix (an `orch.py check` rule, a test, CI, or a pinned rule in `osnoser1/agent-orchestration`) before a wording change.

## Keeping installs current

| Install scope | Check for updates | Update |
|---|---|---|
| Global (`~/.agents/.skill-lock.json`) | `npx skills check -g` | `npx skills update agent-orchestration -g` |
| Project (`./skills-lock.json`) | `npx skills check -p` | `npx skills update agent-orchestration -p` |

Add `-y` to `npx skills update` for non-interactive runs.

- **Cadence**: the user or retro runner checks and updates at the start of every retrospective (or weekly).
- **Offline staleness reminder**: `python3 scripts/orch.py check` reads local skill lockfiles without making network calls and prints an update suggestion when `agent-orchestration` is older than 7 days.
