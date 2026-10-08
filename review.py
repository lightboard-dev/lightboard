"""Review Mode invariants, finding transitions, and gate decisions."""

from __future__ import annotations

import copy
import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from db import Board
from git import _current_task_head, _verify_commit_lineage
from models import (
    LENSES,
    REVIEW_TASK_TYPES,
    SCHEMA_VERSION,
    SEVERITIES,
    GitContext,
    LightboardError,
    _enum,
    _finding_id,
    _finding_ids,
    _mutation,
    _now,
    _optional_text,
    _sha,
    _task_id,
    _text,
)
from repository import (
    get_finding_record,
    infer_verify_task,
    list_finding_records,
    record_event,
    require_finding,
    require_task,
    require_task_finding_relation,
)


def _target_review_values(context: GitContext, target_task_id: str) -> Tuple[str, str, str]:
    target_task_id = _task_id(target_task_id)
    with Board(context).read() as connection:
        target = require_task(connection, target_task_id)
        if target["task_type"] != "FEATURE":
            raise LightboardError(f"review target must be a FEATURE task: {target_task_id}")
        target_base_sha = target["base_sha"]
        target_head_sha = _current_task_head(context, target)
    verified, reason = _verify_commit_lineage(context, target_base_sha, target_head_sha or "")
    if not verified:
        raise LightboardError(f"target {target_task_id} has no valid commit for review: {reason}")
    if target_base_sha is None or target_head_sha is None:
        raise LightboardError(f"target {target_task_id} has no valid commit for review")
    return target_task_id, target_base_sha, target_head_sha


def _validate_target_binding(
    context: GitContext,
    connection: sqlite3.Connection,
    task_id: str,
    task_type: str,
    target_task_id: Optional[str],
    target_base_sha: Optional[str],
    target_head_sha: Optional[str],
) -> Optional[sqlite3.Row]:
    if task_type not in REVIEW_TASK_TYPES:
        if any(value is not None for value in (target_task_id, target_base_sha, target_head_sha)):
            raise LightboardError(f"{task_type} tasks cannot have a review target")
        return None
    if target_task_id is None or target_base_sha is None or target_head_sha is None:
        raise LightboardError(f"{task_type} tasks require target_task_id, target_base_sha, and target_head_sha")
    if target_task_id == task_id:
        raise LightboardError("a review task cannot target itself")
    target = require_task(connection, target_task_id)
    if target["task_type"] != "FEATURE":
        raise LightboardError(f"review target must be a FEATURE task: {target_task_id}")
    if target["base_sha"] != target_base_sha:
        raise LightboardError("target_base_sha does not match target task base_sha")
    current_target_head = _current_task_head(context, target)
    if current_target_head != target_head_sha:
        raise LightboardError("target_head_sha does not match the current target HEAD")
    verified, reason = _verify_commit_lineage(context, target_base_sha, target_head_sha)
    if not verified:
        raise LightboardError(f"invalid target commit binding: {reason}")
    return target


def _validate_related_findings(
    connection: sqlite3.Connection,
    task_type: str,
    target_task_id: str,
    target_base_sha: str,
    target_head_sha: str,
    finding_ids: Sequence[int],
    fix_task_id: Optional[str] = None,
) -> None:
    for finding_id in finding_ids:
        finding = require_finding(connection, finding_id)
        audit = require_task(connection, finding["audit_task_id"])
        if audit["task_type"] != "AUDIT":
            raise LightboardError(f"finding {finding_id} does not belong to an AUDIT task")
        if finding["target_task_id"] != target_task_id:
            raise LightboardError(f"finding {finding_id} targets a different task")
        if finding["target_base_sha"] not in (None, "") and finding["target_base_sha"] != target_base_sha:
            raise LightboardError(f"finding {finding_id} belongs to a different target base SHA")
        if finding["target_head_sha"] not in (None, "") and finding["target_head_sha"] != target_head_sha:
            raise LightboardError(f"finding {finding_id} belongs to a different target commit")
        if audit["target_base_sha"] != target_base_sha or audit["target_head_sha"] != target_head_sha:
            raise LightboardError(f"finding {finding_id} belongs to a different target commit")
        if task_type == "FIX":
            if finding["status"] not in ("OPEN", "REOPENED"):
                raise LightboardError(f"finding {finding_id} is {finding['status']}, not fixable")
        elif task_type == "VERIFY":
            if finding["status"] != "FIXED":
                raise LightboardError(f"finding {finding_id} is {finding['status']}, not ready for verification")
            if finding["resolved_by_task_id"] != fix_task_id:
                raise LightboardError(f"finding {finding_id} was fixed by a different task")
            linked = connection.execute(
                "SELECT 1 FROM task_findings WHERE task_id = ? AND finding_id = ? AND relation = 'FIX'",
                (fix_task_id, finding_id),
            ).fetchone()
            if linked is None:
                raise LightboardError(f"finding {finding_id} is not linked to FIX {fix_task_id}")


