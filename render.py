"""Human-readable plain, Rich, detail, and watch-support rendering."""

from __future__ import annotations

import copy
import sys
from typing import Any, List

from snapshot import _summary


def _short_sha(value):
    return value[:8] if value else "·"


def _deps_label(task: dict) -> str:
    verification = task["deps_verification"]
    if verification["total"] == 0:
        return "·"
    suffix = " ✓" if verification["verified"] == verification["total"] else ""
    return f"{verification['verified']}/{verification['total']}{suffix}"


def _status_label(task: dict) -> str:
    label = task["status"]
    if task.get("stale") or task.get("touch_conflicts"):
        label += " ⚠"
    return label


def _event_text(event: dict) -> str:
    payload = event["payload"]
    timestamp = event["created_at"]
    time_label = timestamp[11:19] if len(timestamp) >= 19 else timestamp
    event_type = event["type"]
    task_id = event.get("task_id") or payload.get("task_id", "?")
    if event_type == "CLAIM":
        return f"{time_label}  {payload.get('agent', '·')} claimed {task_id}"
    if event_type == "RECLAIM":
        return f"{time_label}  {payload.get('old_owner', '·')} → {payload.get('new_owner', '·')} reclaimed {task_id}"
    if event_type == "STATUS":
        return f"{time_label}  {task_id} → {payload.get('to_status', '·')}"
    if event_type == "BLOCK":
        return f"{time_label}  {task_id} → BLOCKED"
    if event_type == "REVIEW":
        return f"{time_label}  {payload.get('agent', '·')} review on {task_id}"
    if event_type == "CONTRACT":
        return f"{time_label}  {payload.get('agent', '·')} contract #{payload.get('contract_id', '·')} on {task_id}"
    if event_type == "DONE":
        return f"{time_label}  {task_id} → DONE {_short_sha(payload.get('head_sha'))}"
    if event_type in ("AUDIT_CREATED", "AUDIT_DONE", "FIX_DONE", "VERIFY_DONE"):
        return f"{time_label}  {event_type} {task_id}"
    if event_type in ("FINDING_CREATED", "FINDING_FIXED", "FINDING_VERIFIED", "FINDING_REOPENED"):
        return f"{time_label}  {event_type} #{payload.get('finding_id', '·')}"
    if event_type in ("REVIEW_STALE", "REVIEW_GATE_PASS", "REVIEW_GATE_FAIL"):
        return f"{time_label}  {event_type} {task_id} {_short_sha(payload.get('target_head_sha'))}"
    return f"{time_label}  {event_type} {task_id}"


def _render_plain(snapshot: dict) -> str:
    lines = [
        f"lb · {snapshot['project_root']} · {snapshot.get('branch') or 'detached'}",
        (
            f"{snapshot['summary']['done']} done · {snapshot['summary']['in_progress']} in_progress · "
            f"{snapshot['summary']['blocked']} blocked · {snapshot['summary']['stale']} stale"
            f"     {snapshot['current_time']}"
        ),
        "",
        "TASK                 STATUS        OWNER        HEAD        DEPS       AGE",
    ]
    for task in snapshot["tasks"]:
        lines.append(
            f"{task['id'][:20]:<20} {_status_label(task):<12} {(task['owner'] or '·')[:12]:<12} "
            f"{_short_sha(task['head_sha']):<10} {_deps_label(task):<10} {task['age']}"
        )
        if task.get("block_reason"):
            lines.append(f"{task['id']} └ waiting: {task['block_reason']}")
        if task.get("touch_conflicts"):
            peers = ", ".join(sorted({item["task_id"] for item in task["touch_conflicts"]}))
            lines.append(f"{task['id']} └ conflict: {peers}")
    lines.extend(["", "── events ───────────────────────────────────────────────────────"])
    lines.extend(_event_text(event) for event in snapshot["events"])
    return "\n".join(lines)


def _rich_imports():
    try:
        from rich.console import Console, Group
        from rich.live import Live
        from rich.rule import Rule
        from rich.table import Table
        from rich.text import Text
    except ImportError:
        return None
    return Console, Group, Live, Rule, Table, Text


def _rich_console(imports):
    Console = imports[0]
    return Console(file=sys.stdout, no_color=not sys.stdout.isatty())


