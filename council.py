#!/usr/bin/env python3
"""Model Council — a three-tier Claude harness.

    orchestrator (Fable 5 or Opus 4.8)   : plans, routes, synthesizes, talks to you
    worker       (Opus 4.8 or Sonnet 5)  : autonomous agent with shell + file access
    routine      (Sonnet 5 or Haiku 4.5) : fast/cheap text tasks (drafts, summaries)

At startup the CLI asks which model to use for each role and which folder to
use as the worker's workspace (Enter = default for each).

The orchestrator can fan out: tool calls issued in one turn run in parallel,
and the run_workflow tool executes multi-stage pipelines (parallel tasks per
stage, stage outputs fed to later stages) in a single call.

Prompt caching: the orchestrator's system prompt and a rotating window of
conversation breakpoints are cached, as is the worker's growing history inside
its own agentic loop. /usage itemizes cache reads/writes.

Usage:
    python council.py

Auth:
    ANTHROPIC_API_KEY env var, or an `ant auth login` profile.

Config (env vars):
    COUNCIL_WORKSPACE      default workspace offered at startup (default ~/council-workspace)
    COUNCIL_MAX_PARALLEL   max concurrent subagents (default 4)

Council memory:
    Workers maintain MEMORY.md at the workspace root (durable learnings, one
    bullet each); its contents are injected into both system prompts at setup.

Tools & connectors:
    The worker has server-side web_search / web_fetch for research. Drop an
    mcp.json in the workspace — a JSON list of {"name", "url",
    "authorization_token"?} — to connect MCP servers (Linear, GitHub, ...);
    their tools become available to the worker automatically.

Commands inside the REPL:
    /goal    set/show the standing goal (/goal clear to unset)
    /loop    pursue the goal autonomously: /loop [max_iterations] [budget_usd]
             each iteration the orchestrator reviews progress, delegates work to
             the cheaper tiers, and checks results; an "achieved" claim must
             survive an independent fresh-context verifier before the loop stops
    /memory  show the council memory file (MEMORY.md)
    /reload  re-read MEMORY.md and mcp.json into the system prompts mid-session
             (costs one prompt-cache rebuild on the next request)
    /save    write history + goal to a JSON file in the workspace
             (/save [name], default council-session.json)
    /resume  restore a saved session (/resume [name])
    /usage   token/cost tally so far
    /reset   clear conversation history (keeps the goal)
    /quit    exit
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic

# Windows consoles/pipes default to cp1252, which can't encode the glyphs and
# model output this harness prints — force UTF-8 so a stray character can't
# crash a turn.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
try:
    # utf-8-sig eats the BOM PowerShell can prepend when piping input,
    # which would otherwise make the first "/command" unrecognizable.
    sys.stdin.reconfigure(encoding="utf-8-sig", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- config

# model id -> (display name, $/1M input, $/1M output)
MODEL_CATALOG = {
    "claude-fable-5": ("Fable 5", 10.00, 50.00),
    "claude-opus-4-8": ("Opus 4.8", 5.00, 25.00),
    "claude-sonnet-5": ("Sonnet 5", 3.00, 15.00),
    "claude-haiku-4-5": ("Haiku 4.5", 1.00, 5.00),
}

# role -> candidate model ids (first entry is the default)
ROLE_CHOICES = {
    "orchestrator": ["claude-fable-5", "claude-opus-4-8"],
    "worker": ["claude-opus-4-8", "claude-sonnet-5"],
    "routine": ["claude-sonnet-5", "claude-haiku-4-5"],
}

# Defaults — the startup picker in main() overrides these via configure_council().
ORCHESTRATOR_MODEL = "claude-fable-5"
WORKER_MODEL = "claude-opus-4-8"
ROUTINE_MODEL = "claude-sonnet-5"

# USD per 1M tokens (input, output) — for the approximate /usage tally.
PRICES = {m: (pin, pout) for m, (_, pin, pout) in MODEL_CATALOG.items()}


def model_name(model: str) -> str:
    return MODEL_CATALOG[model][0]

WORKSPACE = Path(
    os.environ.get("COUNCIL_WORKSPACE", str(Path.home() / "council-workspace"))
).resolve()

MAX_PARALLEL = max(1, int(os.environ.get("COUNCIL_MAX_PARALLEL", "4")))
MAX_TOOL_RESULT_CHARS = 50_000   # protect the orchestrator's context
WORKER_MAX_ITERATIONS = 60       # worker agentic-loop cap
ORCH_MAX_ITERATIONS = 40         # orchestrator loop cap per user turn
BASH_TIMEOUT = 180               # seconds per shell command
CACHE_MARKERS_PER_CONVO = 3      # rotating message breakpoints (+1 on system = 4 max)

DIM, RESET, BOLD = "\x1b[2m", "\x1b[0m", "\x1b[1m"
CYAN, YELLOW, MAGENTA = "\x1b[36m", "\x1b[33m", "\x1b[35m"

GOAL: str | None = None            # standing goal, set via /goal
LOOP_STATE = {"finished": None}    # set by finish_goal: (status, summary)

client = anthropic.Anthropic()

# ---------------------------------------------------------------- output & usage

_print_lock = threading.Lock()
_ctx = threading.local()  # per-thread tag for parallel subagent output


def emit(text: str, color: str = "") -> None:
    tag = getattr(_ctx, "tag", "")
    prefix = f"{DIM}[{tag}]{RESET} " if tag else ""
    with _print_lock:
        print(f"{prefix}{color}{text}{RESET}" if color else f"{prefix}{text}", flush=True)


# model -> [uncached_input, cache_write, cache_read, output]
_usage: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
_usage_lock = threading.Lock()


def track(model: str, u) -> None:
    with _usage_lock:
        row = _usage[model]
        row[0] += u.input_tokens or 0
        row[1] += getattr(u, "cache_creation_input_tokens", 0) or 0
        row[2] += getattr(u, "cache_read_input_tokens", 0) or 0
        row[3] += u.output_tokens or 0


def row_cost(model: str, row: list[int]) -> float:
    pin, pout = PRICES.get(model, PRICES[ORCHESTRATOR_MODEL])
    unc, cw, cr, out = row
    return (unc * pin + cw * 1.25 * pin + cr * 0.10 * pin + out * pout) / 1e6


def total_cost() -> float:
    with _usage_lock:
        return sum(row_cost(m, row) for m, row in _usage.items())


def clip(s: str) -> str:
    if len(s) <= MAX_TOOL_RESULT_CHARS:
        return s
    return s[:MAX_TOOL_RESULT_CHARS] + f"\n...[truncated {len(s) - MAX_TOOL_RESULT_CHARS} chars]"


# ---------------------------------------------------------------- prompt caching

def refresh_cache_markers(messages: list, keep: int = CACHE_MARKERS_PER_CONVO) -> None:
    """Maintain a rotating window of cache breakpoints on the conversation.

    Marks the last block of the final user message (the newest content before
    each request) and keeps at most `keep` such markers, dropping the oldest.
    Only touches dict blocks we constructed; assistant pydantic blocks are
    echoed untouched. With the system-prompt marker this stays within the
    4-breakpoint API limit.
    """
    marked = []
    for m in messages:
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for block in m["content"]:
            if isinstance(block, dict) and "cache_control" in block:
                marked.append(block)
    final = messages[-1]
    if final.get("role") == "user" and isinstance(final.get("content"), list):
        last = final["content"][-1]
        if isinstance(last, dict) and "cache_control" not in last:
            last["cache_control"] = {"type": "ephemeral"}
            marked.append(last)
    while len(marked) > keep:
        marked.pop(0).pop("cache_control", None)


def user_text(text: str) -> dict:
    """User message in block form so cache markers can attach to it."""
    return {"role": "user", "content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------- role configuration

# Built by configure_council() — the orchestrator prompt and tool descriptions
# name the chosen models and workspace, so they must be rebuilt after the
# startup pickers run.
ORCH_SYSTEM = ""
ORCH_TOOLS: list = []
WORKER_SYSTEM_BLOCKS: list = []
MCP_SERVERS: list = []  # loaded from mcp.json in the workspace (or COUNCIL_MCP)

MEMORY_CLIP = 6000  # max chars of MEMORY.md injected into the system prompts


def read_memory() -> str:
    try:
        text = (WORKSPACE / "MEMORY.md").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if len(text) > MEMORY_CLIP:
        text = text[:MEMORY_CLIP] + "\n...[memory clipped]"
    return text


def load_mcp_config() -> None:
    """Read MCP server definitions from mcp.json in the workspace (or the file
    named by COUNCIL_MCP): a JSON list of {name, url, authorization_token?}."""
    global MCP_SERVERS
    MCP_SERVERS = []
    path = Path(os.environ.get("COUNCIL_MCP", str(WORKSPACE / "mcp.json")))
    if not path.exists():
        return
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("expected a JSON list of server objects")
        for e in entries:
            server = {"type": "url", "name": e["name"], "url": e["url"]}
            if e.get("authorization_token"):
                server["authorization_token"] = e["authorization_token"]
            MCP_SERVERS.append(server)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        MCP_SERVERS = []
        print(f"  (ignoring MCP config {path}: {e})")


def configure_council(orch: str, worker: str, routine: str) -> None:
    """Set the council's models and rebuild the prompts/tools that name them
    (or the workspace path)."""
    global ORCHESTRATOR_MODEL, WORKER_MODEL, ROUTINE_MODEL
    global ORCH_SYSTEM, ORCH_TOOLS, WORKER_SYSTEM_BLOCKS
    ORCHESTRATOR_MODEL, WORKER_MODEL, ROUTINE_MODEL = orch, worker, routine
    wname, rname = model_name(worker), model_name(routine)

    memory = read_memory()

    worker_system = f"""You are the hands-on worker of a model council, operating on the user's Windows machine (Git Bash is the shell behind the bash tool; prefer forward-slash paths).

