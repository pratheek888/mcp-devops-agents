"""Command line: `devops-agents ask "..."`, `devops-agents demo`, `devops-agents tools`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .agents import AgentRuntime, AuditLog, Budget, MasterAgent, ToolPolicy, UsageTracker
from .agents.registry import DEFAULT_SPECIALISTS
from .agents.specialist import SpecialistAgent
from .backends import backend_mode

DEMO_QUESTION = (
    "payments-api in the prod namespace is failing and customers can't pay. Find out what's wrong, "
    "what changed, and how bad the impact is. If a rollback to the last healthy version would fix it, "
    "do it and confirm recovery."
)

DIM, BOLD, RED, GREEN, YELLOW, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    DIM = BOLD = RED = GREEN = YELLOW = RESET = ""


def _print_event(kind: str, agent: str, data: dict[str, Any]) -> None:
    if kind == "delegate":
        targets = ", ".join(t.removeprefix("ask_").removesuffix("_agent") for t in data["to"])
        print(f"{BOLD}master{RESET} → {targets}", file=sys.stderr)
    elif kind == "start":
        task = data["task"].replace("\n", " ")
        print(
            f"  {BOLD}{agent}{RESET} {DIM}{task[:110]}{'…' if len(task) > 110 else ''}{RESET}",
            file=sys.stderr,
        )
    elif kind == "tool":
        args = json.dumps(data["args"], separators=(",", ":"))
        if not data["allowed"]:
            mark = f"{RED}✗ blocked{RESET}"
        elif data["read_only"]:
            mark = f"{DIM}read{RESET}"
        else:
            mark = f"{YELLOW}WRITE{RESET}"
        err = f" {RED}(error){RESET}" if data["is_error"] and data["allowed"] else ""
        print(f"    {mark} {agent}.{data['tool']} {DIM}{args[:90]}{RESET}{err}", file=sys.stderr)
    elif kind == "done":
        print(f"  {GREEN}✓{RESET} {agent} reported", file=sys.stderr)


async def _approve(agent: str, tool: str, args: dict[str, Any]) -> bool:
    prompt = f"\n{YELLOW}Approve WRITE{RESET} {agent}.{tool} {json.dumps(args)} ? [y/N] "
    answer = await asyncio.to_thread(input, prompt)
    return answer.strip().lower() in {"y", "yes"}


def _usage_table(rows: list[dict[str, Any]]) -> str:
    cols = [
        "agent",
        "llm_calls",
        "tool_calls",
        "blocked_tool_calls",
        "input_tokens",
        "output_tokens",
        "est_cost_usd",
    ]
    widths = {
        c: max(len(c), *(len(f"{r[c]:,}" if isinstance(r[c], int) else str(r[c])) for r in rows))
        for c in cols
    }
    fmt = lambda v: f"{v:,}" if isinstance(v, int) else str(v)  # noqa: E731
    lines = ["  ".join(c.ljust(widths[c]) for c in cols)]
    lines += ["  ".join(fmt(r[c]).ljust(widths[c]) for c in cols) for r in rows]
    return "\n".join(lines)


async def _ask(args: argparse.Namespace, question: str) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set. Export it, or run `devops-agents tools` to explore "
            "the MCP servers without an LLM.",
            file=sys.stderr,
        )
        return 2
    from .agents.llm import AnthropicLLM

    if args.reset_mock and backend_mode() == "mock":
        from .backends.mock import reset_state

        reset_state()
    runtime = AgentRuntime(
        llm=AnthropicLLM(model=args.model),
        policy=ToolPolicy(allow_writes=args.allow_writes, auto_approve=args.yes, approver=_approve),
        usage=UsageTracker(Budget(max_tokens=args.max_tokens, max_tool_calls=args.max_tool_calls)),
        audit=AuditLog(Path(args.audit_log) if args.audit_log else None),
        on_event=_print_event if not args.quiet else (lambda *a: None),
    )
    mode = (
        "read-only"
        if not args.allow_writes
        else ("writes auto-approved" if args.yes else "writes need approval")
    )
    print(
        f"{DIM}backend={backend_mode()}  model={runtime.llm.model}  policy={mode}{RESET}\n", file=sys.stderr
    )
    async with MasterAgent(runtime) as master:
        result = await master.run(question)
    print("\n" + result.answer)
    if result.stopped_reason:
        print(f"\n{YELLOW}Stopped early: {result.stopped_reason}{RESET}")
    print(f"\n{DIM}{_usage_table(result.usage)}{RESET}", file=sys.stderr)
    if args.audit_log:
        print(f"{DIM}audit log: {args.audit_log}{RESET}", file=sys.stderr)
    return 0


async def _tools() -> int:
    """List every MCP server's tools and whether policy treats them as read-only."""

    class _Nil:
        async def complete(self, **_: Any) -> Any:  # never called
            raise RuntimeError

    rt = AgentRuntime(llm=_Nil())
    for spec in DEFAULT_SPECIALISTS:
        async with SpecialistAgent(spec, rt) as agent:
            print(f"{BOLD}{spec.name}{RESET} agent  ({', '.join(s.name for s in spec.servers)} MCP server)")
            for t in agent.tools.values():
                tag = f"{DIM}read {RESET}" if t.read_only else f"{YELLOW}WRITE{RESET}"
                print(f"  {tag} {t.name:<22} {DIM}{t.description.splitlines()[0][:80]}{RESET}")
            print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="devops-agents", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--allow-writes", action="store_true", help="let agents use write tools (with approval)"
        )
        sp.add_argument(
            "--yes", action="store_true", help="auto-approve write tools (only with --allow-writes)"
        )
        sp.add_argument("--model", default=None, help="Claude model id (default: $DEVOPS_AGENTS_MODEL)")
        sp.add_argument("--max-tokens", type=int, default=400_000, help="session token budget")
        sp.add_argument("--max-tool-calls", type=int, default=60, help="session tool-call budget")
        sp.add_argument("--audit-log", default="audit.jsonl", help="JSONL audit log path ('' to disable)")
        sp.add_argument("--quiet", action="store_true", help="hide the live agent trace")
        sp.add_argument(
            "--no-reset-mock",
            dest="reset_mock",
            action="store_false",
            help="keep mock remediation state from a previous run",
        )

    ask = sub.add_parser("ask", help="ask the agents a question")
    ask.add_argument("question")
    common(ask)
    demo = sub.add_parser("demo", help="run the payments-api incident scenario (mock backends)")
    common(demo)
    sub.add_parser("tools", help="list MCP tools per agent (no LLM needed)")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd == "tools":
        sys.exit(asyncio.run(_tools()))
    if args.cmd == "demo":
        os.environ.setdefault("DEVOPS_BACKEND", "mock")
        sys.exit(asyncio.run(_ask(args, DEMO_QUESTION)))
    sys.exit(asyncio.run(_ask(args, args.question)))


if __name__ == "__main__":
    main()
