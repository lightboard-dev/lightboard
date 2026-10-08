"""Argparse setup, command dispatch, and executable lifecycle."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

from git import resolve_context, verify_integration
from lifecycle import add_entry, add_task, block_task, claim_task, done_task, review_task, set_status
from models import LightboardError
from render import (
    _rich_console,
    _rich_imports,
    _rich_renderable,
    _render_integration,
    _render_review_gate,
    render_detail,
    render_list,
)
from review import (
    _add_audit_task,
    _add_fix_task,
    _add_verify_task,
    _audit_reviewer,
    _finding_list_result,
    _infer_verify_task,
    create_finding,
    reopen_finding,
    review_gate,
    review_state,
    verify_finding,
)
from snapshot import (
    _compact_snapshot,
    _detail_task,
    _filtered_snapshot,
    _protocol,
    _ready_snapshot,
    build_snapshot,
    get_task,
)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="local coordination board shared by git worktrees")
    parser.add_argument("--json", action="store_true", help="machine-readable canonical protocol output")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, json_flag: bool = True) -> argparse.ArgumentParser:
        subcommand = commands.add_parser(name, help=help_text)
        if json_flag:
            subcommand.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        return subcommand

    def command_group(name: str, help_text: str):
        group = commands.add_parser(name, help=help_text)
        group_commands = group.add_subparsers(dest=f"{name}_command", required=True)
        return group_commands

    def leaf(group_commands, name: str, help_text: str):
        subcommand = group_commands.add_parser(name, help=help_text)
        subcommand.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        return subcommand

    listing = command("list", "list tasks")
    listing.add_argument("--watch", action="store_true", help="refresh a Rich Live view while attached to a terminal")
    listing.add_argument("--active", action="store_true", help="show only active tasks")
    listing.add_argument("--limit", type=_positive_int, help="show at most this many tasks")
    listing.add_argument("--agent", help="show only tasks owned by this agent")
    command("state", "show the canonical board snapshot")
    command("swarm", "show the full canonical swarm snapshot")

    get = command("get", "show one task")
    get.add_argument("task_id")

    add = command("add", "add a task")
    add.add_argument("task_id")
    add.add_argument("--title", required=True)
    add.add_argument("--depends-on", nargs="+", default=[])
    add.add_argument("--follow-up-to")

    claim = command("claim", "atomically claim a TODO task or stale active task")
    claim.add_argument("task_id")
    claim.add_argument("--agent", required=True)
    claim.add_argument("--branch", required=True)
    claim.add_argument("--touches", action="append", nargs="+", default=[])
    claim.add_argument("--force", action="store_true", help="reclaim only a stale active task")

    status = command("status", "advance a task through an allowed manual transition")
    status.add_argument("task_id")
    status.add_argument("status")

    for name, help_text in (
        ("note", "append a note"),
        ("contract", "append a contract"),
        ("block", "record the current blocker"),
        ("review", "record a legacy finding; use review state/gate <task> for Review Mode"),
    ):
        entry = command(name, help_text)
        entry.add_argument("task_id")
        entry.add_argument("text")

    done = command("done", "verify and mark a task done")
    done.add_argument("task_id")
    done.add_argument("--sha", required=True)
    done.add_argument("--summary", required=True)
    done.add_argument("--verified", action="append", default=[])
    done.add_argument("--unverified", action="append", default=[])
    done.add_argument("--not-reviewed", dest="not_reviewed", action="append", default=[])

    audit_commands = command_group("audit", "create and complete read-only audits")
    audit_add = leaf(audit_commands, "add", "add an AUDIT bound to the current target HEAD")
    audit_add.add_argument("audit_id")
    audit_add.add_argument("--target", required=True)
    audit_add.add_argument("--lens", required=True)
    audit_add.add_argument("--group", required=True)
    audit_add.add_argument("--slot", required=True, choices=("A", "B"))
    audit_add.add_argument("--scope", required=True, nargs="+")

    finding_commands = command_group("finding", "create and update typed review findings")
    finding_add = leaf(finding_commands, "add", "add a finding to an AUDIT")
    finding_add.add_argument("audit_id")
    finding_add.add_argument("--severity", required=True)
    finding_add.add_argument("--lens", required=True)
    finding_add.add_argument("--claim", required=True)
    finding_add.add_argument("--evidence", required=True)
    finding_add.add_argument("--scenario", required=True)
    finding_add.add_argument("--required", dest="required_property", required=True)
    finding_add.add_argument("--reviewer")
    finding_list = leaf(finding_commands, "list", "list findings for a target task")
    finding_list.add_argument("--target", required=True)
    finding_verify = leaf(finding_commands, "verify", "mark a fixed finding VERIFIED")
    finding_verify.add_argument("finding_id", type=_positive_int)
    finding_verify.add_argument("--verify", dest="verify_task_id")
    finding_reopen = leaf(finding_commands, "reopen", "reopen a finding after failed verification")
    finding_reopen.add_argument("finding_id", type=_positive_int)
    finding_reopen.add_argument("--reason", required=True)
    finding_reopen.add_argument("--verify", dest="verify_task_id")

    fix_commands = command_group("fix", "create finding FIX tasks")
    fix_add = leaf(fix_commands, "add", "add a FIX for findings")
    fix_add.add_argument("fix_id")
    fix_add.add_argument("--target", required=True)
    fix_add.add_argument("--findings", required=True, nargs="+", type=_positive_int)

    verify_commands = command_group("verify", "create finding VERIFY tasks")
    verify_add = leaf(verify_commands, "add", "add a VERIFY for a completed FIX")
    verify_add.add_argument("verify_id")
    verify_add.add_argument("--fix", dest="fix_task_id", required=True)
    verify_add.add_argument("--findings", required=True, nargs="+", type=_positive_int)

    command("ready", "list tasks whose dependencies are verified")
    verify = command("verify-integration", "verify direct dependency commits are integrated")
    verify.add_argument("task_id")
    return parser


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    return _parser().parse_args(argv)


def _execute(context, args: argparse.Namespace):
    if args.command in {"list", "state"}:
        return _filtered_snapshot(
            context,
            active=getattr(args, "active", False),
            limit=getattr(args, "limit", None),
            agent=getattr(args, "agent", None),
        )
    if args.command == "swarm":
        return build_snapshot(context)
    if args.command == "get":
        return get_task(context, args.task_id)
    if args.command == "ready":
        return _ready_snapshot(context)
    if args.command == "verify-integration":
        return verify_integration(context, args.task_id)
    if args.command == "audit" and args.audit_command == "add":
        return _add_audit_task(
            context,
            args.audit_id,
            args.target,
            args.lens,
            args.group,
            args.slot,
            args.scope,
        )
    if args.command == "finding":
        if args.finding_command == "add":
            reviewer = _audit_reviewer(context, args.audit_id, args.reviewer)
            return create_finding(
                context,
                args.audit_id,
                reviewer,
                args.severity,
                args.lens,
                args.claim,
                args.evidence,
                args.scenario,
                args.required_property,
            )
        if args.finding_command == "list":
            return _finding_list_result(context, args.target)
        if args.finding_command == "verify":
            verify_task_id = args.verify_task_id or _infer_verify_task(context, args.finding_id)
            return verify_finding(context, verify_task_id, args.finding_id, "VERIFIED")
        if args.finding_command == "reopen":
            return reopen_finding(context, args.finding_id, args.reason, args.verify_task_id)
    if args.command == "fix" and args.fix_command == "add":
        return _add_fix_task(context, args.fix_id, args.target, args.findings)
    if args.command == "verify" and args.verify_command == "add":
        return _add_verify_task(context, args.verify_id, args.fix_task_id, args.findings)
    if args.command == "add":
        return add_task(context, args.task_id, args.title, args.depends_on, args.follow_up_to)
    if args.command == "claim":
        touches = [path for group in args.touches for path in group]
        return claim_task(context, args.task_id, args.agent, args.branch, touches, args.force)
    if args.command == "status":
        return set_status(context, args.task_id, args.status)
    if args.command == "note":
        return add_entry(context, args.task_id, "NOTE", args.text)
    if args.command == "contract":
        return add_entry(context, args.task_id, "CONTRACT", args.text)
    if args.command == "block":
        return block_task(context, args.task_id, args.text)
    if args.command == "review":
        if args.task_id == "state":
            return review_state(context, args.text)
        if args.task_id == "gate":
            return review_gate(context, args.text)
        return review_task(context, args.task_id, args.text)
    if args.command == "done":
        return done_task(
            context,
            args.task_id,
            args.sha,
            args.summary,
            args.verified,
            args.unverified,
            args.not_reviewed,
            auto_fix=True,
        )
    raise LightboardError(f"unknown command: {args.command}")


def _watch(context, args: argparse.Namespace) -> int:
    imports = _rich_imports()
    if imports is None or not sys.stdout.isatty():
        render_list(_filtered_snapshot(context, args.active, args.limit, args.agent))
        return 0
    Live = imports[2]
    console = _rich_console(imports)
    try:
        with Live(
            _rich_renderable(imports, _filtered_snapshot(context, args.active, args.limit, args.agent)),
            console=console,
            refresh_per_second=1,
            screen=False,
        ) as live:
            while True:
                time.sleep(1)
                live.update(_rich_renderable(imports, _filtered_snapshot(context, args.active, args.limit, args.agent)))
    except KeyboardInterrupt:
        return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args: Optional[argparse.Namespace] = None
    try:
        args = _parse(list(sys.argv[1:] if argv is None else argv))
        context = resolve_context(Path.cwd())
        if args.command == "list" and args.watch and not args.json:
            return _watch(context, args)
        result = _execute(context, args)
        if args.json:
            if args.command in {"list", "ready"}:
                result = _compact_snapshot(result)
            elif args.command == "get":
                result = _detail_task(result)
            print(json.dumps(_protocol(result), ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command in {"list", "state", "swarm", "ready"}:
            render_list(result)
        elif args.command == "get":
            render_detail(result)
        elif args.command == "verify-integration":
            print(_render_integration(result))
        elif args.command == "review" and args.task_id == "gate":
            print(_render_review_gate(result))
        elif args.command == "review" and args.task_id == "state":
            print(f"{result['task_id']} review state: {result['state']} at {result['target_head_sha'] or '·'}")
        elif args.command == "finding" and args.finding_command == "list":
            for finding in result["findings"]:
                print(f"#{finding['id']} {finding['status']} {finding['severity']} · {finding['claim']}")
        else:
            print(result.get("message", json.dumps(result, ensure_ascii=False, sort_keys=True)))
        if (args.command == "verify-integration" or (args.command == "review" and args.task_id == "gate")) and not result["ok"]:
            return 1
        return 0
    except LightboardError as exc:
        if args is not None and args.json:
            print(json.dumps(_protocol({"ok": False, "error": str(exc)}), ensure_ascii=False, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
