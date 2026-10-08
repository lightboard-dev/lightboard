"""Canonical board snapshots and machine-readable API serialization."""

from __future__ import annotations

import copy
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from git import _current_target_head, _git_commit_status, _git_is_ancestor
from models import (
    ACTIVE_STATUSES,
    EVENT_LIMIT,
    REVIEW_TASK_TYPES,
    SCHEMA_VERSION,
    SORT_ORDER,
    STALE_THRESHOLDS,
    STATUS_SUMMARY_VALUES,
    GitContext,
    LightboardError,
    _age_info,
    _time_value,
    review_stale,
)
from repository import load_snapshot_records
from review import _review_aggregate


def _hidden_sibling_audits(context: GitContext, tasks: Sequence[dict]) -> set[str]:
    if not context.branch:
        return set()
    own_audits = [
        task
        for task in tasks
        if task["task_type"] == "AUDIT"
        and task.get("branch") == context.branch
        and task["status"] != "DONE"
        and task.get("review_group_id")
    ]
    groups = {task["review_group_id"] for task in own_audits}
    if not groups:
        return set()
    own_ids = {task["id"] for task in own_audits}
    return {
        task["id"]
        for task in tasks
        if task["task_type"] == "AUDIT"
        and task.get("review_group_id") in groups
        and task["id"] not in own_ids
    }


def _copy_task(task: dict) -> dict:
    return copy.deepcopy(task)


def _verify_dependency(context: GitContext, dependency_id: str, dependency: Optional[dict]) -> dict:
    if dependency is None:
        return {
            "task_id": dependency_id,
            "status": None,
            "head_sha": None,
            "base_sha": None,
            "head_exists": False,
            "base_exists": False,
            "base_is_ancestor": False,
            "verified": False,
            "reason": "dependency task is missing",
        }
    head_sha = dependency.get("head_sha")
    base_sha = dependency.get("base_sha")
    head_exists = False
    base_exists = False
    base_is_ancestor = False
    reasons: List[str] = []
    if head_sha:
        head_exists, head_reason = _git_commit_status(context, head_sha)
        if not head_exists:
            reasons.append(f"head SHA is not verified: {head_reason}")
    else:
        reasons.append("head SHA is missing")
    if base_sha:
        base_exists, base_reason = _git_commit_status(context, base_sha)
        if not base_exists:
            reasons.append(f"base SHA is not verified: {base_reason}")
    else:
        reasons.append("base SHA is missing")
    if head_exists and base_exists:
        try:
            base_is_ancestor = _git_is_ancestor(context, base_sha, head_sha)
        except LightboardError as exc:
            reasons.append(str(exc))
        if not base_is_ancestor:
            reasons.append("head SHA is not descended from base_sha")
    if dependency.get("status") != "DONE":
        reasons.insert(0, f"status is {dependency.get('status')}")
    verified = dependency.get("status") == "DONE" and head_exists and base_exists and base_is_ancestor
    return {
        "task_id": dependency_id,
        "status": dependency.get("status"),
        "head_sha": head_sha,
        "base_sha": base_sha,
        "head_exists": head_exists,
        "base_exists": base_exists,
        "base_is_ancestor": base_is_ancestor,
        "verified": verified,
        "reason": "verified" if verified else "; ".join(dict.fromkeys(reasons)),
    }


def _dependency_order(dependencies: Sequence[str], tasks_by_id: Dict[str, dict]) -> List[str]:
    pending = deque(dependencies)
    seen = set()
    ordered: List[str] = []
    while pending:
        dependency_id = pending.popleft()
        if dependency_id in seen:
            continue
        seen.add(dependency_id)
        dependency = tasks_by_id.get(dependency_id)
        if dependency is None:
            continue
        ordered.append(dependency_id)
        pending.extend(dependency["dependencies"])
    return ordered