def _validate_task_definition(
    context: GitContext,
    connection: sqlite3.Connection,
    task_id: str,
    task_type: str,
    target_task_id: Optional[str],
    target_base_sha: Optional[str],
    target_head_sha: Optional[str],
    lens: Optional[str],
    review_group_id: Optional[str],
    review_slot: Optional[str],
    scope_paths: Sequence[str],
    finding_ids: Sequence[int],
    fix_task_id: Optional[str],
    fix_head_sha: Optional[str],
) -> dict:
    target = _validate_target_binding(
        context,
        connection,
        task_id,
        task_type,
        target_task_id,
        target_base_sha,
        target_head_sha,
    )
    if task_type == "AUDIT":
        if lens is None:
            raise LightboardError("AUDIT tasks require lens")
        if (review_group_id is None) != (review_slot is None):
            raise LightboardError("review_group_id and review_slot must be provided together")
    elif scope_paths:
        raise LightboardError(f"{task_type} tasks cannot have audit scope")
    elif lens is not None:
        raise LightboardError(f"{task_type} tasks cannot have an audit lens")
    if task_type != "AUDIT" and (review_group_id is not None or review_slot is not None):
        raise LightboardError(f"{task_type} tasks cannot have a review group")
    if task_type == "VERIFY":
        if fix_task_id is None or fix_head_sha is None:
            raise LightboardError("VERIFY tasks require fix_task_id and fix_head_sha")
        fix = require_task(connection, fix_task_id)
        if fix["task_type"] != "FIX":
            raise LightboardError("VERIFY fix_task_id must reference a FIX task")
        if fix["status"] != "DONE" or fix["head_sha"] != fix_head_sha:
            raise LightboardError("VERIFY fix_head_sha must match a completed FIX task head_sha")
        if (
            fix["target_task_id"] != target_task_id
            or fix["target_base_sha"] != target_base_sha
            or fix["target_head_sha"] != target_head_sha
        ):
            raise LightboardError("VERIFY fix relation targets a different commit")
    elif fix_task_id is not None or fix_head_sha is not None:
        raise LightboardError(f"{task_type} tasks cannot have a fix relation")
    if task_type in ("FIX", "VERIFY"):
        if not finding_ids:
            raise LightboardError(f"{task_type} tasks require at least one finding")
        _validate_related_findings(
            connection,
            task_type,
            target_task_id,
            target_base_sha,
            target_head_sha,
            finding_ids,
            fix_task_id,
        )
    elif finding_ids:
        raise LightboardError(f"{task_type} tasks cannot have finding relations")
    return {"target": target}


def create_finding(
    context: GitContext,
    audit_task_id: str,
    reviewer: str,
    severity: str,
    lens: str,
    claim: str,
    evidence: str,
    scenario: str,
    required_property: str,
) -> dict:
    audit_task_id = _task_id(audit_task_id)
    reviewer = _text(reviewer, "reviewer")
    severity = _enum(severity, "severity", SEVERITIES)
    lens = _enum(lens, "lens", LENSES)
    claim = _text(claim, "claim")
    evidence = _text(evidence, "evidence")
    scenario = _text(scenario, "scenario")
    required_property = _text(required_property, "required property")
    timestamp = _now()
    with Board(context).write() as connection:
        audit = require_task(connection, audit_task_id)
        if audit["task_type"] != "AUDIT":
            raise LightboardError("findings can only belong to AUDIT tasks")
        if not audit["target_task_id"] or not audit["target_head_sha"]:
            raise LightboardError("AUDIT has no valid target binding")
        cursor = connection.execute(
            """
            INSERT INTO findings (
                audit_task_id, target_task_id, target_base_sha, target_head_sha, reviewer, severity, lens, status,
                claim, evidence, scenario, required_property, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
            """,
            (
                audit_task_id,
                audit["target_task_id"],
                audit["target_base_sha"],
                audit["target_head_sha"],
                reviewer,
                severity,
                lens,
                claim,
                evidence,
                scenario,
                required_property,
                timestamp,
            ),
        )
        finding_id = cursor.lastrowid
        connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp, audit_task_id))
        record_event(
            connection,
            audit_task_id,
            "FINDING_CREATED",
            {
                "finding_id": finding_id,
                "target_task_id": audit["target_task_id"],
                "target_base_sha": audit["target_base_sha"],
                "target_head_sha": audit["target_head_sha"],
                "reviewer": reviewer,
                "severity": severity,
                "lens": lens,
            },
            timestamp,
        )
    return _mutation(
        f"created finding #{finding_id} for {audit_task_id}",
        audit_task_id,
        id=finding_id,
        finding_id=finding_id,
        status="OPEN",
        target_task_id=audit["target_task_id"],
        target_base_sha=audit["target_base_sha"],
        target_head_sha=audit["target_head_sha"],
    )


