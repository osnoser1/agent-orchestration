# Repository orchestration policy

A target repository may add an optional `## Orchestration` section to its `AGENTS.md` (which Claude Code reaches via `CLAUDE.md`, and other harnesses read directly) to override this skill's default workflow for that repository.

## Contract

```markdown
## Orchestration
Repository policy for the agent-orchestration skill; it overrides the skill's defaults here.
- Implement: <skill names or guide>
- Review: <skill names>
- PR: <skill name>
- Land: <command or process>
- High risk: <extra paths/areas that count as high risk>
- Models: <task-type preferences, e.g. UI/UX work → Anthropic>
- Conversations: <when new top-level conversations are preferred or restricted>
```

Every line is optional. When the section is absent, the skill's defaults apply unchanged.

| Field | Effect |
|---|---|
| `Implement` | Skills or guides the implementing worker must follow (named explicitly in implementation briefs). |
| `Review` | Review skills or checklists the reviewer must apply (named explicitly in review briefs). |
| `PR` | Skill or workflow used to open/update pull requests (named explicitly in PR briefs). |
| `Land` | Exact command or process used in Step 5 (Land). |
| `High risk` | Extra paths, modules, or change classes treated as `high` risk in Step 2 (Classify), additive to `references/selection.md`. |
| `Models` | Task-type model or family preferences (e.g. `UI/UX work → Anthropic`). Unmet preferences follow `references/selection.md`: `preferences.ui_fallback` (from `assets.yaml`, Precedence 2) governs switching to another family when the preferred family is `exhausted` (including mid-task quota switches in `references/harnesses.md`); `not offered here` proceeds with the best available model in this harness and one informational note. |
| `Conversations` | Repo guidance for opening new top-level conversations (`references/harnesses.md`). In-place model switches and scheduled resumes stay in the existing conversation and do not count as new conversations. |

## Precedence

1. **User's explicit instruction** — overrides everything for the current request.
2. **User's `assets.yaml` and harness rules** — keep authority over quota limits (`low_threshold`, `exhausted_threshold`, `exhausted_until`), `preferences.ui_fallback` when a preferred family is exhausted, `pay_per_use`, `policy: avoid`, and the ask-once gates in `SKILL.md` Step 7 (`preferences.new_conversations`, emergency spending). Assets and harness rules always win on these.
3. **Repository `## Orchestration`** — overrides skill defaults for process (phase skills/commands), extra high-risk triggers, and task-type model preferences (`Models:`).
4. **Skill defaults** — apply for any omitted line or when `## Orchestration` is absent.

## Applying policy in briefs

Read `## Orchestration` once during Step 1 (Discover). In Step 4 (Brief), name the active phase's skills or commands explicitly in `CONSTRAINTS` and `VERIFICATION` (for example, `"follow the implement-plan skill"`, `"use the create-pr skill"`, or `"land with bun run land <pr>"`) rather than only instructing the child to read `AGENTS.md`.

## Example

```markdown
## Orchestration
Repository policy for the agent-orchestration skill; it overrides the skill's defaults here.
- Implement: implement-plan, tdd
- Review: code-review
- PR: create-pr
- Land: bun run land <pr>
- High risk: db/migrations/**, auth/**, billing/**
- Models: UI/UX work → Anthropic; SQL migrations → OpenAI
- Conversations: cross-repo follow-ups may open a new thread when user permits
```