def _decorate_snapshot(
    context: GitContext,
    raw_tasks: List[dict],
    entries_by_task: Dict[str, List[dict]],
    findings_by_task: Dict[str, List[dict]],
    related_findings_by_task: Dict[str, List[dict]],
    events: List[dict],
    current: datetime,
) -> List[dict]:
    tasks_by_id = {task["id"]: task for task in raw_tasks}
    events_by_task: Dict[str, List[dict]] = {}
    for event in events:
        if event["task_id"] is not None:
            events_by_task.setdefault(event["task_id"], []).append(event)
    decorated: List[dict] = []
    for raw in raw_tasks:
        task = _copy_task(raw)
        task["entries"] = copy.deepcopy(entries_by_task.get(task["id"], []))
        task["findings"] = copy.deepcopy(findings_by_task.get(task["id"], []))
        task["related_findings"] = copy.deepcopy(related_findings_by_task.get(task["id"], []))
        task["finding_ids"] = sorted(
            {finding["id"] for finding in task["findings"] + task["related_findings"]}
        )
        task_events = events_by_task.get(task["id"], [])
        task["events"] = copy.deepcopy(task_events)
        task["contracts"] = [
            {**entry, "verified": entry["id"] in task["verified_contract_ids"]}
            for entry in task["entries"]
            if entry["kind"] == "CONTRACT"
        ]
        task["contract_ids"] = [entry["id"] for entry in task["contracts"]]
        task["blocker"] = next(
            (entry for entry in reversed(task["entries"]) if entry["kind"] == "BLOCKER"), None
        )
        task["review"] = next(
            (entry for entry in reversed(task["entries"]) if entry["kind"] == "REVIEW_FINDING"), None
        )
        task["dependency_tasks"] = {
            dependency_id: _copy_task(tasks_by_id[dependency_id])
            for dependency_id in task["dependencies"]
            if dependency_id in tasks_by_id
        }
        task["dependency_verification"] = [
            _verify_dependency(context, dependency_id, tasks_by_id.get(dependency_id))
            for dependency_id in task["dependencies"]
        ]
        verification = task["dependency_verification"]
        task["deps_verification"] = {
            "verified": sum(1 for item in verification if item["verified"]),
            "total": len(verification),
            "all_verified": all(item["verified"] for item in verification),
        }
        dependency_contracts: Dict[str, List[dict]] = {}
        inherited_contracts: List[dict] = []
        for dependency_id in _dependency_order(task["dependencies"], tasks_by_id):
            dependency = tasks_by_id[dependency_id]
            contracts = [
                {
                    **entry,
                    "source_task_id": dependency_id,
                    "verified": entry["id"] in dependency["verified_contract_ids"],
                }
                for entry in entries_by_task.get(dependency_id, [])
                if entry["kind"] == "CONTRACT"
            ]
            dependency_contracts[dependency_id] = contracts
            inherited_contracts.extend(contracts)
        task["dependency_contracts"] = dependency_contracts
        task["inherited_contracts"] = inherited_contracts
        task["active"] = task["status"] in ACTIVE_STATUSES
        if task["task_type"] in REVIEW_TASK_TYPES:
            target = tasks_by_id.get(task.get("target_task_id"))
            current_target_head = _current_target_head(context, target) if target else None
            task["review_stale"] = review_stale(task.get("reviewed_target_head_sha"), current_target_head)
            task["current_target_head_sha"] = current_target_head
            if task["task_type"] == "AUDIT":
                task["audit_result"] = {
                    "verified": copy.deepcopy(task["audit_verified"]),
                    "unverified": copy.deepcopy(task["audit_unverified"]),
                    "not_reviewed": copy.deepcopy(task["audit_not_reviewed"]),
                    "findings": copy.deepcopy(task["finding_ids"]),
                }
        else:
            task["review_stale"] = False
            task["current_target_head_sha"] = None
        task.update(_age_info(task["updated_at"], current, task["active"]))
        decorated.append(task)

    paths_to_tasks: Dict[str, List[str]] = {}
    for task in decorated:
        if not task["active"]:
            continue
        for path in task["touches"]:
            paths_to_tasks.setdefault(path, []).append(task["id"])
    conflicts: List[dict] = []
    for path, task_ids in sorted(paths_to_tasks.items()):
        unique_ids = sorted(set(task_ids))
        if len(unique_ids) < 2:
            continue
        conflicts.append({"path": path, "tasks": unique_ids})
        for task in decorated:
            if task["id"] not in unique_ids:
                continue
            task.setdefault("touch_conflicts", [])
            task["touch_conflicts"].extend(
                {"path": path, "task_id": other} for other in unique_ids if other != task["id"]
            )
    for task in decorated:
        task.setdefault("touch_conflicts", [])
        task["touch_conflicts"].sort(key=lambda item: (item["path"], item["task_id"]))
        task["conflicts"] = copy.deepcopy(task["touch_conflicts"])
    all_findings = [finding for task_findings in findings_by_task.values() for finding in task_findings]
    for task in decorated:
        if task["task_type"] == "FEATURE":
            task["review_mode"] = _review_aggregate(context, task, decorated, all_findings)
            task["review_state"] = task["review_mode"]["state"]
    return decorated