add_finding = create_finding


def fix_finding(context: GitContext, fix_task_id: str, finding_id: Any) -> dict:
    fix_task_id = _task_id(fix_task_id)
    finding_id = _finding_id(finding_id)
    with Board(context).write() as connection:
        fix = require_task(connection, fix_task_id)
        if fix["task_type"] != "FIX":
            raise LightboardError("only FIX tasks can mark findings FIXED")
        if fix["status"] != "DONE" or not fix["head_sha"]:
            raise LightboardError("a FIX must be DONE before marking findings FIXED")
        require_task_finding_relation(connection, fix_task_id, finding_id, "FIX")
        finding = require_finding(connection, finding_id)
        if finding["target_base_sha"] not in (None, "") and finding["target_base_sha"] != fix["target_base_sha"]:
            raise LightboardError(f"finding {finding_id} belongs to a different target base SHA")
        if finding["target_head_sha"] not in (None, "") and finding["target_head_sha"] != fix["target_head_sha"]:
            raise LightboardError(f"finding {finding_id} belongs to a different target commit")
        if finding["status"] not in ("OPEN", "REOPENED"):
            raise LightboardError(f"finding {finding_id} is {finding['status']}, not fixable")
        if finding["target_task_id"] != fix["target_task_id"]:
            raise LightboardError(f"finding {finding_id} targets a different task")
        timestamp = _now()
        updated = connection.execute(
            """
            UPDATE findings
            SET status = 'FIXED', resolved_by_task_id = ?, verified_by_task_id = NULL, verification_reason = NULL
            WHERE id = ? AND status IN ('OPEN', 'REOPENED')
            """,
            (fix_task_id, finding_id),
        ).rowcount
        if updated != 1:
            raise LightboardError(f"finding {finding_id} changed before it could be fixed")
        connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp, fix_task_id))
        record_event(
            connection,
            fix_task_id,
            "FINDING_FIXED",
            {"finding_id": finding_id, "fix_task_id": fix_task_id, "fix_head_sha": fix["head_sha"]},
            timestamp,
        )
    return _mutation(
        f"fixed finding #{finding_id} with {fix_task_id}",
        fix_task_id,
        finding_id=finding_id,
        status="FIXED",
    )