Your working directory is {WORKSPACE}. All file work must stay under it; paths outside are blocked by the harness. You also have server-side web_search and web_fetch tools for research — their results arrive automatically.

Complete the task you are given end to end, then report what you did and the outcome, citing actual command output or file contents as evidence. If tests fail or a step errors, say so plainly with the output — never claim unverified success.

Don't add features, refactors, or abstractions beyond what the task requires.

Shared council memory lives in MEMORY.md at the workspace root. When you learn something durable — a correction, a constraint, a confirmed approach and why it mattered — append it as one '- ' bullet. Don't duplicate existing entries; fix entries that prove wrong."""
    if memory:
        worker_system += f"\n\nCurrent council memory:\n{memory}"
    if MCP_SERVERS:
        worker_system += (
            "\n\nConnected MCP servers: "
            + ", ".join(s["name"] for s in MCP_SERVERS)
            + " — their tools are available to you directly."
        )

    WORKER_SYSTEM_BLOCKS = [
        {"type": "text", "text": worker_system, "cache_control": {"type": "ephemeral"}}
    ]

    ORCH_SYSTEM = f"""You are the orchestrator of a three-model council running in a local CLI on the user's machine.

Your council:
- delegate_task -> Claude {wname}, an autonomous worker with shell and file access confined to {WORKSPACE}. Use it for substantive hands-on work: writing or editing code, running commands, analyzing files, multi-step builds. It works best from ONE complete brief — include the goal, all relevant context, constraints, and what "done" looks like, rather than drip-feeding instructions.
- quick_task -> Claude {rname}, fast and cheap, no tools. Use it for routine text work: drafting messages or emails, summaries, rewrites, reformatting, boilerplate, classification.
- run_workflow -> a multi-stage pipeline engine. Stages run in sequence; the tasks inside a stage run in parallel, and every later stage automatically receives the outputs of all earlier stages. Use it for fan-out/fan-in patterns (e.g. three parallel research tasks, then one synthesis task) instead of sequencing many turns yourself.
- finish_goal -> declare the user's standing goal achieved or blocked. An "achieved" claim is audited by an independent fresh-context verifier before it counts; if the verifier refutes it, its findings come back to you as more work.

You yourself have no file or shell access — anything hands-on goes through the tools.

How to work:
- Answer trivial questions directly; don't delegate what you can answer in a sentence.
- Route routine text work to quick_task and substantive work to delegate_task.
- Tool calls you issue in the same turn run IN PARALLEL — fan out independent subtasks freely. For dependent stages, either sequence turns yourself (when you need to verify between stages) or use run_workflow (when the pipeline shape is known up front).
- Parallel workers share one workspace: never assign two concurrent tasks that write the same files.
- When a worker reports back, check the result actually addresses the task before telling the user it's done. Report failures honestly, with the evidence the worker gave.
- When you have enough information to act, act. If weighing a choice, give a recommendation, not a survey.
- When a standing goal is set (it appears in a <council-goal> reminder), keep every turn pointed at it. In autonomous loop turns the user is away: never ask questions — decide, delegate, and verify. Only call finish_goal "achieved" when you have concrete evidence the goal is fully met, and "blocked" only when work truly cannot proceed without the user.
- The council keeps a shared memory file, MEMORY.md at the workspace root; workers maintain it. When a task surfaces a durable learning, tell the worker to record it there.
- Lead your replies with the outcome. Write readable prose, not fragments."""

    if memory:
        ORCH_SYSTEM += (
            "\n\nCouncil memory (MEMORY.md contents at session start):\n" + memory
        )

    task_spec = {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "enum": ["worker", "routine"],
                "description": f"worker = {wname} with shell/files; routine = {rname}, text only.",
            },
            "task": {"type": "string", "description": "Complete, self-contained brief."},
            "effort": {
                "type": "string",
                "enum": ["low", "medium", "high", "xhigh"],
                "description": "Worker effort (ignored for routine). Default xhigh.",
            },
        },
        "required": ["agent", "task"],
        "additionalProperties": False,
    }

    ORCH_TOOLS = [
        {
            "name": "delegate_task",
            "description": (
                f"Hand a substantive task to the {wname} worker agent, which has shell and "
                "file access inside the council workspace. Call this for coding, running "
                "commands, analyzing files, or any multi-step hands-on work. Provide the "
                "complete brief up front: goal, relevant context, constraints, and what "
                "done looks like. Multiple delegate_task calls in one turn run in parallel."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Complete, self-contained task brief for the worker.",
                    },
                    "effort": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "xhigh"],
                        "description": (
                            "Worker effort. Default xhigh for coding/agentic work; "
                            "medium or low for simple mechanical jobs."
                        ),
                    },
                },
                "required": ["task"],
                "additionalProperties": False,
            },
        },
        {
            "name": "quick_task",
            "description": (
                f"Send a routine, tool-free text task to {rname}: drafting messages or "
                "emails, summaries, rewrites, formatting, classification, boilerplate. "
                "Fast and cheap; returns plain text. Multiple quick_task calls in one "
                "turn run in parallel."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Self-contained prompt, including any text to operate on.",
                    }
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "run_workflow",
            "description": (
                "Run a multi-stage workflow in one call. `stages` is a list of stages; "
                "each stage is a list of tasks that run IN PARALLEL; stages run in "
                "sequence, and every task in a later stage automatically receives the "
                "full outputs of all earlier stages appended to its brief. Use for "
                "fan-out/fan-in pipelines, e.g. stage 1 = three parallel research tasks, "
                "stage 2 = one synthesis task. Returns the labeled output of every task."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "stages": {
                        "type": "array",
                        "description": "Stages in execution order.",
                        "items": {"type": "array", "items": task_spec},
                    }
                },
                "required": ["stages"],
                "additionalProperties": False,
            },
        },
        {
            "name": "finish_goal",
            "description": (
                "Declare the user's standing goal finished. Use status \"achieved\" ONLY "
                "when you have concrete evidence the goal is fully met — an independent "
                "fresh-context verifier then audits the claim against the workspace, and "
                "if it refutes you its findings come back as more work. Use status "
                "\"blocked\" when the goal cannot proceed without the user. Never call "
                "this when no standing goal is set."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["achieved", "blocked"]},
                    "summary": {
                        "type": "string",
                        "description": "1-3 sentences: what was accomplished, or what is blocking.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": (
                            "Concrete evidence for an achieved claim: files created, "
                            "tests run and their output, checks performed."
                        ),
                    },
                },
                "required": ["status", "summary"],
                "additionalProperties": False,
            },
        },
    ]