def _sort_tasks(tasks: Sequence[dict]) -> List[dict]:
    def key(task: dict) -> Tuple[int, str, str]:
        if task["status"] == "BLOCKED":
            rank = 0
        elif task.get("stale"):
            rank = 1
        else:
            rank = SORT_ORDER.get(task["status"], 99)
        return rank, task["created_at"], task["id"]

    return sorted(tasks, key=key)


def _summary(tasks: Sequence[dict]) -> dict:
    counts = {status.lower(): sum(1 for task in tasks if task["status"] == status) for status in STATUS_SUMMARY_VALUES}
    counts["total"] = len(tasks)
    counts["active"] = sum(1 for task in tasks if task["status"] in ACTIVE_STATUSES)
    counts["stale"] = sum(1 for task in tasks if task.get("stale", False))
    return counts


def build_snapshot(
    context: GitContext,
    now: Optional[object] = None,
    event_limit: int = EVENT_LIMIT,
) -> dict:
    current_time, current = _time_value(now)
    records = load_snapshot_records(context, event_limit)
    raw_tasks = records["raw_tasks"]
    findings = records["findings"]
    events = records["events"]
    all_events = records["all_events"]
    hidden_audits = _hidden_sibling_audits(context, raw_tasks)
    findings = [finding for finding in findings if finding["audit_task_id"] not in hidden_audits]
    events = [event for event in events if event.get("task_id") not in hidden_audits]
    all_events = [event for event in all_events if event.get("task_id") not in hidden_audits]

    tasks = _decorate_snapshot(
        context,
        raw_tasks,
        records["entries_by_task"],
        records["findings_by_task"],
        records["related_findings_by_task"],
        all_events,
        current,
    )
    tasks = _sort_tasks(tasks)
    touch_conflicts: List[dict] = []
    for task in tasks:
        for conflict in task.get("touch_conflicts", []):
            candidate = {"path": conflict["path"], "tasks": sorted([task["id"], conflict["task_id"]])}
            if candidate not in touch_conflicts:
                touch_conflicts.append(candidate)
    touch_conflicts.sort(key=lambda item: (item["path"], item["tasks"]))
    contracts = []
    for task in tasks:
        contracts.extend({**contract, "source_task_id": task["id"]} for contract in task["contracts"])
    return {
        "schema_version": SCHEMA_VERSION,
        "project_root": context.repo_root.name,
        "repo_root": str(context.repo_root),
        "branch": context.branch,
        "current_time": current_time,
        "staleness": dict(STALE_THRESHOLDS),
        "summary": _summary(tasks),
        "tasks": tasks,
        "dependency_verification": [
            {"task_id": task["id"], **task["deps_verification"]} for task in tasks
        ],
        "touch_conflicts": touch_conflicts,
        "conflicts": touch_conflicts,
        "contracts": contracts,
        "findings": findings,
        "events": events,
        "last_events": events,
    }


def get_task(context: GitContext, task_id: str) -> dict:
    from models import _task_id

    task_id = _task_id(task_id)
    snapshot = build_snapshot(context)
    for task in snapshot["tasks"]:
        if task["id"] == task_id:
            result = {"schema_version": SCHEMA_VERSION, **task}
            if task["task_type"] == "AUDIT":
                result["review_result"] = copy.deepcopy(task.get("audit_result"))
            elif task["task_type"] == "FIX":
                result["fix_sha"] = task.get("head_sha")
                result["fix_findings"] = copy.deepcopy(task.get("related_findings", []))
            elif task["task_type"] == "VERIFY":
                result["verification_results"] = copy.deepcopy(task.get("related_findings", []))
            return result
    raise LightboardError(f"task not found: {task_id}")