def verify_finding(
    context: GitContext,
    verify_task_id: str,
    finding_id: Any,
    outcome: Any,
    reason: Optional[str] = None,
) -> dict:
    verify_task_id = _task_id(verify_task_id)
    finding_id = _finding_id(finding_id)
    if isinstance(outcome, bool):
        outcome = "VERIFIED" if outcome else "REOPENED"
    outcome = _enum(outcome, "verification outcome", ("VERIFIED", "REOPENED"))
    reason = None if reason is None else _text(reason, "verification reason")
    if outcome == "REOPENED" and reason is None:
        reason = "verification reopened finding"
    with Board(context).write() as connection:
        verify = require_task(connection, verify_task_id)
        if verify["task_type"] != "VERIFY":
            raise LightboardError("only VERIFY tasks can verify findings")
        if verify["status"] != "DONE" or not verify["head_sha"]:
            raise LightboardError("a VERIFY must be DONE before changing finding status")
        require_task_finding_relation(connection, verify_task_id, finding_id, "VERIFY")
        finding = require_finding(connection, finding_id)
        if finding["status"] != "FIXED":
            raise LightboardError(f"finding {finding_id} is {finding['status']}, not ready for verification")
        if finding["resolved_by_task_id"] != verify["fix_task_id"]:
            raise LightboardError(f"finding {finding_id} was fixed by a different task")
        if finding["target_base_sha"] not in (None, "") and finding["target_base_sha"] != verify["target_base_sha"]:
            raise LightboardError(f"finding {finding_id} belongs to a different target base SHA")
        if finding["target_head_sha"] not in (None, "") and finding["target_head_sha"] != verify["target_head_sha"]:
            raise LightboardError(f"finding {finding_id} belongs to a different target commit")
        timestamp = _now()
        updated = connection.execute(
            "UPDATE findings SET status = ?, verified_by_task_id = ?, verification_reason = ? WHERE id = ? AND status = 'FIXED'",
            (outcome, verify_task_id, reason, finding_id),
        ).rowcount
        if updated != 1:
            raise LightboardError(f"finding {finding_id} changed before verification")
        connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp, verify_task_id))
        record_event(
            connection,
            verify_task_id,
            f"FINDING_{outcome}",
            {
                "finding_id": finding_id,
                "verify_task_id": verify_task_id,
                "fix_task_id": verify["fix_task_id"],
                "fix_head_sha": verify["fix_head_sha"],
                "outcome": outcome,
                "reason": reason,
            },
            timestamp,
        )
    return _mutation(
        f"{outcome.lower()} finding #{finding_id} with {verify_task_id}",
        verify_task_id,
        finding_id=finding_id,
        status=outcome,
        reason=reason,
    )


def get_finding(context: GitContext, finding_id: Any) -> dict:
    finding_id = _finding_id(finding_id)
    return {"schema_version": SCHEMA_VERSION, **get_finding_record(context, finding_id)}


def list_findings(
    context: GitContext,
    target_task_id: Optional[str] = None,
    audit_task_id: Optional[str] = None,
) -> List[dict]:
    return list_finding_records(
        context,
        _optional_text(target_task_id, "target task id"),
        _optional_text(audit_task_id, "audit task id"),
    )


def _infer_verify_task(context: GitContext, finding_id: int) -> str:
    return infer_verify_task(context, finding_id)


def reopen_finding(
    context: GitContext,
    finding_id: Any,
    reason: str,
    verify_task_id: Optional[str] = None,
) -> dict:
    finding_id = _finding_id(finding_id)
    reason = _text(reason, "reopen reason")
    if verify_task_id is None:
        verify_task_id = _infer_verify_task(context, finding_id)
    return verify_finding(context, verify_task_id, finding_id, "REOPENED", reason)


def _text_values(values: Optional[Sequence[str]], label: str) -> List[str]:
    return [_text(value, label) for value in (values or ())]


def _audit_result(
    verified: Optional[Sequence[str]],
    unverified: Optional[Sequence[str]],
    not_reviewed: Optional[Sequence[str]],
) -> Tuple[List[str], List[str], List[str]]:
    verified_values = _text_values(verified, "verified scope")
    unverified_values = _text_values(unverified, "unverified scope")
    not_reviewed_values = _text_values(not_reviewed, "not reviewed scope")
    if not (verified_values or unverified_values or not_reviewed_values):
        not_reviewed_values = ["scope result not provided"]
    return verified_values, unverified_values, not_reviewed_values


def _review_task_summary(task: dict) -> dict:
    return {
        "id": task["id"],
        "title": task["title"],
        "task_type": task["task_type"],
        "status": task["status"],
        "owner": task.get("owner"),
        "target_task_id": task.get("target_task_id"),
        "target_base_sha": task.get("target_base_sha"),
        "target_head_sha": task.get("target_head_sha"),
        "current_target_head_sha": task.get("current_target_head_sha"),
        "review_stale": task.get("review_stale", False),
        "head_sha": task.get("head_sha"),
        "lens": task.get("lens"),
        "scope_paths": copy.deepcopy(task.get("scope_paths", [])),
        "audit_result": copy.deepcopy(task.get("audit_result")),
        "finding_ids": copy.deepcopy(task.get("finding_ids", [])),
        "fix_task_id": task.get("fix_task_id"),
        "fix_head_sha": task.get("fix_head_sha"),
    }


