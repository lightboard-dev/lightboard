"""Database record queries and shared SQL helpers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

from db import Board
from models import GitContext, LightboardError, _int_list, _string_list


def task_from_row(row: sqlite3.Row) -> dict:
    task = dict(row)
    task["dependencies"] = _string_list(task.get("dependencies"), f"dependencies for task {task['id']}")
    task["touches"] = _string_list(task.get("touches"), f"touches for task {task['id']}")
    task["scope_paths"] = _string_list(task.get("scope_paths"), f"scope paths for task {task['id']}")
    task["audit_verified"] = _string_list(task.get("audit_verified"), f"verified scope for task {task['id']}")
    task["audit_unverified"] = _string_list(task.get("audit_unverified"), f"unverified scope for task {task['id']}")
    task["audit_not_reviewed"] = _string_list(
        task.get("audit_not_reviewed"), f"not reviewed scope for task {task['id']}"
    )
    task["verified_contract_ids"] = _int_list(
        task.get("verified_contract_ids"), f"verified contracts for task {task['id']}"
    )
    return task


def finding_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def entry_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def event_from_row(row: sqlite3.Row) -> dict:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError) as exc:
        raise LightboardError(f"database error: invalid event payload #{row['id']}") from exc
    if not isinstance(payload, dict):
        raise LightboardError(f"database error: invalid event payload #{row['id']}")
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "type": row["type"],
        "kind": row["type"],
        "created_at": row["created_at"],
        "payload": payload,
    }


def require_task(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise LightboardError(f"task not found: {task_id}")
    return row


def require_finding(connection: sqlite3.Connection, finding_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    if row is None:
        raise LightboardError(f"finding not found: {finding_id}")
    return row


def record_event(
    connection: sqlite3.Connection,
    task_id: str,
    event_type: str,
    payload: Dict[str, Any],
    timestamp: str,
) -> None:
    connection.execute(
        "INSERT INTO events (task_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (task_id, event_type, json.dumps(payload, ensure_ascii=False, sort_keys=True), timestamp),
    )


def require_task_finding_relation(
    connection: sqlite3.Connection,
    task_id: str,
    finding_id: int,
    relation: str,
) -> None:
    linked = connection.execute(
        "SELECT 1 FROM task_findings WHERE task_id = ? AND finding_id = ? AND relation = ?",
        (task_id, finding_id, relation),
    ).fetchone()
    if linked is None:
        raise LightboardError(f"finding {finding_id} is not linked to {relation} task {task_id}")


def load_snapshot_records(context: GitContext, event_limit: int) -> dict:
    with Board(context).read() as connection:
        raw_tasks = [
            task_from_row(row)
            for row in connection.execute("SELECT * FROM tasks ORDER BY created_at, id").fetchall()
        ]
        entries_by_task: Dict[str, List[dict]] = {}
        for row in connection.execute("SELECT id, task_id, kind, body, created_at FROM entries ORDER BY id"):
            entries_by_task.setdefault(row["task_id"], []).append(entry_from_row(row))
        findings = [
            finding_from_row(row)
            for row in connection.execute("SELECT * FROM findings ORDER BY id").fetchall()
        ]
        findings_by_task: Dict[str, List[dict]] = {}
        for finding in findings:
            findings_by_task.setdefault(finding["audit_task_id"], []).append(finding)
        related_findings_by_task: Dict[str, List[dict]] = {}
        relation_rows = connection.execute(
            "SELECT task_id, finding_id, relation FROM task_findings ORDER BY task_id, finding_id, relation"
        ).fetchall()
        findings_by_id = {finding["id"]: finding for finding in findings}
        for relation in relation_rows:
            finding = findings_by_id.get(relation["finding_id"])
            if finding is not None:
                related_findings_by_task.setdefault(relation["task_id"], []).append(
                    {**finding, "relation": relation["relation"]}
                )
        event_rows = connection.execute(
            "SELECT id, task_id, type, payload, created_at FROM events ORDER BY id DESC LIMIT ?", (event_limit,)
        ).fetchall()
        events = [event_from_row(row) for row in reversed(event_rows)]
        all_event_rows = connection.execute(
            "SELECT id, task_id, type, payload, created_at FROM events ORDER BY id"
        ).fetchall()
        all_events = [event_from_row(row) for row in all_event_rows]
    return {
        "raw_tasks": raw_tasks,
        "entries_by_task": entries_by_task,
        "findings": findings,
        "findings_by_task": findings_by_task,
        "related_findings_by_task": related_findings_by_task,
        "events": events,
        "all_events": all_events,
    }


def get_finding_record(context: GitContext, finding_id: int) -> dict:
    with Board(context).read() as connection:
        finding = dict(require_finding(connection, finding_id))
        finding["relations"] = [
            dict(row)
            for row in connection.execute(
                "SELECT task_id, relation FROM task_findings WHERE finding_id = ? ORDER BY relation, task_id",
                (finding_id,),
            )
        ]
    return finding


def list_finding_records(
    context: GitContext,
    target_task_id: Optional[str] = None,
    audit_task_id: Optional[str] = None,
) -> List[dict]:
    clauses: List[str] = []
    values: List[str] = []
    if target_task_id is not None:
        clauses.append("target_task_id = ?")
        values.append(target_task_id)
    if audit_task_id is not None:
        clauses.append("audit_task_id = ?")
        values.append(audit_task_id)
    query = "SELECT * FROM findings"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id"
    with Board(context).read() as connection:
        return [finding_from_row(row) for row in connection.execute(query, values).fetchall()]


def infer_verify_task(context: GitContext, finding_id: int) -> str:
    with Board(context).read() as connection:
        finding = require_finding(connection, finding_id)
        row = connection.execute(
            """
            SELECT tasks.id FROM tasks
            JOIN task_findings ON task_findings.task_id = tasks.id
            WHERE task_findings.finding_id = ? AND task_findings.relation = 'VERIFY'
              AND tasks.task_type = 'VERIFY' AND tasks.status = 'DONE'
              AND tasks.fix_task_id = ?
            ORDER BY tasks.completed_at DESC, tasks.id DESC
            LIMIT 1
            """,
            (finding_id, finding["resolved_by_task_id"]),
        ).fetchone()
    if row is None:
        raise LightboardError(f"no completed VERIFY task is linked to finding {finding_id}")
    return row["id"]
