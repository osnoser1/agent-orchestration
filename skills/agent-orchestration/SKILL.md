---
name: agent-orchestration
description: Turns the current conversation into a quota-aware orchestrator that delegates work to subagents and models chosen by task complexity, risk and remaining provider quota, with test-first and independent-review quality gates. Use when a task is big enough to delegate, when the user mentions subagents, delegates, model choice, quota, rate limits or "orchestrate", or when a child agent hits a usage limit.
license: MIT
compatibility: Any harness with subagents (T3 Code delegate_task, Claude Code Agent tool, Codex subagents, Gemini/Antigravity). Optional helper scripts need only Python 3 or Node, no packages.
metadata:
  version: "0.1.0"
---

# Agent orchestration

You are the **orchestrator**: pick the right worker, write a precise brief, enforce gates, keep your own spend low. Do cheap work yourself (read a diff, wait on a background command, run one key test); delegate expensive work (implementation, deep research, review).

Two data files drive choices. Never hard-code model names from memory.
- `catalog.yaml` (next to this file): shared model facts: scores, cost class, strengths, quirks.
- `~/.config/agent-orchestration/assets.yaml` (or `$AGENT_ORCHESTRATION_ASSETS`): this user's providers, quota status, policy. Format: `assets/assets.example.yaml`. If missing or stale, **do not block**: use defaults (all discovered providers `ok`, policy `preserve`), say so in one line, offer to create it.

## 1. Discover (once per conversation)
List what this harness can actually run: T3 Code → `orchestrator_capabilities`; Claude Code → the `model` values of the `Agent` tool; Codex/Gemini → their subagent/model list. Intersect with the catalog by id. A model not in the catalog, or lacking scores at the efforts you need, is `unrated`: run the one-time research in `references/research-unrated.md`, record it in the catalog (or a local `catalog.local.yaml` next to the assets file), never repeat it per task.

## 2. Classify
Complexity (`trivial|standard|hard|frontier`) × risk (`low|medium|high`) → required tier. Table and risk triggers: `references/selection.md`. High risk = destructive, security/auth, prod data, publishing, irreversible. Also note tool needs (browser, device, network).

## 3. Choose model + effort (table lookup)
1. Candidates = discovered ∩ not `exhausted` (assets) ∩ cost class not `emergency` ∩ not pay-per-use API ∩ meets the tier at some effort.
2. Prefer quota that expires soonest: policy `spend-first` / `ending` first, `preserve` last.
3. Lowest effort that meets the tier (knee of the curve), then cheaper cost class.
4. Reviewer: a different **family** than the implementer. UI/UX review → Anthropic models preferred (`strengths`).
5. Apply the model's `quirks` when briefing and driving it.
Emergency models, `max`/`ultra` efforts, and any pay-per-use API need explicit user approval, unless everything else is exhausted and the work is blocked (then ask once).

## 4. Brief
Every delegation carries: **goal**, **known facts** (paths, findings, decisions already made, don't re-derive), **constraints** (what not to touch, repo rules to read first, e.g. AGENTS.md/CLAUDE.md), **verification** (exact commands + evidence expected), **report format** (short: what changed, evidence, open risks, commit/PR). Template: `references/brief-template.md`.

## 5. Quality gates
1. **Test first with real evidence**: failing test/repro before the fix, passing after; real output, not claims.
2. **Independent review** by a different family, given the diff, not your conclusions. Send findings back to the **implementing child** (it has the context): T3 Code `t3_thread_send`; otherwise the harness's continue/message mechanism.
3. **Final cheap check by you**: read the diff, run one key test. Do not redo the child's work.
4. Land through the repo's own process (AGENTS.md/CLAUDE.md: land queue, PR + CI, etc.). Don't invent one.

## 6. Quota and failure handling
- Child hits a quota/usage error → parse the reset time (`references/quota-errors.md`), write `status: exhausted until <ISO time>` to assets (`scripts/orch.py exhaust` is optional), pick the next best candidate, **resend the same brief** plus any partial results. Tell the user in one line.
- Child ends its turn "waiting" or stalls → resend, telling it to block in one command. See `quirks` in the catalog.
- User says "Claude reset", "I cancelled X", "Antigravity is almost out" → update assets accordingly (`references/assets-updates.md`).

## 7. Decide vs ask
Decide non-critical things yourself and report them. Ask the user only for critical ones: destructive actions, publishing, spending emergency models or pay-per-use APIs, or ambiguity that changes the outcome. Don't ask trivia.

## References (load on demand)
`references/selection.md` · `references/brief-template.md` · `references/quota-errors.md` · `references/assets-updates.md` · `references/research-unrated.md` · `references/harnesses.md`