def _rich_status(imports, task: dict):
    Text = imports[5]
    style = {
        "DONE": "dim",
        "IN_PROGRESS": "bold white",
        "CLAIMED": "dim cyan",
        "BLOCKED": "bold red",
        "REVIEW": "yellow",
        "TODO": "white",
    }.get(task["status"], "white")
    text = Text(task["status"], style=style)
    if task.get("stale") or task.get("touch_conflicts"):
        text.append(" ⚠", style="bold yellow")
    return text


def _rich_table(imports, snapshot: dict):
    Table = imports[4]
    Text = imports[5]
    table = Table(box=None, padding=(0, 1), pad_edge=False, expand=False, show_edge=False)
    table.add_column("TASK", width=20, no_wrap=True, overflow="ellipsis")
    table.add_column("STATUS", width=14, no_wrap=True, overflow="ellipsis")
    table.add_column("OWNER", width=16, no_wrap=True, overflow="ellipsis")
    table.add_column("HEAD", width=10, no_wrap=True, overflow="ellipsis")
    table.add_column("DEPS", width=9, no_wrap=True, overflow="ellipsis")
    table.add_column("AGE", width=10, no_wrap=True, overflow="ellipsis")
    for task in snapshot["tasks"]:
        table.add_row(
            task["id"],
            _rich_status(imports, task),
            task.get("owner") or "·",
            _short_sha(task.get("head_sha")),
            _deps_label(task),
            task["age"],
        )
        if task.get("block_reason"):
            table.add_row(Text(f"└ waiting: {task['block_reason']}", style="bold red"), "", "", "", "", "")
        if task.get("touch_conflicts"):
            peers = ", ".join(sorted({item["task_id"] for item in task["touch_conflicts"]}))
            table.add_row(Text(f"└ conflict: {peers}", style="yellow"), "", "", "", "", "")
    return table


def _rich_renderable(imports, snapshot: dict):
    Group, Rule, Text = imports[1], imports[3], imports[5]
    header = Text(
        f"lb · {snapshot['project_root']} · {snapshot.get('branch') or 'detached'}\n"
        f"{snapshot['summary']['done']} done · {snapshot['summary']['in_progress']} in_progress · "
        f"{snapshot['summary']['blocked']} blocked · {snapshot['summary']['stale']} stale"
        f"     {snapshot['current_time']}"
    )
    parts: List[Any] = [header, _rich_table(imports, snapshot)]
    parts.append(Rule("events", style="dim"))
    parts.extend(Text(_event_text(event)) for event in snapshot["events"])
    return Group(*parts)


def _visible_for_height(snapshot: dict, imports) -> dict:
    if imports is None or not sys.stdout.isatty():
        return snapshot
    console = _rich_console(imports)
    available = max(1, console.height - 8 - len(snapshot["events"]))
    if len(snapshot["tasks"]) <= available:
        return snapshot
    active = [task for task in snapshot["tasks"] if task["status"] != "DONE"]
    done = [task for task in snapshot["tasks"] if task["status"] == "DONE"]
    tasks = (active + done)[:available]
    result = copy.deepcopy(snapshot)
    result["tasks"] = tasks
    result["summary"] = _summary(tasks)
    return result


def render_list(snapshot: dict) -> None:
    imports = _rich_imports()
    snapshot = _visible_for_height(snapshot, imports)
    if imports is None:
        print(_render_plain(snapshot))
        return
    console = _rich_console(imports)
    console.print(_rich_renderable(imports, snapshot))


def _contract_title(contract: dict) -> str:
    body = contract.get("body", "").replace("\n", " ").strip()
    return body if len(body) <= 72 else body[:69] + "..."