configure_council(ORCHESTRATOR_MODEL, WORKER_MODEL, ROUTINE_MODEL)


# ---------------------------------------------------------------- worker tool handlers

BASH_EXE = shutil.which("bash")


def run_bash(tool_input: dict) -> str:
    if tool_input.get("restart"):
        return "Shell session restarted."
    cmd = tool_input.get("command", "")
    emit(f"  $ {cmd}", DIM)
    try:
        if BASH_EXE:
            proc = subprocess.run(
                [BASH_EXE, "-c", cmd], cwd=WORKSPACE,
                capture_output=True, text=True, timeout=BASH_TIMEOUT,
            )
        else:
            proc = subprocess.run(
                cmd, shell=True, cwd=WORKSPACE,
                capture_output=True, text=True, timeout=BASH_TIMEOUT,
            )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {BASH_TIMEOUT}s"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        out += f"\n[exit code {proc.returncode}]"
    return out.strip() or "(no output)"


def safe_path(raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = WORKSPACE / p
    resolved = p.resolve()
    if resolved != WORKSPACE and not resolved.is_relative_to(WORKSPACE):
        raise ValueError(f"path {raw!r} is outside the workspace {WORKSPACE}")
    return resolved


def run_text_editor(inp: dict) -> str:
    cmd = inp["command"]
    path = safe_path(inp["path"])
    rel = path.relative_to(WORKSPACE) if path != WORKSPACE else Path(".")
    emit(f"  ✎ {cmd} {rel}", DIM)

    if cmd == "view":
        if path.is_dir():
            entries = sorted(
                p.name + ("/" if p.is_dir() else "") for p in path.iterdir()
            )
            return "\n".join(entries) or "(empty directory)"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = 1, len(lines)
        if inp.get("view_range"):
            start, end = inp["view_range"]
            if end == -1:
                end = len(lines)
        body = "\n".join(f"{i}\t{l}" for i, l in enumerate(lines[start - 1:end], start))
        return body or "(empty file)"

    if cmd == "create":
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        path.write_text(inp["file_text"], encoding="utf-8")
        return f"Created {path}"

    if cmd == "str_replace":
        text = path.read_text(encoding="utf-8")
        n = text.count(inp["old_str"])
        if n == 0:
            return "Error: old_str not found in file"
        if n > 1:
            return f"Error: old_str matches {n} times; it must match exactly once"
        path.write_text(text.replace(inp["old_str"], inp["new_str"]), encoding="utf-8")
        return "Replacement done."

    if cmd == "insert":
        lines = path.read_text(encoding="utf-8").splitlines()
        idx = inp["insert_line"]
        if not 0 <= idx <= len(lines):
            return f"Error: insert_line {idx} out of range (file has {len(lines)} lines)"
        lines[idx:idx] = inp["insert_text"].splitlines()
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return "Insert done."

    return f"Error: unsupported command {cmd!r}"


# ---------------------------------------------------------------- worker (Opus 4.8)

WORKER_TOOLS = [
    {"type": "bash_20250124", "name": "bash"},
    {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
    # Server-side research tools (with dynamic filtering); results arrive
    # inside the same response — no client-side execution.
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8},
]


def worker_request_kwargs() -> dict:
    """Tools (+ MCP connector plumbing when servers are configured)."""
    if not MCP_SERVERS:
        return {"tools": WORKER_TOOLS}
    return {
        "tools": WORKER_TOOLS
        + [{"type": "mcp_toolset", "mcp_server_name": s["name"]} for s in MCP_SERVERS],
        "mcp_servers": MCP_SERVERS,
        "betas": ["mcp-client-2025-11-20"],
    }


def run_worker(task: str, effort: str = "xhigh", quiet: bool = False) -> str:
    if quiet:
        emit(f"▶ worker started (effort={effort}): {task[:80]}...", YELLOW)
    else:
        print(f"\n{YELLOW}{BOLD}▶ {model_name(WORKER_MODEL)} worker (effort={effort}){RESET}")
    messages = [user_text(task)]
    request_kwargs = worker_request_kwargs()
    api = client.beta.messages if "betas" in request_kwargs else client.messages
    for _ in range(WORKER_MAX_ITERATIONS):
        refresh_cache_markers(messages)
        with api.stream(
            model=WORKER_MODEL,
            max_tokens=64000,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            system=WORKER_SYSTEM_BLOCKS,
            messages=messages,
            **request_kwargs,
        ) as stream:
            if quiet:
                resp = stream.get_final_message()
            else:
                for text in stream.text_stream:
                    print(f"{DIM}{text}{RESET}", end="", flush=True)
                resp = stream.get_final_message()
                print()
        track(WORKER_MODEL, resp.usage)

        if resp.stop_reason == "refusal":
            return "[the worker declined this task (safety refusal)]"

        if resp.stop_reason == "pause_turn":
            # Server-side tool loop paused; echo the turn back to resume.
            messages.append({"role": "assistant", "content": resp.content})
            continue

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not tool_uses:
            final = "\n".join(b.text for b in resp.content if b.type == "text")
            if quiet:
                emit("✔ worker finished", YELLOW)
            return final or "(worker returned no text)"

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for tu in tool_uses:
            try:
                if tu.name == "bash":
                    out = run_bash(tu.input)
                elif tu.name == "str_replace_based_edit_tool":
                    out = run_text_editor(tu.input)
                else:
                    out = f"Error: unknown tool {tu.name}"
                results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": clip(out)}
                )
            except Exception as e:  # tool errors go back to the model, not up the stack
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": f"Error: {e}",
                        "is_error": True,
                    }
                )
        messages.append({"role": "user", "content": results})
    return "[worker hit the iteration limit before finishing]"