def list_tasks(context: GitContext) -> List[dict]:
    return build_snapshot(context)["tasks"]


def ready_tasks(context: GitContext) -> List[dict]:
    return _ready_snapshot(context)["tasks"]


def _ready_snapshot(context: GitContext) -> dict:
    snapshot = build_snapshot(context)
    ready = [
        task
        for task in snapshot["tasks"]
        if task["status"] != "DONE"
        and task["deps_verification"]["total"] == task["deps_verification"]["verified"]
    ]
    result = copy.deepcopy(snapshot)
    result["tasks"] = _sort_tasks(ready)
    result["summary"] = _summary(result["tasks"])
    result["ready_task_ids"] = [task["id"] for task in result["tasks"]]
    return result


def _filtered_snapshot(
    context: GitContext,
    active: bool = False,
    limit: Optional[int] = None,
    agent: Optional[str] = None,
) -> dict:
    snapshot = build_snapshot(context)
    tasks = snapshot["tasks"]
    if active:
        tasks = [task for task in tasks if task["status"] in ACTIVE_STATUSES]
    if agent is not None:
        tasks = [task for task in tasks if task.get("owner") == agent]
    tasks = _sort_tasks(tasks)
    if limit is not None:
        tasks = tasks[:limit]
    result = copy.deepcopy(snapshot)
    result["tasks"] = tasks
    result["summary"] = _summary(tasks)
    result["filters"] = {"active": active, "limit": limit, "agent": agent}
    return result


_OVERVIEW_TASK_FIELDS = (
    "id",
    "title",
    "task_type",
    "status",
    "owner",
    "branch",
    "base_sha",
    "head_sha",
    "dependencies",
    "deps_verification",
    "block_reason",
    "summary",
    "active",
    "age_seconds",
    "age",
    "warning",
    "stale",
    "stale_level",
    "touch_conflicts",
)


def _review_overview(task: dict) -> Optional[dict]:
    if task["task_type"] == "FEATURE":
        review = task.get("review_mode")
        if review is None:
            return None
        return {
            "state": review["state"],
            "target_head_sha": review["target_head_sha"],
            "audits": len(review["audits"]),
            "findings": len(review["findings"]),
            "fixes": len(review["fixes"]),
            "verifications": len(review["verifications"]),
            "stale": bool(review["stale_task_ids"]),
        }
    if task["task_type"] in REVIEW_TASK_TYPES:
        return {
            "task_type": task["task_type"],
            "target_task_id": task.get("target_task_id"),
            "target_head_sha": task.get("target_head_sha"),
            "status": task["status"],
            "summary": task.get("summary") or "",
            "finding_count": len(task.get("finding_ids", [])),
        }
    return None


def _compact_task(task: dict) -> dict:
    compact = {
        field: copy.deepcopy(task[field])
        for field in _OVERVIEW_TASK_FIELDS
        if field in task
    }
    review = _review_overview(task)
    if review is not None:
        compact["review_summary"] = review
    return compact


def _compact_snapshot(snapshot: dict) -> dict:
    result = {
        field: copy.deepcopy(snapshot[field])
        for field in (
            "schema_version",
            "project_root",
            "repo_root",
            "branch",
            "current_time",
            "staleness",
            "summary",
            "filters",
            "ready_task_ids",
            "touch_conflicts",
        )
        if field in snapshot
    }
    result["tasks"] = [_compact_task(task) for task in snapshot["tasks"]]
    return result


_DETAIL_TASK_FIELDS = (
    "id",
    "title",
    "task_type",
    "status",
    "owner",
    "branch",
    "base_sha",
    "head_sha",
    "target_task_id",
    "target_base_sha",
    "target_head_sha",
    "current_target_head_sha",
    "reviewed_target_head_sha",
    "review_stale",
    "review_state",
    "review_group_id",
    "review_slot",
    "lens",
    "fix_task_id",
    "fix_head_sha",
    "scope_paths",
    "dependencies",
    "summary",
    "created_at",
    "updated_at",
    "completed_at",
    "follow_up_to",
    "touches",
    "block_reason",
    "verified_contract_ids",
    "active",
    "age_seconds",
    "age",
    "warning",
    "stale",
    "stale_level",
    "deps_verification",
    "dependency_verification",
    "touch_conflicts",
)