def _review_aggregate(context: GitContext, target: dict, tasks: Sequence[dict], findings: Sequence[dict]) -> dict:
    target_id = target["id"]
    current_target_head = _current_task_head(context, target)
    review_tasks = [task for task in tasks if task.get("target_task_id") == target_id]
    audits = [task for task in review_tasks if task["task_type"] == "AUDIT"]
    fixes = [task for task in review_tasks if task["task_type"] == "FIX"]
    verifications = [task for task in review_tasks if task["task_type"] == "VERIFY"]
    target_findings = [
        copy.deepcopy(finding) for finding in findings if finding.get("target_task_id") == target_id
    ]
    stale_task_ids = [
        task["id"]
        for task in review_tasks
        if task.get("target_head_sha") != current_target_head
    ]
    open_findings = [
        finding for finding in target_findings if finding["status"] in ("OPEN", "REOPENED")
    ]
    incomplete_scope = any(
        not audit.get("audit_verified")
        or audit.get("audit_unverified")
        or audit.get("audit_not_reviewed")
        for audit in audits
    )
    if not audits:
        state = "NONE"
    elif stale_task_ids:
        state = "STALE"
    elif any(audit["status"] != "DONE" for audit in audits):
        state = "IN_PROGRESS"
    elif open_findings:
        state = "FINDINGS"
    elif incomplete_scope or any(finding["status"] != "VERIFIED" for finding in target_findings):
        state = "PARTIAL"
    else:
        state = "CLEAN"

    review_events: List[dict] = []
    for task in [target, *review_tasks]:
        review_events.extend(copy.deepcopy(task.get("events", [])))
    review_events.sort(key=lambda event: event["id"])
    gate_events = [
        event
        for event in target.get("events", [])
        if event["type"] in ("REVIEW_GATE_PASS", "REVIEW_GATE_FAIL")
    ]
    gate = None
    if gate_events:
        event = gate_events[-1]
        payload = event["payload"]
        gate = {
            "ok": event["type"] == "REVIEW_GATE_PASS",
            "event_id": event["id"],
            "target_head_sha": payload.get("target_head_sha"),
            "current": payload.get("target_head_sha") == current_target_head
            and event["id"] == (review_events[-1]["id"] if review_events else event["id"]),
            "reasons": copy.deepcopy(payload.get("reasons", [])),
        }
        gate["stale"] = not gate["current"]

    return {
        "state": state,
        "target_task_id": target_id,
        "target_head_sha": current_target_head,
        "audits": [_review_task_summary(task) for task in audits],
        "findings": target_findings,
        "fixes": [_review_task_summary(task) for task in fixes],
        "verifications": [_review_task_summary(task) for task in verifications],
        "stale_task_ids": stale_task_ids,
        "gate": gate,
        "events": review_events,
    }


def review_state(context: GitContext, task_id: str) -> dict:
    from snapshot import get_task

    task = get_task(context, task_id)
    if task["task_type"] != "FEATURE":
        raise LightboardError(f"review state requires a FEATURE task: {task_id}")
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "state": task["review_state"],
        "target_head_sha": task["review_mode"]["target_head_sha"],
        "review_mode": copy.deepcopy(task["review_mode"]),
    }