# ---------------------------------------------------------------- routine (Sonnet 5)

def run_routine(prompt: str, quiet: bool = False) -> str:
    if quiet:
        emit(f"▶ routine task: {prompt[:80]}...", MAGENTA)
    else:
        print(f"\n{MAGENTA}▶ {model_name(ROUTINE_MODEL)} routine task{RESET}")
    # Haiku 4.5 rejects the effort parameter; Sonnet-tier models accept it.
    effort_kwargs = (
        {} if ROUTINE_MODEL == "claude-haiku-4-5"
        else {"output_config": {"effort": "low"}}
    )
    resp = client.messages.create(
        model=ROUTINE_MODEL,
        max_tokens=8192,
        **effort_kwargs,
        messages=[{"role": "user", "content": prompt}],
    )
    track(ROUTINE_MODEL, resp.usage)
    if resp.stop_reason == "refusal":
        return "[the routine model declined this task]"
    if quiet:
        emit("✔ routine task finished", MAGENTA)
    return "".join(b.text for b in resp.content if b.type == "text")


# ---------------------------------------------------------------- workflow engine

def _run_workflow_task(spec: dict, context: str, tag: str) -> str:
    _ctx.tag = tag
    try:
        task = spec["task"]
        if context:
            task += "\n\n## Results from earlier stages\n\n" + context
        if spec.get("agent") == "routine":
            return run_routine(task, quiet=True)
        return run_worker(task, spec.get("effort", "xhigh"), quiet=True)
    finally:
        _ctx.tag = ""


