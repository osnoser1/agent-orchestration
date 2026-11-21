---
name: agent-doc-review-loop
description: Review loop for agent-facing documents using writing-for-agents. Use when editing or reviewing skills, references, catalog comments, or AGENTS.md.
metadata:
  internal: "true"
---

# Agent document review loop

Run [`writing-for-agents`](../writing-for-agents/SKILL.md) as an iterative review → fix → re-review loop (capped at 3 passes) over every touched agent-facing file in this repository (`skills/*/SKILL.md`, `skills/*/references/*.md`, `skills/*/catalog.yaml` comments, `.agents/skills/*/SKILL.md`, `AGENTS.md`).

## 1. Load the rubric
Read [`.agents/skills/writing-for-agents/SKILL.md`](../writing-for-agents/SKILL.md). When any changed file is a `SKILL.md`, also read [`SKILL-MECHANICS.md`](../writing-for-agents/SKILL-MECHANICS.md) and confirm repo-local skills under `.agents/skills/` carry `metadata.internal: "true"`.

*Done when*: the writing levers and skill frontmatter/invocation rules are loaded in context.

## 2. Iterate review → fix → re-review (max 3 passes)
Run up to 3 numbered passes across the full set of changed agent-facing files. In each pass, check every file line by line against these six levers:

1. **Context pointers**: front-load the leading word; keep one trigger per distinct branch; cut identity that the target body already carries.
2. **Information hierarchy**: separate ordered steps from on-demand reference; disclose branch-specific material behind pointers; co-locate each concept's definition, rules, and caveats under one heading without sprawl.
3. **Completion criteria**: end every step on a checkable, high-demand `*Done when*` bound.
4. **Leading words and positive targets**: collapse repeated descriptions into compact pretrained tokens; state target behaviors positively (pairing any hard guardrail with what to do).
5. **Pruning**: keep one source of truth per rule across all files; cut environment caches, exposition, and instructions the model obeys by default.
6. **Determinism**: a rule that counts, compares, or matches fixed inputs belongs in a script with tests (`skills/agent-orchestration/scripts/orch.py` for user-facing decisions, `tools/repo.py` for maintainer checks). Propose the script and point the doc at the command and its decision line instead of adding prose.

When a pass finds any defect, fix all findings immediately and start the next numbered pass across all changed files. Stop as soon as a complete pass produces zero findings, or after pass 3 and list any remaining findings under `## Obstacles / not verified`.

*Done when*: one full pass across all touched agent-facing files finds zero defects, or 3 passes complete with any residual findings recorded under `## Obstacles / not verified`.

## 3. Report the passes
Include a per-file review-loop summary in the completion report showing the number of rounds (1–3), what changed in each round, and the final pass outcome.

*Done when*: every touched agent-facing file has its round count, per-round fixes, and final pass status recorded in the report.
