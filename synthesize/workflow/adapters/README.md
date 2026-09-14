# Coding Agent Adapters

The workflow itself is agent-neutral: `SKILL.md`, `agents/`, `hooks/`, `scripts/`, `references/`.
This directory holds the small pieces each coding agent needs to load it.

| Agent | File | Why it exists |
|---|---|---|
| Claude Code | `claude/commands/skysynth.md` | the `/skysynth` slash command; reads the root `SKILL.md` and follows it |
| Codex | `codex/skills/skysynth/SKILL.md` | same entry point in Codex's skill layout |
| Codex | `codex/hooks.json` | Codex's hook events, pointing at the shared scripts in `hooks/` |
| Codex | `codex/agents.py` | turns the briefs in `agents/<phase>/*.md` into the TOML files Codex needs; run by `skydiscover init --agent codex` |
| pi | `pi/skydiscover-hooks.ts` | pi has no hooks file, so this extension runs the shared hook scripts |

Cursor needs no adapter: `skydiscover init --agent cursor` symlinks the workflow into
`.cursor/skills/` and writes `.cursor/hooks.json` directly.

The plugin manifests, `../.claude-plugin/plugin.json` and `../.codex-plugin/plugin.json`, are not in
this directory because Claude Code and Codex require them at the plugin root — the directory that
holds the components the plugin ships, namely `workflow/`. Paths inside a manifest are relative to
that root and cannot leave it. The manifests point at the files in this directory.

## Formal worker isolation

Native role wiring does not restrict candidate writes. For scored or proof-only formal work,
launch the complete DSA/ISA process through `spec.proof worker` or provide equivalent external
isolation with a separately controlled evaluator. Keep the original contract digest in that
evaluator's environment. Remote tool servers must obey the same write boundary. The default
proof checker/build/benchmark runner uses sandbox-exec on macOS or bwrap on Linux and fails if
it cannot establish isolation. Explicit `--isolation external` records operator-provided
isolation; it is never an automatic fallback. See [proof evaluation](../../PROOF_EVALUATION.md).

Structured DSA/ISA SubagentStop events return partial progress without triggering final delivery.
Final delivery still requires complete proof verification and, for scored tasks, its measurement.
On adapters without role metadata, keep inner proof tasks separate from final delivery tasks.