def run_workflow_tool(inp: dict) -> str:
    stages = inp["stages"]
    if not stages or not all(isinstance(s, list) and s for s in stages):
        return "Error: stages must be a non-empty list of non-empty task lists"
    transcript: list[str] = []
    for si, stage in enumerate(stages, 1):
        context = "\n\n".join(transcript)
        with _print_lock:
            print(f"{BOLD}▶ workflow stage {si}/{len(stages)} — "
                  f"{len(stage)} task(s) in parallel{RESET}")
        with ThreadPoolExecutor(max_workers=min(len(stage), MAX_PARALLEL)) as ex:
            futures = [
                ex.submit(_run_workflow_task, spec, context, f"s{si}.t{j}")
                for j, spec in enumerate(stage, 1)
            ]
            outs = []
            for j, f in enumerate(futures, 1):
                try:
                    outs.append(f.result())
                except Exception as e:
                    outs.append(f"[task failed: {e}]")
        for j, (spec, out) in enumerate(zip(stage, outs), 1):
            transcript.append(
                f"### Stage {si}, task {j} ({spec.get('agent', 'worker')})\n{out}"
            )
    return "\n\n".join(transcript)


# ---------------------------------------------------------------- goal & verifier

def verifier_brief(summary: str, evidence: str) -> str:
    return f"""You are an independent verifier for a model council. Another agent claims a goal is complete; your job is to try to REFUTE that claim.

Standing goal: {GOAL}

Claim: {summary}

Evidence offered: {evidence or "(none provided)"}

Audit the claim inside your workspace with fresh eyes. Do not trust the claim or the evidence — re-derive everything: read the relevant files, run the tests or commands yourself, and hunt for unmet parts of the goal, missing files, or broken behavior. Be strict: partially done is not done.

End your report with exactly one line:
VERDICT: CONFIRMED
or
VERDICT: REFUTED"""


def handle_finish_goal(inp: dict) -> str:
    status, summary = inp.get("status"), inp.get("summary", "")
    if not GOAL:
        return "Error: no standing goal is set — nothing to finish."
    if status == "blocked":
        LOOP_STATE["finished"] = ("blocked", summary)
        return ("Acknowledged as blocked. The autonomous loop will stop; tell the user "
                "plainly what is blocking and what you need from them.")
    emit("⚖ independent verifier auditing the completion claim...", CYAN)
    verdict = run_worker(
        verifier_brief(summary, inp.get("evidence", "")), effort="high", quiet=True
    )
    if "VERDICT: CONFIRMED" in verdict:
        LOOP_STATE["finished"] = ("achieved", summary)
        return ("Independent verification PASSED — the goal is confirmed complete. "
                "Write the final summary for the user now.\n\nVerifier report:\n" + verdict)
    return ("Independent verification did NOT confirm completion. Keep working on the "
            "gaps it found before claiming the goal again.\n\nVerifier report:\n" + verdict)


