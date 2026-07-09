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

## Autonomous mode: `/goal` + `/loop`

Set a standing objective, then let the council pursue it without you:

```
you> /goal build a fizzbuzz CLI with unit tests, all passing
you> /loop 10 2.50        # up to 10 iterations, stop if loop spend exceeds $2.50
```

- `/goal <description>` registers the goal. From then on every orchestrator
  turn receives it as trusted context (a mid-conversation `system` message on
  an Opus 4.8 orchestrator; a `<system-reminder>` block on Fable 5, which
  doesn't support mid-conversation system messages) — injected transiently
  per request, after the cached prefix, so it never bloats history.
- `/loop [max_iterations] [budget_usd]` feeds the orchestrator a synthetic
  turn each cycle: review progress, delegate the next round of work to the
  cheaper tiers, check the results. The top tier reasons and decides; the
  worker/routine tiers do the hands-on work and report back.
- The loop stops only when the orchestrator calls the `finish_goal` tool —
  and an **"achieved" claim must survive an independent verifier**: a
  fresh-context worker is spawned to adversarially audit the claim (re-read
  files, re-run tests) and its refutation comes back as more work if the
  claim doesn't hold. `"blocked"` stops the loop and reports what's needed.
- Safety rails: iteration cap (default 10), optional dollar budget checked
  against the live usage tally, and Ctrl+C rolls back the current iteration.

## Council memory

Workers maintain a shared `MEMORY.md` at the workspace root — one bullet per
durable learning (corrections, constraints, confirmed approaches and why they
mattered). Its contents are injected into both the orchestrator's and the
worker's system prompts at setup, so successive sessions on the same workspace
compound instead of starting cold. `/memory` shows the current contents
(clipped to 6 KB when injected).

## Tools & connectors

- **Web research** — the worker carries server-side `web_search` and
  `web_fetch` tools (the `_20260209` variants with dynamic filtering). They
  run on Anthropic's infrastructure and their results arrive inside the same
  response, so research goals need no extra client code.
- **MCP connectors** — drop an `mcp.json` in the workspace (or point
  `COUNCIL_MCP` at one): a JSON list of `{"name", "url",
  "authorization_token"?}` entries. Each server is attached to the worker via
  the MCP connector beta (`mcp-client-2025-11-20`) and its tools become
  available automatically:

  ```json
  [
    {"name": "linear", "url": "https://mcp.linear.app/mcp", "authorization_token": "..."},
    {"name": "docs",   "url": "https://example.com/mcp"}
  ]
  ```

## REPL commands

- `/goal` — set/show the standing goal (`/goal clear` to unset)
- `/loop [n] [budget]` — pursue the goal autonomously (see above)
- `/memory` — show the council memory file
- `/save [name]` — write history + goal to a JSON file in the workspace
  (default `council-session.json`)
- `/resume [name]` — restore a saved session, goal included
- `/usage` — approximate token/cost tally per model
- `/reset` — clear conversation history (keeps the goal)
- `/quit` — exit (prints the session tally)

## Notes

- Fable 5 requires 30-day data retention on your org — a zero-data-retention
  org gets a 400 on every request; the harness prints a hint when that happens.
- Orchestrator effort is `high`; worker defaults to `xhigh` (the orchestrator
  can lower it per delegation); routine tasks run at `low` — except on
  Haiku 4.5, which doesn't support the effort parameter, so it's omitted there.
- Costs (per 1M tokens in/out): Fable 5 $10/$50, Opus 4.8 $5/$25,
  Sonnet 5 $3/$15 (intro $2/$10 through 2026-08-31), Haiku 4.5 $1/$5.
