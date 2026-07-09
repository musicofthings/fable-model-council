# Model Council

A three-tier Claude harness, run as an interactive CLI:

| Role | Model choices (default first) | Job |
|---|---|---|
| Orchestrator | `claude-fable-5` or `claude-opus-4-8` | Talks to you, plans, routes work, verifies and synthesizes results |
| Worker | `claude-opus-4-8` or `claude-sonnet-5` | Autonomous agent with shell + file access (confined to the workspace) |
| Routine | `claude-sonnet-5` or `claude-haiku-4-5` | Fast/cheap tool-free text tasks: drafts, summaries, rewrites, boilerplate |

## Run

```bash
python council.py
```

At startup the CLI asks which model to use for each role — answer `1`/`2`, type
a model name, or press Enter for the default — and which folder to use as the
council workspace (any path on the machine; Enter keeps `~/council-workspace`,
or whatever `COUNCIL_WORKSPACE` is set to). Prompts, tool descriptions,
pricing, and the refusal-fallback wiring all adapt to the selection.

Requires `anthropic>=0.116` (installed) and either `ANTHROPIC_API_KEY` in the
environment or an `ant auth login` profile.

## How it works

- Fable 5 runs the top-level agentic loop with three tools: `delegate_task`
  (spawns an Opus 4.8 worker with its own bash + text-editor loop),
  `quick_task` (a single Sonnet 5 call at `effort: low`), and `run_workflow`
  (a multi-stage pipeline: tasks within a stage run in parallel, stages run in
  sequence, and later stages automatically receive all earlier outputs).
- Multiple tool calls issued in one orchestrator turn run in parallel
  (`COUNCIL_MAX_PARALLEL`, default 4). Parallel subagents print tagged,
  line-buffered progress (`[w1]`, `[s2.t1]`) instead of interleaved streams.
- The worker's shell commands run under Git Bash with a 180 s timeout; file
  edits are path-confined to the workspace chosen at startup
  (`~/council-workspace` by default; `COUNCIL_WORKSPACE` sets the default).
- A Fable 5 orchestrator ships with server-side refusal fallbacks to Opus 4.8
  (`server-side-fallback-2026-06-01`), so a safety-classifier false positive is
  transparently re-served instead of failing the turn. (An Opus 4.8
  orchestrator doesn't request the fallback beta — it isn't needed there.)
- Prompt caching: the orchestrator's system prompt carries a fixed cache
  breakpoint, and a rotating window of up to 3 breakpoints follows the growing
  conversation (staying under the 4-breakpoint API limit). The worker's inner
  loop caches its own growing history the same way. `/usage` itemizes
  uncached input, cache writes (1.25x), and cache reads (0.1x) per model.

## REPL commands

- `/usage` — approximate token/cost tally per model
- `/reset` — clear conversation history
- `/quit` — exit (prints the session tally)

## Notes

- Fable 5 requires 30-day data retention on your org — a zero-data-retention
  org gets a 400 on every request; the harness prints a hint when that happens.
- Orchestrator effort is `high`; worker defaults to `xhigh` (the orchestrator
  can lower it per delegation); routine tasks run at `low` — except on
  Haiku 4.5, which doesn't support the effort parameter, so it's omitted there.
- Costs (per 1M tokens in/out): Fable 5 $10/$50, Opus 4.8 $5/$25,
  Sonnet 5 $3/$15 (intro $2/$10 through 2026-08-31), Haiku 4.5 $1/$5.