def _review_gate_result(context: GitContext, task_id: str, snapshot: Optional[dict] = None) -> dict:
    if snapshot is None:
        from snapshot import build_snapshot

        snapshot = build_snapshot(context)
    target = next((task for task in snapshot["tasks"] if task["id"] == task_id), None)
    if target is None:
        raise LightboardError(f"task not found: {task_id}")
    if target["task_type"] != "FEATURE":
        raise LightboardError(f"review gate requires a FEATURE task: {task_id}")

    review = target["review_mode"]
    current_target_head = review["target_head_sha"]
    audits = review["audits"]
    findings = review["findings"]
    fixes = {fix["id"]: fix for fix in review["fixes"]}
    verifications = {verify["id"]: verify for verify in review["verifications"]}
    reasons: List[str] = []

    def add_reason(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if target["status"] != "DONE":
        add_reason(f"implementation {task_id} is {target['status']}, not DONE")
    if not current_target_head:
        add_reason("target has no current HEAD")
    else:
        from git import _git_commit_status

        head_exists, head_reason = _git_commit_status(context, current_target_head)
        if not head_exists:
            add_reason(f"target HEAD is invalid: {head_reason}")
        if target.get("base_sha"):
            lineage_ok, lineage_reason = _verify_commit_lineage(
                context, target["base_sha"], current_target_head
            )
            if not lineage_ok:
                add_reason(f"target HEAD lineage is invalid: {lineage_reason}")
    if target.get("head_sha") != current_target_head:
        add_reason(
            f"review is STALE: target HEAD changed from {target.get('head_sha')} to {current_target_head}"
        )
    if not audits:
        add_reason("no AUDIT is linked to the target")

    stale_task_ids: List[str] = []
    for audit in audits:
        if audit["status"] != "DONE":
            add_reason(f"AUDIT {audit['id']} is {audit['status']}, not DONE")
        if audit.get("target_head_sha") != current_target_head:
            stale_task_ids.append(audit["id"])
            add_reason(f"AUDIT {audit['id']} is stale for target HEAD {current_target_head}")
        if not audit.get("audit_result", {}).get("verified"):
            add_reason(f"AUDIT {audit['id']} has no VERIFIED scope result")
        if audit.get("audit_result", {}).get("unverified"):
            add_reason(
                f"AUDIT {audit['id']} has UNVERIFIED scope: "
                + ", ".join(audit["audit_result"]["unverified"])
            )
        if audit.get("audit_result", {}).get("not_reviewed"):
            add_reason(
                f"AUDIT {audit['id']} has NOT_REVIEWED scope: "
                + ", ".join(audit["audit_result"]["not_reviewed"])
            )

    for finding in findings:
        finding_id = finding["id"]
        if finding.get("target_head_sha") not in (None, "", current_target_head):
            add_reason(f"finding #{finding_id} belongs to a different target HEAD")
        status = finding["status"]
        if status in ("OPEN", "REOPENED"):
            add_reason(f"finding #{finding_id} is {status}; blocking finding remains")
        elif status == "FIXED":
            add_reason(f"finding #{finding_id} is FIXED but not VERIFIED")
        elif status in ("REJECTED", "DEFERRED"):
            add_reason(f"finding #{finding_id} is {status}, not VERIFIED")
        elif status == "VERIFIED":
            verify_id = finding.get("verified_by_task_id")
            verify = verifications.get(verify_id)
            fix_id = finding.get("resolved_by_task_id")
            fix = fixes.get(fix_id)
            if verify is None or verify["status"] != "DONE":
                add_reason(f"finding #{finding_id} has no completed VERIFY")
            if fix is None or fix["status"] != "DONE":
                add_reason(f"finding #{finding_id} has no completed FIX")
            if verify is not None and fix is not None:
                if verify.get("fix_task_id") != fix_id or verify.get("fix_head_sha") != fix.get("head_sha"):
                    add_reason(f"finding #{finding_id} verification is not bound to its FIX SHA")
                if verify.get("target_head_sha") != finding.get("target_head_sha"):
                    add_reason(f"finding #{finding_id} verification targets a different commit")
                if finding_id not in verify.get("finding_ids", []):
                    add_reason(f"finding #{finding_id} is not linked to its VERIFY task")
        else:
            add_reason(f"finding #{finding_id} has unsupported status {status}")

    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "ok": not reasons,
        "state": review["state"],
        "target_task_id": task_id,
        "target_head_sha": current_target_head,
        "reasons": reasons,
        "audits": copy.deepcopy(audits),
        "findings": copy.deepcopy(findings),
        "fixes": copy.deepcopy(list(fixes.values())),
        "verifications": copy.deepcopy(list(verifications.values())),
        "stale_task_ids": stale_task_ids,
    }


def _has_event_for_target_head(
    connection: sqlite3.Connection,
    task_id: str,
    event_type: str,
    target_head_sha: Optional[str],
) -> bool:
    for row in connection.execute(
        "SELECT payload FROM events WHERE task_id = ? AND type = ? ORDER BY id DESC",
        (task_id, event_type),
    ):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("target_head_sha") == target_head_sha:
            return True
    return False