def goal_reminder_message() -> dict | None:
    """Transient per-request reminder of the standing goal (never stored in history)."""
    if not GOAL:
        return None
    text = (
        "<council-goal>\n"
        f"Standing goal: {GOAL}\n"
        "Keep this turn pointed at the goal and verify results against it. When it is "
        'fully achieved call finish_goal (status "achieved"); if it cannot proceed '
        'without the user call finish_goal (status "blocked").\n'
        "</council-goal>"
    )
    if ORCHESTRATOR_MODEL == "claude-opus-4-8":
        # Opus 4.8 supports mid-conversation system messages (operator channel).
        return {"role": "system", "content": text}
    # Fable 5 doesn't; fall back to a system-reminder block in a user turn.
    return {
        "role": "user",
        "content": [{"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}],
    }


# ---------------------------------------------------------------- tool dispatch

def dispatch_tool(tu, quiet: bool, tag: str) -> dict:
    _ctx.tag = tag
    try:
        if tu.name == "delegate_task":
            out = run_worker(tu.input["task"], tu.input.get("effort", "xhigh"), quiet=quiet)
        elif tu.name == "quick_task":
            out = run_routine(tu.input["prompt"], quiet=quiet)
        elif tu.name == "run_workflow":
            out = run_workflow_tool(tu.input)
        elif tu.name == "finish_goal":
            out = handle_finish_goal(tu.input)
        else:
            return {
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": f"Error: unknown tool {tu.name}",
                "is_error": True,
            }
        return {"type": "tool_result", "tool_use_id": tu.id, "content": clip(out)}
    except Exception as e:
        return {
            "type": "tool_result",
            "tool_use_id": tu.id,
            "content": f"Error: {e}",
            "is_error": True,
        }
    finally:
        _ctx.tag = ""


def execute_tool_calls(tool_uses: list) -> list[dict]:
    if len(tool_uses) == 1:
        return [dispatch_tool(tool_uses[0], quiet=False, tag="")]
    print(f"\n{BOLD}▶ running {len(tool_uses)} council tasks in parallel{RESET}")
    with ThreadPoolExecutor(max_workers=min(len(tool_uses), MAX_PARALLEL)) as ex:
        futures = [
            ex.submit(dispatch_tool, tu, True, f"w{i}")
            for i, tu in enumerate(tool_uses, 1)
        ]
        return [f.result() for f in futures]


# ---------------------------------------------------------------- orchestrator (Fable 5)

def _last_fallback_index(blocks) -> int | None:
    idx = None
    for i, b in enumerate(blocks):
        if getattr(b, "type", None) == "fallback":
            idx = i
    return idx


def sanitize_assistant_content(blocks):
    """Echo rules after a mid-output safety fallback: thinking / tool_use blocks
    that precede the final fallback block must be omitted when the turn is sent
    back. With no fallback block, return content unchanged (thinking blocks must
    be echoed verbatim on the same model)."""
    blocks = list(blocks)
    fb = _last_fallback_index(blocks)
    if fb is None:
        return blocks
    drop = {"thinking", "redacted_thinking", "tool_use"}
    return [b for i, b in enumerate(blocks) if i > fb or b.type not in drop]


def executable_tool_uses(blocks):
    """Only execute tool_use blocks that survive the fallback-echo rules."""
    fb = _last_fallback_index(blocks)
    start = 0 if fb is None else fb + 1
    return [b for b in list(blocks)[start:] if b.type == "tool_use"]


def orchestrator_turn(history: list) -> None:
    # Server-side refusal fallback is a Fable 5 capability; with an Opus
    # orchestrator the request must not ask for it. The only supported
    # fallback target is claude-opus-4-8 (regardless of the worker choice).
    fallback_kwargs = (
        {
            "betas": ["server-side-fallback-2026-06-01"],
            "fallbacks": [{"model": "claude-opus-4-8"}],
        }
        if ORCHESTRATOR_MODEL == "claude-fable-5"
        else {}
    )
    for _ in range(ORCH_MAX_ITERATIONS):
        refresh_cache_markers(history)
        # Standing goal rides along as a transient trailing message — after the
        # cached prefix, never stored in history.
        request_messages = history
        reminder = goal_reminder_message()
        if reminder and history and history[-1].get("role") == "user":
            request_messages = history + [reminder]
        with client.beta.messages.stream(
            model=ORCHESTRATOR_MODEL,
            max_tokens=32000,
            **fallback_kwargs,
            # Fable 5: thinking is always on; "summarized" makes it visible.
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": "high"},
            system=[
                {
                    "type": "text",
                    "text": ORCH_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=ORCH_TOOLS,
            messages=request_messages,
        ) as stream:
            for event in stream:
                if event.type == "content_block_delta":
                    if event.delta.type == "thinking_delta" and event.delta.thinking:
                        print(f"{DIM}{event.delta.thinking}{RESET}", end="", flush=True)
                    elif event.delta.type == "text_delta":
                        print(f"{CYAN}{event.delta.text}{RESET}", end="", flush=True)
            resp = stream.get_final_message()
        print()
        track(resp.model if resp.model in PRICES else ORCHESTRATOR_MODEL, resp.usage)

        for b in resp.content:
            if getattr(b, "type", None) == "fallback":
                print(
                    f"{DIM}[safety fallback: {b.from_.model} declined; "
                    f"{b.to.model} continued]{RESET}"
                )

        if resp.stop_reason == "refusal":
            # Pre-output refusal isn't billed; a mid-stream partial is discarded.
            print("The council declined this request (safety refusal on all models).")
            return

        if resp.stop_reason == "pause_turn":
            history.append({"role": "assistant", "content": resp.content})
            continue

        content = sanitize_assistant_content(resp.content)
        tool_uses = executable_tool_uses(resp.content)

        if resp.stop_reason != "tool_use" or not tool_uses:
            history.append({"role": "assistant", "content": content})
            return

        history.append({"role": "assistant", "content": content})
        history.append({"role": "user", "content": execute_tool_calls(tool_uses)})
    print("[orchestrator hit the iteration limit for this turn]")


# ---------------------------------------------------------------- autonomous loop

def loop_turn_prompt(k: int, n: int) -> str:
    return (
        f"[Autonomous loop — iteration {k} of {n}. The user is not watching; do not ask "
        "questions or wait for input.] Review progress toward the standing goal, "
        "delegate the next round of work to the council, and check the results against "
        "the goal. If you have verified the goal is fully achieved, call finish_goal "
        'with status "achieved"; if it cannot proceed without the user, call finish_goal '
        'with status "blocked"; otherwise make as much verified progress as you can '
        "this iteration."
    )


def run_goal_loop(history: list, arg: str) -> None:
    if not GOAL:
        print("No standing goal. Set one first: /goal <description>")
        return
    max_iter, budget = 10, None
    try:
        for tok in arg.split():
            if tok.startswith("$") or "." in tok:
                budget = float(tok.lstrip("$"))
            else:
                max_iter = max(1, int(tok))
    except ValueError:
        print("usage: /loop [max_iterations] [budget_usd]   e.g. /loop 10 2.50")
        return

    LOOP_STATE["finished"] = None
    start_cost = total_cost()
    print(f"{BOLD}▶ autonomous loop: up to {max_iter} iteration(s)"
          + (f", budget ${budget:.2f}" if budget is not None else "")
          + f" — Ctrl+C stops it{RESET}")
    for k in range(1, max_iter + 1):
        spent = total_cost() - start_cost
        if budget is not None and spent >= budget:
            print(f"\n■ loop stopped: budget ${budget:.2f} exhausted (${spent:.2f} spent)")
            return
        print(f"\n{BOLD}― iteration {k}/{max_iter} (loop spend ${spent:.2f}){RESET}")
        checkpoint = len(history)
        history.append(user_text(loop_turn_prompt(k, max_iter)))
        try:
            orchestrator_turn(history)
        except KeyboardInterrupt:
            del history[checkpoint:]
            print("\n■ loop interrupted — iteration rolled back, goal stays set")
            return
        except anthropic.APIStatusError as e:
            del history[checkpoint:]
            print(f"\n■ loop stopped on API error {e.status_code}: {e.message}")
            return
        except anthropic.APIConnectionError:
            del history[checkpoint:]
            print("\n■ loop stopped on a network error")
            return
        if LOOP_STATE["finished"]:
            status, _summary = LOOP_STATE["finished"]
            LOOP_STATE["finished"] = None
            mark = "✔" if status == "achieved" else "■"
            print(f"\n{BOLD}{mark} goal {status} after {k} iteration(s) "
                  f"(loop spend ${total_cost() - start_cost:.2f}){RESET}")
            return
    print(f"\n■ loop ended: {max_iter} iteration(s) without finish_goal — "
          "the goal stays set; run /loop again to continue")


# ---------------------------------------------------------------- session persistence

def session_file(arg: str) -> Path:
    name = arg.strip() or "council-session.json"
    if not name.endswith(".json"):
        name += ".json"
    p = Path(name)
    return p if p.is_absolute() else WORKSPACE / name


def save_session(history: list, arg: str) -> None:
    """Serialize history (+ goal and models) to JSON in the workspace."""
    ser = []
    for m in history:
        content = m["content"]
        if isinstance(content, list):
            content = [
                b if isinstance(b, dict) else b.model_dump(mode="json", exclude_none=True)
                for b in content
            ]
        ser.append({"role": m["role"], "content": content})
    path = session_file(arg)
    path.write_text(
        json.dumps(
            {
                "goal": GOAL,
                "models": [ORCHESTRATOR_MODEL, WORKER_MODEL, ROUTINE_MODEL],
                "history": ser,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"(saved {len(ser)} message(s) to {path})")


def load_session(history: list, arg: str) -> None:
    global GOAL
    path = session_file(arg)
    if not path.exists():
        print(f"no session file at {path}")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"couldn't load session: {e}")
        return
    history.clear()
    history.extend(data.get("history", []))
    GOAL = data.get("goal")
    saved_models = data.get("models", [])
    current = [ORCHESTRATOR_MODEL, WORKER_MODEL, ROUTINE_MODEL]
    print(f"(resumed {len(history)} message(s) from {path}"
          + (f", goal: {GOAL}" if GOAL else "") + ")")
    if saved_models and saved_models != current:
        print(f"  note: session was saved with {', '.join(saved_models)}; "
              "thinking blocks from other models are dropped by the API, which is fine")


# ---------------------------------------------------------------- CLI

def print_usage_tally() -> None:
    if not _usage:
        print("  (no API calls yet)")
        return
    total = 0.0
    for model, row in _usage.items():
        unc, cw, cr, out = row
        cost = row_cost(model, row)
        total += cost
        print(
            f"  {model}: {unc:,} in + {cw:,} cache-write + {cr:,} cache-read "
            f"/ {out:,} out  ~ ${cost:.4f}"
        )
    print(f"  total ~ ${total:.4f}")


def pick_model(role: str, choices: list[str]) -> str:
    """Ask which model to use for a role; Enter takes the first (default) choice."""
    default = choices[0]
    labels = "  ".join(
        f"[{i}] {model_name(m)} (${PRICES[m][0]:g}/${PRICES[m][1]:g} per 1M)"
        for i, m in enumerate(choices, 1)
    )
    while True:
        try:
            raw = input(f"  {role:<12} {labels}  [default 1]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        matches = [m for m in choices if raw in m or raw in model_name(m).lower()]
        if len(matches) == 1:
            return matches[0]
        print(f"    please enter 1-{len(choices)} or a model name")


def pick_workspace() -> None:
    """Ask which folder the worker may touch; Enter keeps the default."""
    global WORKSPACE
    while True:
        try:
            raw = input(f"  workspace    [{WORKSPACE}]: ").strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            break
        candidate = Path(raw).expanduser()
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"    can't use that folder ({e}) — try another path")
            continue
        WORKSPACE = candidate.resolve()
        break


def choose_setup() -> None:
    print(f"{BOLD}Council setup{RESET} — pick a model for each role, and a workspace:")
    orch = pick_model("orchestrator", ROLE_CHOICES["orchestrator"])
    worker = pick_model("worker", ROLE_CHOICES["worker"])
    routine = pick_model("routine", ROLE_CHOICES["routine"])
    pick_workspace()  # must precede configure_council: prompts embed the path
    load_mcp_config()  # mcp.json lives in the chosen workspace
    configure_council(orch, worker, routine)
    print()


def main() -> None:
    os.system("")  # enable ANSI escape handling on Windows consoles
    choose_setup()
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    print(f"{BOLD}Model Council{RESET}")
    print(f"  orchestrator {CYAN}{ORCHESTRATOR_MODEL}{RESET}   "
          f"worker {YELLOW}{WORKER_MODEL}{RESET}   "
          f"routine {MAGENTA}{ROUTINE_MODEL}{RESET}")
    print(f"  workspace: {WORKSPACE}   parallel subagents: up to {MAX_PARALLEL}")
    extras = []
    extras.append("memory: MEMORY.md loaded" if read_memory() else "memory: none yet")
    extras.append(
        "mcp: " + ", ".join(s["name"] for s in MCP_SERVERS) if MCP_SERVERS
        else "mcp: none (add mcp.json to the workspace)"
    )
    print(f"  {'   '.join(extras)}")
    print("  commands: /goal  /loop  /memory  /reload  /save  /resume  /usage  /reset  /quit\n")

    global GOAL
    history: list = []
    while True:
        try:
            user = input(f"{BOLD}you> {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user in ("/quit", "/exit"):
            break
        if user == "/reset":
            history.clear()
            print("(history cleared" + (", goal kept — /goal clear to unset)" if GOAL else ")"))
            continue
        if user == "/usage":
            print_usage_tally()
            continue
        if user == "/memory":
            mem = read_memory()
            print(mem if mem else "(no MEMORY.md in the workspace yet — "
                  "workers create it as they learn things)")
            continue
        if user == "/reload":
            load_mcp_config()
            configure_council(ORCHESTRATOR_MODEL, WORKER_MODEL, ROUTINE_MODEL)
            print("(reloaded MEMORY.md and mcp.json into the system prompts — "
                  f"memory: {'loaded' if read_memory() else 'none'}, "
                  f"mcp: {', '.join(s['name'] for s in MCP_SERVERS) or 'none'}; "
                  "note: this rebuilds the prompt cache on the next request)")
            continue
        if user == "/goal" or user.startswith("/goal "):
            arg = user[len("/goal"):].strip()
            if not arg:
                print(f"  goal: {GOAL}" if GOAL else "  (no goal set — /goal <description>)")
            elif arg.lower() in ("clear", "off", "none"):
                GOAL = None
                print("(goal cleared)")
            else:
                GOAL = arg
                print("(goal set — every turn now works toward it; "
                      "/loop [n] [budget] runs autonomously)")
            continue
        if user == "/loop" or user.startswith("/loop "):
            run_goal_loop(history, user[len("/loop"):].strip())
            continue
        if user == "/save" or user.startswith("/save "):
            try:
                save_session(history, user[len("/save"):])
            except OSError as e:
                print(f"couldn't save session: {e}")
            continue
        if user == "/resume" or user.startswith("/resume "):
            load_session(history, user[len("/resume"):])
            continue

        checkpoint = len(history)
        history.append(user_text(user))
        try:
            orchestrator_turn(history)
        except KeyboardInterrupt:
            del history[checkpoint:]
            print("\n(interrupted — turn rolled back)")
        except anthropic.APIStatusError as e:
            del history[checkpoint:]
            print(f"\nAPI error {e.status_code}: {e.message}")
            if ORCHESTRATOR_MODEL in str(e.message):
                print("Note: Fable 5 requires 30-day data retention on your org; "
                      "a ZDR org gets 400 on every request.")
        except anthropic.APIConnectionError:
            del history[checkpoint:]
            print("\nNetwork error — check your connection and retry.")

    print("\nSession usage:")
    print_usage_tally()


if __name__ == "__main__":
    main()