def _detail_lines(task: dict) -> List[str]:
    verification = task["deps_verification"]
    started = task.get("started_at") or task["created_at"]
    deps = "·" if verification["total"] == 0 else f"{verification['verified']}/{verification['total']} ✓"
    lines = [
        f"{task['id']} · {task['status']} · {task.get('owner') or '·'} · {task.get('branch') or '·'}",
        f"started {started} · age {task['age']} · deps {deps}",
        "",
        "summary",
        task.get("summary") or "—",
        "",
        "dependencies",
    ]
    if verification["total"] == 0:
        lines.append("deps ·")
    else:
        for item in task["dependency_verification"]:
            marker = "✓" if item["verified"] else "⚠"
            lines.append(f"{marker} {item['task_id']}    {_short_sha(item.get('head_sha'))}")
    lines.extend(["", f"inherited contracts ({len(task['inherited_contracts'])})", f"full: lb get {task['id']}"])
    for contract in task["inherited_contracts"]:
        lines.append(f"#{contract['id']}  {_contract_title(contract)}")
    lines.extend(["", "blocker / review / follow-up"])
    lines.append(f"blocker: {task.get('block_reason') or '—'}")
    lines.append(f"review: {_contract_title(task['review']) if task.get('review') else '—'}")
    lines.append(f"follow-up: {task.get('follow_up_to') or '—'}")
    if task["task_type"] == "AUDIT":
        result = task.get("audit_result", {})
        lines.extend(
            [
                "",
                "review audit",
                f"target: {task.get('target_task_id') or '—'} @ {_short_sha(task.get('target_head_sha'))}",
                f"lens: {task.get('lens') or '—'} · group: {task.get('review_group_id') or '—'} · slot: {task.get('review_slot') or '—'}",
                f"scope: {', '.join(task.get('scope_paths', [])) or '—'}",
                f"VERIFIED: {', '.join(result.get('verified', [])) or '—'}",
                f"UNVERIFIED: {', '.join(result.get('unverified', [])) or '—'}",
                f"NOT_REVIEWED: {', '.join(result.get('not_reviewed', [])) or '—'}",
            ]
        )
    elif task["task_type"] in ("FIX", "VERIFY"):
        related = task.get("related_findings", [])
        lines.extend(
            [
                "",
                f"review {task['task_type'].lower()}",
                f"target: {task.get('target_task_id') or '—'} @ {_short_sha(task.get('target_head_sha'))}",
                f"fix SHA: {_short_sha(task.get('head_sha') if task['task_type'] == 'FIX' else task.get('fix_head_sha'))}",
                "findings: " + (", ".join(f"#{item['id']} {item['status']}" for item in related) or "—"),
            ]
        )
    elif task.get("review_mode") is not None:
        review = task["review_mode"]
        lines.extend(
            [
                "",
                "review",
                f"state: {review['state']} · target HEAD: {_short_sha(review.get('target_head_sha'))}",
                f"audits: {len(review['audits'])} · findings: {len(review['findings'])} · fixes: {len(review['fixes'])} · verifications: {len(review['verifications'])}",
            ]
        )
    lines.extend(["", "own contracts"])
    if task["contracts"]:
        for contract in task["contracts"]:
            verified = " ✓" if contract["verified"] else ""
            lines.append(f"#{contract['id']}  {_contract_title(contract)}{verified}")
    else:
        lines.append("—")
    lines.extend(["", "touches / conflicts"])
    lines.append(f"touches: {', '.join(task['touches']) if task['touches'] else '—'}")
    if task["touch_conflicts"]:
        lines.extend(f"conflict: {item['task_id']} · {item['path']}" for item in task["touch_conflicts"])
    else:
        lines.append("conflicts: —")
    return lines


def render_detail(task: dict) -> None:
    imports = _rich_imports()
    lines = _detail_lines(task)
    if imports is None:
        print("\n".join(lines))
        return
    console = _rich_console(imports)
    Text = imports[5]
    console.print(Text("\n".join(lines)))


def _render_integration(result: dict) -> str:
    lines = [f"{result['task_id']} integration HEAD {result.get('integration_head') or '·'}"]
    for dependency in result["dependencies"]:
        marker = "✓" if dependency["integrated"] else "⚠"
        lines.append(f"{marker} {dependency['task_id']} {_short_sha(dependency.get('head_sha'))} · {dependency['reason']}")
    lines.append("integration verified" if result["ok"] else "integration verification failed")
    return "\n".join(lines)


def _render_review_gate(result: dict) -> str:
    lines = [
        f"{result['task_id']} review {result['target_head_sha'] or '·'} · {result['state']}",
        f"GATE={'PASS' if result['ok'] else 'FAIL'}",
    ]
    lines.extend(f"- {reason}" for reason in result["reasons"])
    return "\n".join(lines)
