# Harness notes

**T3 Code**: `orchestrator_capabilities` once per thread (lists providers, models, option keys), then `delegate_task` with target provider/model/options; continue a child with `t3_thread_send`; stop with `task_cancel`. Don't poll children. Use the option key the catalog `quirks` give (e.g. Antigravity: `thought_level`).
**Claude Code (single harness)**: `Agent` tool with `model` (`haiku|sonnet|opus` or full ids), `subagent_type`; continue with `SendMessage`. Only Anthropic family is available, so "different family" review is impossible: use a different model + fresh context and say so.
**Codex**: spawn subagents with model/effort choices the CLI exposes (e.g. `/model`, subagent config); OpenAI family only.
**Gemini / Antigravity**: models listed by the app; Gemini children may end their turn while "waiting".
Rule: only choose what discovery shows. If the harness can't set a model per subagent, use its default and say so.
