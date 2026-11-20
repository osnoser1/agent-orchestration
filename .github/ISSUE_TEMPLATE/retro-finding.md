---
name: Retro finding
about: Report an orchestration issue discovered during a retrospective with ledger evidence
title: "retro: <short summary>"
labels: ["retro-finding"]
---

## Category

Select one:
- [ ] Wrong model/effort choice
- [ ] Quota surprise
- [ ] Stalled child
- [ ] Brief gap
- [ ] Missing harness quirk
- [ ] Unrated model
- [ ] Other

## Observed problem

Describe what happened and how it diverged from expected orchestration behaviour.

## Evidence

Paste relevant lines from `~/.config/agent-orchestration/ledger.jsonl` (keep prompts, code, secrets, and private details out):

```jsonl

```

## Proposed change

Describe the rule, script, template, or catalog adjustment that would prevent this next time.

## Affected file(s)

- e.g. `skills/agent-orchestration/references/selection.md`
- e.g. `skills/agent-orchestration/scripts/orch.py`
- e.g. `skills/agent-orchestration/catalog.yaml`