def review_gate(context: GitContext, task_id: str) -> dict:
    task_id = _task_id(task_id)
    result = _review_gate_result(context, task_id)
    timestamp = _now()
    with Board(context).write() as connection:
        for stale_task_id in result["stale_task_ids"]:
            if not _has_event_for_target_head(connection, stale_task_id, "REVIEW_STALE", result["target_head_sha"]):
                record_event(
                    connection,
                    stale_task_id,
                    "REVIEW_STALE",
                    {
                        "target_task_id": task_id,
                        "target_head_sha": result["target_head_sha"],
                    },
                    timestamp,
                )
        event_type = "REVIEW_GATE_PASS" if result["ok"] else "REVIEW_GATE_FAIL"
        record_event(
            connection,
            task_id,
            event_type,
            {
                "target_task_id": task_id,
                "target_head_sha": result["target_head_sha"],
                "state": result["state"],
                "reasons": result["reasons"],
                "audit_ids": [audit["id"] for audit in result["audits"]],
                "finding_ids": [finding["id"] for finding in result["findings"]],
            },
            timestamp,
        )
    return result


def review_staleness(
    context: GitContext,
    review_task_id: str,
    current_target_head_sha: Optional[str] = None,
) -> bool:
    from snapshot import get_task

    task = get_task(context, review_task_id)
    if task["task_type"] not in REVIEW_TASK_TYPES:
        raise LightboardError(f"task {review_task_id} is not a review task")
    current = current_target_head_sha
    if current is None:
        current = task.get("current_target_head_sha")
    from models import review_stale

    return review_stale(task.get("reviewed_target_head_sha"), current)


def _add_audit_task(
    context: GitContext,
    audit_id: str,
    target_task_id: str,
    lens: str,
    group: str,
    slot: str,
    scope_paths: Sequence[str],
) -> dict:
    from lifecycle import add_task

    target_id, target_base_sha, target_head_sha = _target_review_values(context, target_task_id)
    return add_task(
        context,
        audit_id,
        audit_id,
        task_type="AUDIT",
        target_task_id=target_id,
        target_base_sha=target_base_sha,
        target_head_sha=target_head_sha,
        lens=lens,
        review_group_id=group,
        review_slot=slot,
        scope_paths=scope_paths,
    )


def _add_fix_task(
    context: GitContext,
    fix_id: str,
    target_task_id: str,
    finding_ids: Sequence[Any],
) -> dict:
    from lifecycle import add_task

    target_id, target_base_sha, target_head_sha = _target_review_values(context, target_task_id)
    normalized_findings = _finding_ids(finding_ids)
    return add_task(
        context,
        fix_id,
        fix_id,
        task_type="FIX",
        target_task_id=target_id,
        target_base_sha=target_base_sha,
        target_head_sha=target_head_sha,
        finding_ids=normalized_findings,
    )


def _add_verify_task(
    context: GitContext,
    verify_id: str,
    fix_task_id: str,
    finding_ids: Sequence[Any],
) -> dict:
    from lifecycle import add_task

    fix_task_id = _task_id(fix_task_id)
    with Board(context).read() as connection:
        fix = require_task(connection, fix_task_id)
    if fix["task_type"] != "FIX":
        raise LightboardError("VERIFY --fix must reference a FIX task")
    if fix["status"] != "DONE" or not fix["head_sha"]:
        raise LightboardError("VERIFY --fix must reference a completed FIX task")
    if not fix["target_task_id"] or not fix["target_base_sha"] or not fix["target_head_sha"]:
        raise LightboardError("FIX has no valid review target")
    return add_task(
        context,
        verify_id,
        verify_id,
        task_type="VERIFY",
        target_task_id=fix["target_task_id"],
        target_base_sha=fix["target_base_sha"],
        target_head_sha=fix["target_head_sha"],
        fix_task_id=fix_task_id,
        fix_head_sha=fix["head_sha"],
        finding_ids=_finding_ids(finding_ids),
    )


def _audit_reviewer(context: GitContext, audit_task_id: str, explicit: Optional[str]) -> str:
    with Board(context).read() as connection:
        audit = require_task(connection, _task_id(audit_task_id))
    if audit["task_type"] != "AUDIT":
        raise LightboardError("findings can only belong to AUDIT tasks")
    if not audit["owner"]:
        raise LightboardError("AUDIT must be claimed before adding a finding")
    return audit["owner"] if explicit is None else _text(explicit, "reviewer")


def _finding_list_result(context: GitContext, target_task_id: str) -> dict:
    from snapshot import build_snapshot

    target_task_id = _task_id(target_task_id)
    findings = [
        finding
        for finding in build_snapshot(context)["findings"]
        if finding["target_task_id"] == target_task_id
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "target_task_id": target_task_id,
        "findings": findings,
    }
