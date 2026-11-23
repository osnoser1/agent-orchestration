# Launch spec and requested-vs-effective verification

Use a `<name>.launch.yaml` spec (`assets/orchestrator.launch.example.yaml`) whenever you open a new top-level conversation for an **orchestrator**, or for an **implementer** or **reviewer** that runs as its own top-level conversation (`surface: conversation` in `references/harnesses.md`). Store real specs and briefs in `~/.config/agent-orchestration/plans/` (outside git). Launch spec and pool caps are inspired by [shinpr/sub-agents-skills](https://github.com/shinpr/sub-agents-skills) and Kraft.

## Spec keys (`<name>.launch.yaml`)

| Key | Required | Values and rules |
|---|---|---|
| `version` | optional | `1` (default `1`) |
| `role` | required | `orchestrator`, `implementer`, or `reviewer` |
| `title` | optional | Conversation title (defaults to the spec filename) |
| `repo` | optional | Repository label passed as basename to `orch.py log --repo` |
| `brief` | required | Path relative to the spec file; must exist and be non-empty |
| `rubric` | required | Map with all six keys (`size`, `uncertainty`, `deps`, `consequences`, `verification`, `coordination`) in `0..2` |
| `ladder` | optional | `auto` (default) or `[provider:model@effort, ...]`; for `orchestrator`, overrides `preferences.orchestrator.ladder` |
| `implementer` | reviewer only | Implementing model (`provider:model` or model id); required when `role: reviewer` |
| `available` | optional | List of `provider:model` pairs from discovery; `--available` on the CLI overrides it |
| `workspace` | optional | `{strategy: worktree\|root\|existing_worktree, base: <ref>, children: shared\|worktree, worktree_path: <dir>}`; `base` required for `worktree`, `worktree_path` for `existing_worktree` |
| `caps` | optional | `{max_escalations: 0..3, max_children_per_pool: N, budget: {<provider>: N}}`; non-negative ints; `max_escalations` overrides `preferences.orchestrator.max_escalations` |

## Launch and header contract

1. **Resolve and write payload**: run `python3 scripts/orch.py launch <spec> [--available <provider:model,...>] [--out <file>]` and follow its `action:` line. Unless `--no-probe` is passed, `orch.py launch` refreshes quota first (`orch.py probe`), classifies the rubric, resolves rung 1 of the orchestrator ladder (or the worker candidate), and writes `<name>.payload.json` next to the spec. Any non-dispatch outcome (hold, stage, gated, ask, or no tier match) exits `3` without writing a payload and points to `orch.py select`.
2. **Dispatch on the active harness**: the JSON payload (`title`, `message`, `modelSelection` with the discovery harness model id and provider effort option key — `claudeAgent* → effort`, `codex* → reasoningEffort` + `serviceTier: default`, `antigravity → thought_level`, or `effort_key` on the provider in `assets.yaml` — `workspaceStrategy`, `clientRequestId`) targets T3 Code `t3_thread_launch`. On other harnesses, pass the printed `requested:` model/effort and the payload's `message` (header + brief) to the harness's **New conversation** command in `references/harnesses.md`.
3. **Phase-boundary header**: `orch.py launch` prepends a plain-text role header (`ORCHESTRATOR`, `IMPLEMENTER`, or `REVIEWER`; worker roles omit ladder, provider-switch, and escalate lines) to the brief:
   ```text
   ORCHESTRATOR — repeat this block, updated, at every phase boundary
   model: codex/gpt-6-2-sol @ medium (rung 1 of 2)
   ladder: codex/gpt-6-2-sol @ medium -> codex/gpt-6-2-sol @ high
   provider switch (phase boundary, ask the user first): claudeAgent/claude-opus-5-5 @ high
   escalate on: outcome rework | 3+ review findings your brief did not flag | a child's STATUS: ESCALATE; at most 2 escalations; never step down mid-phase
   caps: children per pool 2; budget antigravity 10, claudeAgent 4
   workspace: worktree from origin/main; children shared
   rubric: size=2,uncertainty=1,deps=2,consequences=1,verification=1,coordination=2 (hard/medium)
   verify: the launcher compares requested vs effective model/effort after launch (references/orchestrator-launch.md)
   ```
   The launched orchestrator repeats this block, updated with the active rung and remaining escalations/budget, at every phase boundary.
4. **Re-launch after quota renewal**: rerun `python3 scripts/orch.py launch <spec>` on the same spec file when a pool renews to re-probe quota and regenerate the payload.

*Done when*: `orch.py launch` exits `0`, writes `<name>.payload.json` with the role header prepended to the brief, and the new conversation is launched (or handed to `orch.py select` on exit `3`).

## Post-launch and post-delegation verification

Subagents and launched threads can silently drop effort or model settings (e.g., [anthropics/claude-code#43083](https://github.com/anthropics/claude-code/issues/43083): effort is silently ignored in subagents — pass effort explicitly in the tool call and verify; never rely on frontmatter `effort:`). Verify after **every** `t3_thread_launch` and **every** `delegate_task` (worker children matter most):

1. **Read the effective selection**: in T3 Code, call `t3_thread_configure` with only `threadId` to read `{modelSelection: {instanceId, model, options}}`. When `executedModel` from `t3_thread_read` (`recentRuns[].executedModel`) or the `delegate_task` result is non-null, it wins over the configured `model`.
2. **Log requested vs effective**: run `python3 scripts/orch.py log --event select --complexity <c> --risk <r> --provider <p> --model <m> --effort <e> --role <role> --rubric <rubric> --requested <p:m@e> --effective <p:m@e>`.
3. **Fix mismatches in place**: when `orch.py log` prints `warning: effective … differs from requested …`, reconfigure the child in place (`t3_thread_configure` with the requested `modelSelection`, or the harness's **Switch model in place** command in `references/harnesses.md`) and state the correction in the report. When the active harness cannot report the effective model or effort, record that under `## Obstacles / not verified`.

*Done when*: every launched conversation and delegated child has its effective model/effort read, logged against `--requested`, and corrected in place on mismatch (or noted under `## Obstacles / not verified` when the harness cannot report it).
