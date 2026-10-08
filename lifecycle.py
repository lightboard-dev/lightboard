"""Task lifecycle and entry mutations."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, List, Optional, Sequence

from db import Board
from git import _read_only_violation, _verify_commit_lineage
from models import (
    ACTIVE_STATUSES,
    ENTRY_KINDS,
    LENSES,
    MANUAL_STATUSES,
    READ_ONLY_REVIEW_TYPES,
    REVIEW_TASK_TYPES,
    TASK_TYPES,
    GitContext,
    LightboardError,
    _enum,
    _finding_ids,
    _mutation,
    _normalize_scope_paths,
    _normalize_touches,
    _now,
    _optional_text,
    _parse_time,
    _sha,
    _task_id,
    _task_is_stale,
    _text,
)
from repository import record_event, require_task, require_task_finding_relation, require_finding, task_from_row
from review import _audit_result, _validate_task_definition


def add_task(
    context: GitContext,
    task_id: str,
    title: str,
    dependencies: Sequence[str] = (),
    follow_up_to: Optional[str] = None,
    *,
    task_type: str = "FEATURE",
    target_task_id: Optional[str] = None,
    target_base_sha: Optional[str] = None,
    target_head_sha: Optional[str] = None,
    lens: Optional[str] = None,
    review_group_id: Optional[str] = None,
    review_slot: Optional[str] = None,
    scope_paths: Sequence[str] = (),
    finding_ids: Sequence[Any] = (),
    fix_task_id: Optional[str] = None,
    fix_head_sha: Optional[str] = None,
) -> dict:
    task_id = _task_id(task_id)
    title = _text(title, "title")
    task_type = _enum(task_type, "task type", TASK_TYPES)
    dependencies = [_task_id(dependency) for dependency in dependencies]
    finding_ids = _finding_ids(finding_ids)
    target_task_id = _optional_text(target_task_id, "target task id")
    target_base_sha = None if target_base_sha is None else _sha(target_base_sha, "target base SHA")
    target_head_sha = None if target_head_sha is None else _sha(target_head_sha, "target head SHA")
    lens = None if lens is None else _enum(lens, "lens", LENSES)
    review_group_id = _optional_text(review_group_id, "review group id")
    review_slot = None if review_slot is None else _enum(review_slot, "review slot", ("A", "B"))
    scope_paths = _normalize_scope_paths(context, scope_paths)
    fix_task_id = _optional_text(fix_task_id, "fix task id")
    fix_head_sha = None if fix_head_sha is None else _sha(fix_head_sha, "fix head SHA")
    if len(set(dependencies)) != len(dependencies):
        raise LightboardError("dependencies must be unique")
    if task_id in dependencies:
        raise LightboardError("a task cannot depend on itself")
    if follow_up_to is not None:
        follow_up_to = _task_id(follow_up_to)
        if follow_up_to == task_id:
            raise LightboardError("a task cannot follow up to itself")

    timestamp = _now()
    with Board(context).write() as connection:
        _validate_task_definition(
            context,
            connection,
            task_id,
            task_type,
            target_task_id,
            target_base_sha,
            target_head_sha,
            lens,
            review_group_id,
            review_slot,
            scope_paths,
            finding_ids,
            fix_task_id,
            fix_head_sha,
        )
        for dependency in dependencies:
            if connection.execute("SELECT 1 FROM tasks WHERE id = ?", (dependency,)).fetchone() is None:
                raise LightboardError(f"dependency task not found: {dependency}")
        if follow_up_to is not None and connection.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (follow_up_to,)
        ).fetchone() is None:
            raise LightboardError(f"follow-up task not found: {follow_up_to}")
        try:
            if connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is not None:
                raise LightboardError(f"task already exists: {task_id}")
            connection.execute(
                """
                INSERT INTO tasks (
                    id, title, task_type, status, base_sha, target_task_id, target_base_sha, target_head_sha,
                    reviewed_target_head_sha, lens, review_group_id, review_slot, fix_task_id, fix_head_sha,
                    scope_paths, audit_verified, audit_unverified, audit_not_reviewed,
                    dependencies, summary, created_at, updated_at, follow_up_to
                ) VALUES (?, ?, ?, 'TODO', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    title,
                    task_type,
                    context.base_sha,
                    target_task_id,
                    target_base_sha,
                    target_head_sha,
                    target_head_sha if task_type in REVIEW_TASK_TYPES else None,
                    lens,
                    review_group_id,
                    review_slot,
                    fix_task_id,
                    fix_head_sha,
                    json.dumps(scope_paths, separators=(",", ":")),
                    "[]",
                    "[]",
                    "[]",
                    json.dumps(dependencies, separators=(",", ":")),
                    "",
                    timestamp,
                    timestamp,
                    follow_up_to,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise LightboardError(f"cannot add task {task_id}: {exc}") from exc
        for finding_id in finding_ids:
            connection.execute(
                "INSERT INTO task_findings (task_id, finding_id, relation) VALUES (?, ?, ?)",
                (task_id, finding_id, "FIX" if task_type == "FIX" else "VERIFY"),
            )
        if task_type in REVIEW_TASK_TYPES:
            record_event(
                connection,
                task_id,
                f"{task_type}_CREATED",
                {
                    "task_type": task_type,
                    "target_task_id": target_task_id,
                    "target_base_sha": target_base_sha,
                    "target_head_sha": target_head_sha,
                    "review_group_id": review_group_id,
                    "review_slot": review_slot,
                    "lens": lens,
                    "scope_paths": scope_paths,
                    "finding_ids": finding_ids,
                    "fix_task_id": fix_task_id,
                    "fix_head_sha": fix_head_sha,
                },
                timestamp,
            )
    return _mutation(
        f"added {task_id}",
        task_id,
        task_type=task_type,
        target_task_id=target_task_id,
        target_base_sha=target_base_sha,
        target_head_sha=target_head_sha,
        review_group_id=review_group_id,
        review_slot=review_slot,
        scope_paths=scope_paths,
        finding_ids=finding_ids,
        fix_task_id=fix_task_id,
        fix_head_sha=fix_head_sha,
        follow_up_to=follow_up_to,
    )


def claim_task(
    context: GitContext,
    task_id: str,
    agent: str,
    branch: str,
    touches: Sequence[str] = (),
    force: bool = False,
) -> dict:
    task_id = _task_id(task_id)
    agent = _text(agent, "agent")
    branch = _text(branch, "branch")
    normalized_touches = _normalize_touches(context, touches)
    timestamp = _now()
    current_time = _parse_time(timestamp)
    with Board(context).write() as connection:
        row = require_task(connection, task_id)
        if row["task_type"] in READ_ONLY_REVIEW_TYPES and normalized_touches:
            raise LightboardError(f"{row['task_type']} tasks are read-only and cannot declare touches")
        current_status = row["status"]
        old_owner = row["owner"]
        if force:
            if current_status not in ACTIVE_STATUSES:
                raise LightboardError("force reclaim is allowed only for a stale active task")
            task = task_from_row(row)
            if not _task_is_stale(task, current_time):
                raise LightboardError(f"task {task_id} is active but not stale")
            connection.execute(
                """
                UPDATE tasks
                SET owner = ?, branch = ?, status = 'CLAIMED', touches = ?, block_reason = NULL,
                    started_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    agent,
                    branch,
                    json.dumps(normalized_touches, separators=(",", ":")),
                    timestamp,
                    timestamp,
                    task_id,
                    current_status,
                ),
            )
            record_event(
                connection,
                task_id,
                "RECLAIM",
                {
                    "old_owner": old_owner,
                    "new_owner": agent,
                    "old_status": current_status,
                    "new_status": "CLAIMED",
                    "old_branch": row["branch"],
                    "new_branch": branch,
                    "touches": normalized_touches,
                },
                timestamp,
            )
            return _mutation(
                f"reclaimed {task_id} by {agent}",
                task_id,
                owner=agent,
                status="CLAIMED",
                touches=normalized_touches,
                reclaimed_from=old_owner,
            )

        updated = connection.execute(
            """
            UPDATE tasks
            SET owner = ?, branch = ?, status = 'CLAIMED', touches = ?, started_at = ?, updated_at = ?
            WHERE id = ? AND status = 'TODO'
            """,
            (
                agent,
                branch,
                json.dumps(normalized_touches, separators=(",", ":")),
                timestamp,
                timestamp,
                task_id,
            ),
        ).rowcount
        if updated != 1:
            raise LightboardError(f"cannot claim task {task_id}: already claimed or unavailable")
        record_event(
            connection,
            task_id,
            "CLAIM",
            {"agent": agent, "branch": branch, "touches": normalized_touches, "new_status": "CLAIMED"},
            timestamp,
        )
    return _mutation(f"claimed {task_id} by {agent}", task_id, owner=agent, status="CLAIMED", touches=normalized_touches)


def set_status(context: GitContext, task_id: str, status: str) -> dict:
    task_id = _task_id(task_id)
    status = _text(status, "status").upper()
    if status not in MANUAL_STATUSES:
        raise LightboardError(f"invalid manual status {status}; expected one of {', '.join(MANUAL_STATUSES)}")
    timestamp = _now()
    with Board(context).write() as connection:
        row = require_task(connection, task_id)
        current = row["status"]
        if current == "DONE":
            raise LightboardError(f"task {task_id} is DONE and terminal")
        if status != "IN_PROGRESS" or current not in ("CLAIMED", "BLOCKED"):
            raise LightboardError(
                f"invalid transition {current} -> {status}; use block/review for their required finding"
            )
        connection.execute(
            "UPDATE tasks SET status = ?, block_reason = NULL, updated_at = ? WHERE id = ?",
            (status, timestamp, task_id),
        )
        record_event(
            connection,
            task_id,
            "STATUS",
            {"actor": row["owner"], "from_status": current, "to_status": status},
            timestamp,
        )
    return _mutation(f"{task_id}: {status}", task_id, status=status, from_status=current)


def _ensure_mutable(row: sqlite3.Row, task_id: str) -> None:
    if row["status"] == "DONE":
        raise LightboardError(f"task {task_id} is DONE and terminal")


def add_entry(context: GitContext, task_id: str, kind: str, body: str) -> dict:
    task_id = _task_id(task_id)
    body = _text(body, "entry text")
    if kind not in ENTRY_KINDS:
        raise LightboardError(f"invalid entry kind: {kind}")
    with Board(context).write() as connection:
        row = require_task(connection, task_id)
        _ensure_mutable(row, task_id)
        timestamp = _now()
        cursor = connection.execute(
            "INSERT INTO entries (task_id, kind, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, kind, body, timestamp),
        )
        connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp, task_id))
        if kind == "CONTRACT":
            record_event(
                connection,
                task_id,
                "CONTRACT",
                {"agent": row["owner"], "contract_id": cursor.lastrowid, "body": body},
                timestamp,
            )
    return _mutation(f"recorded {kind} for {task_id}", task_id, entry_id=cursor.lastrowid, kind=kind)


def block_task(context: GitContext, task_id: str, body: str) -> dict:
    task_id = _task_id(task_id)
    body = _text(body, "blocker reason")
    timestamp = _now()
    with Board(context).write() as connection:
        row = require_task(connection, task_id)
        _ensure_mutable(row, task_id)
        if row["status"] not in ("IN_PROGRESS", "BLOCKED"):
            raise LightboardError(f"invalid transition {row['status']} -> BLOCKED")
        cursor = connection.execute(
            "INSERT INTO entries (task_id, kind, body, created_at) VALUES (?, 'BLOCKER', ?, ?)",
            (task_id, body, timestamp),
        )
        connection.execute(
            "UPDATE tasks SET status = 'BLOCKED', block_reason = ?, updated_at = ? WHERE id = ?",
            (body, timestamp, task_id),
        )
        record_event(
            connection,
            task_id,
            "BLOCK",
            {
                "actor": row["owner"],
                "from_status": row["status"],
                "to_status": "BLOCKED",
                "blocker_id": cursor.lastrowid,
                "reason": body,
            },
            timestamp,
        )
    return _mutation(f"blocked {task_id}", task_id, status="BLOCKED", blocker_id=cursor.lastrowid, block_reason=body)


def review_task(context: GitContext, task_id: str, body: str) -> dict:
    task_id = _task_id(task_id)
    body = _text(body, "review finding")
    timestamp = _now()
    with Board(context).write() as connection:
        row = require_task(connection, task_id)
        _ensure_mutable(row, task_id)
        if row["task_type"] != "FEATURE":
            raise LightboardError("legacy REVIEW status is only available for FEATURE tasks")
        current = row["status"]
        if current == "IN_PROGRESS":
            next_status = "REVIEW"
        elif current == "REVIEW":
            next_status = "IN_PROGRESS"
        else:
            raise LightboardError(f"invalid transition {current} -> REVIEW")
        cursor = connection.execute(
            "INSERT INTO entries (task_id, kind, body, created_at) VALUES (?, 'REVIEW_FINDING', ?, ?)",
            (task_id, body, timestamp),
        )
        connection.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (next_status, timestamp, task_id),
        )
        record_event(
            connection,
            task_id,
            "REVIEW",
            {
                "agent": row["owner"],
                "from_status": current,
                "to_status": next_status,
                "finding_id": cursor.lastrowid,
                "finding": body,
            },
            timestamp,
        )
    return _mutation(
        f"{task_id}: {next_status}",
        task_id,
        status=next_status,
        finding_id=cursor.lastrowid,
        owner=row["owner"],
    )


def done_task(
    context: GitContext,
    task_id: str,
    head_sha: str,
    summary: str,
    verified: Optional[Sequence[str]] = None,
    unverified: Optional[Sequence[str]] = None,
    not_reviewed: Optional[Sequence[str]] = None,
    auto_fix: bool = False,
) -> dict:
    task_id = _task_id(task_id)
    head_sha = _text(head_sha, "SHA")
    summary = _text(summary, "summary")
    audit_verified: List[str] = []
    audit_unverified: List[str] = []
    audit_not_reviewed: List[str] = []
    with Board(context).read() as connection:
        row = require_task(connection, task_id)
        if row["status"] == "DONE":
            raise LightboardError(f"task {task_id} is DONE and terminal")
        if row["status"] not in ("IN_PROGRESS", "REVIEW"):
            raise LightboardError(f"invalid transition {row['status']} -> DONE")
        if row["task_type"] == "AUDIT":
            audit_verified, audit_unverified, audit_not_reviewed = _audit_result(
                verified, unverified, not_reviewed
            )
        elif any(value for value in (verified, unverified, not_reviewed)):
            raise LightboardError(f"{row['task_type']} tasks cannot record audit scope results")
        lineage_ok, reason = _verify_commit_lineage(context, row["base_sha"], head_sha)
        if not lineage_ok:
            raise LightboardError(f"cannot complete {task_id}: {reason}")
        if row["task_type"] in READ_ONLY_REVIEW_TYPES:
            violations = _read_only_violation(context, row["base_sha"], head_sha)
            if violations:
                paths = ", ".join(violations)
                raise LightboardError(f"cannot complete {task_id}: read-only violation in production files: {paths}")

    with Board(context).write() as connection:
        row = require_task(connection, task_id)
        if row["status"] == "DONE":
            raise LightboardError(f"task {task_id} is DONE and terminal")
        if row["status"] not in ("IN_PROGRESS", "REVIEW"):
            raise LightboardError(f"invalid transition {row['status']} -> DONE")
        lineage_ok, reason = _verify_commit_lineage(context, row["base_sha"], head_sha)
        if not lineage_ok:
            raise LightboardError(f"cannot complete {task_id}: {reason}")
        if row["task_type"] in READ_ONLY_REVIEW_TYPES:
            violations = _read_only_violation(context, row["base_sha"], head_sha)
            if violations:
                paths = ", ".join(violations)
                raise LightboardError(f"cannot complete {task_id}: read-only violation in production files: {paths}")
        contracts = connection.execute(
            "SELECT id FROM entries WHERE task_id = ? AND kind = 'CONTRACT' ORDER BY id", (task_id,)
        ).fetchall()
        verified_contract_ids = [entry["id"] for entry in contracts]
        timestamp = _now()
        connection.execute(
            """
            UPDATE tasks
            SET head_sha = ?, summary = ?, status = 'DONE', completed_at = ?, updated_at = ?,
                verified_contract_ids = ?, audit_verified = ?, audit_unverified = ?, audit_not_reviewed = ?
            WHERE id = ?
            """,
            (
                head_sha,
                summary,
                timestamp,
                timestamp,
                json.dumps(verified_contract_ids, separators=(",", ":")),
                json.dumps(audit_verified, separators=(",", ":")),
                json.dumps(audit_unverified, separators=(",", ":")),
                json.dumps(audit_not_reviewed, separators=(",", ":")),
                task_id,
            ),
        )
        fixed_finding_ids: List[int] = []
        if row["task_type"] == "FIX" and auto_fix:
            linked_findings = connection.execute(
                """
                SELECT findings.* FROM findings
                JOIN task_findings ON task_findings.finding_id = findings.id
                WHERE task_findings.task_id = ? AND task_findings.relation = 'FIX'
                ORDER BY findings.id
                """,
                (task_id,),
            ).fetchall()
            for finding in linked_findings:
                if finding["status"] not in ("OPEN", "REOPENED"):
                    raise LightboardError(
                        f"finding {finding['id']} is {finding['status']}, not fixable by {task_id}"
                    )
                connection.execute(
                    """
                    UPDATE findings
                    SET status = 'FIXED', resolved_by_task_id = ?, verified_by_task_id = NULL,
                        verification_reason = NULL
                    WHERE id = ? AND status IN ('OPEN', 'REOPENED')
                    """,
                    (task_id, finding["id"]),
                )
                fixed_finding_ids.append(finding["id"])
                record_event(
                    connection,
                    task_id,
                    "FINDING_FIXED",
                    {"finding_id": finding["id"], "fix_task_id": task_id, "fix_head_sha": head_sha},
                    timestamp,
                )
        record_event(
            connection,
            task_id,
            "DONE",
            {
                "head_sha": head_sha,
                "summary": summary,
                "verified_contract_ids": verified_contract_ids,
                "from_status": row["status"],
                "to_status": "DONE",
            },
            timestamp,
        )
        if row["task_type"] == "AUDIT":
            record_event(
                connection,
                task_id,
                "AUDIT_DONE",
                {
                    "target_task_id": row["target_task_id"],
                    "target_base_sha": row["target_base_sha"],
                    "target_head_sha": row["target_head_sha"],
                    "verified": audit_verified,
                    "unverified": audit_unverified,
                    "not_reviewed": audit_not_reviewed,
                },
                timestamp,
            )
        elif row["task_type"] == "FIX":
            record_event(
                connection,
                task_id,
                "FIX_DONE",
                {"fix_head_sha": head_sha, "finding_ids": fixed_finding_ids},
                timestamp,
            )
        elif row["task_type"] == "VERIFY":
            record_event(
                connection,
                task_id,
                "VERIFY_DONE",
                {"fix_task_id": row["fix_task_id"], "fix_head_sha": row["fix_head_sha"]},
                timestamp,
            )
    contract_text = " ".join(f"#{contract_id}" for contract_id in verified_contract_ids) or "—"
    return _mutation(
        f"done {task_id} at {head_sha}; verified contracts {contract_text}",
        task_id,
        status="DONE",
        head_sha=head_sha,
        summary=summary,
        verified_contract_ids=verified_contract_ids,
        audit_verified=audit_verified,
        audit_unverified=audit_unverified,
        audit_not_reviewed=audit_not_reviewed,
        fixed_finding_ids=fixed_finding_ids,
    )