def _public_contract(contract: dict) -> dict:
    return {
        field: copy.deepcopy(contract[field])
        for field in ("id", "body", "created_at", "verified")
        if field in contract
    }


def _public_note(entry: dict) -> dict:
    return {
        field: copy.deepcopy(entry[field])
        for field in ("id", "body", "created_at")
        if field in entry
    }


def _compact_event(event: dict) -> dict:
    payload = {
        key: copy.deepcopy(value)
        for key, value in event.get("payload", {}).items()
        if key
        not in {
            "body",
            "finding",
            "reason",
            "summary",
            "scope_paths",
            "verified_contract_ids",
            "verified",
            "unverified",
            "not_reviewed",
        }
    }
    return {
        field: copy.deepcopy(event[field])
        for field in ("id", "task_id", "type", "created_at")
        if field in event
    } | {"payload": payload}


def _detail_findings(task: dict) -> list[dict]:
    if task["task_type"] == "FEATURE":
        candidates = (task.get("review_mode") or {}).get("findings", [])
    elif task["task_type"] == "AUDIT":
        candidates = task.get("findings", [])
    else:
        candidates = task.get("related_findings", [])
    findings: list[dict] = []
    seen: set[int] = set()
    for finding in candidates:
        if finding["id"] in seen:
            continue
        seen.add(finding["id"])
        findings.append(copy.deepcopy(finding))
    return findings


def _detail_review(task: dict) -> Optional[dict]:
    review = task.get("review_mode")
    if task["task_type"] == "FEATURE" and review is not None:
        return {
            "state": review["state"],
            "target_task_id": review["target_task_id"],
            "target_head_sha": review["target_head_sha"],
            "audits": copy.deepcopy(review["audits"]),
            "finding_ids": [finding["id"] for finding in review["findings"]],
            "fixes": copy.deepcopy(review["fixes"]),
            "verifications": copy.deepcopy(review["verifications"]),
            "stale_task_ids": copy.deepcopy(review["stale_task_ids"]),
            "gate": copy.deepcopy(review["gate"]),
        }
    if task["task_type"] == "AUDIT":
        return {
            "state": "AUDIT",
            "audit_result": copy.deepcopy(task.get("audit_result")),
            "finding_ids": copy.deepcopy(task.get("finding_ids", [])),
        }
    if task["task_type"] in ("FIX", "VERIFY"):
        return {
            "state": task["task_type"],
            "finding_ids": copy.deepcopy(task.get("finding_ids", [])),
        }
    return None


def _detail_task(task: dict) -> dict:
    detail = {
        field: copy.deepcopy(task[field])
        for field in _DETAIL_TASK_FIELDS
        if field in task
    }
    detail["dependency_tasks"] = {}
    detail["dependency_contracts"] = {}
    for dependency_id in task.get("dependencies", []):
        dependency = task.get("dependency_tasks", {}).get(dependency_id)
        if dependency is not None:
            detail["dependency_tasks"][dependency_id] = {
                field: copy.deepcopy(dependency[field])
                for field in ("id", "status", "head_sha", "summary")
                if field in dependency
            }
        contracts = task.get("dependency_contracts", {}).get(dependency_id, [])
        detail["dependency_contracts"][dependency_id] = [
            _public_contract(contract) for contract in contracts
        ]
    detail["contracts"] = [
        _public_contract(contract) for contract in task.get("contracts", [])
    ]
    detail["notes"] = [
        _public_note(entry)
        for entry in task.get("entries", [])
        if entry.get("kind") == "NOTE"
    ]
    detail["findings"] = _detail_findings(task)
    review = _detail_review(task)
    if review is not None:
        detail["review"] = review
    legacy_review = task.get("review")
    if legacy_review is not None:
        detail["legacy_review"] = _public_note(legacy_review)
    detail["events"] = [_compact_event(event) for event in task.get("events", [])]
    return {"schema_version": SCHEMA_VERSION, **detail}


def _protocol(value: object) -> dict:
    if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION:
        return value
    if isinstance(value, dict):
        return {"schema_version": SCHEMA_VERSION, **value}
    return {"schema_version": SCHEMA_VERSION, "result": value}
