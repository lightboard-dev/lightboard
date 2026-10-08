"""Value objects, protocol constants, and side-effect-free validation helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple


SCHEMA_VERSION = 3
DB_SCHEMA_VERSION = 3
TASK_TYPES = ("FEATURE", "AUDIT", "FIX", "VERIFY")
STATUSES = ("TODO", "CLAIMED", "IN_PROGRESS", "BLOCKED", "DONE")
# REVIEW is retained only for the pre-Review-Mode generic command and migrated rows.
LEGACY_STATUS = "REVIEW"
MANUAL_STATUSES = ("IN_PROGRESS", "BLOCKED")
ACTIVE_STATUSES = frozenset(("CLAIMED", "IN_PROGRESS", "BLOCKED", LEGACY_STATUS))
ENTRY_KINDS = ("NOTE", "CONTRACT", "BLOCKER", "REVIEW_FINDING")
FINDING_STATUSES = ("OPEN", "FIXED", "VERIFIED", "REOPENED", "REJECTED", "DEFERRED")
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
LENSES = (
    "GENERAL",
    "STATE",
    "OWNERSHIP",
    "LIFECYCLE",
    "CONCURRENCY",
    "FAILURE",
    "TRANSACTION",
    "CRITICAL",
    "TEST_ORACLE",
)
REVIEW_TASK_TYPES = frozenset(("AUDIT", "FIX", "VERIFY"))
READ_ONLY_REVIEW_TYPES = frozenset(("AUDIT", "VERIFY"))
STATUS_SUMMARY_VALUES = STATUSES + (LEGACY_STATUS,)
# ponytail: bounded local lock wait; add configurable retry/backoff if contention grows.
BUSY_TIMEOUT_MS = 750
STALE_THRESHOLDS = {"warning_seconds": 30 * 60, "stale_seconds": 2 * 60 * 60}
EVENT_LIMIT = 20
SORT_ORDER = {"BLOCKED": 2, LEGACY_STATUS: 4, "IN_PROGRESS": 5, "CLAIMED": 6, "TODO": 7, "DONE": 8}


class LightboardError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitContext:
    repo_root: Path
    common_dir: Path
    base_sha: Optional[str]
    branch: Optional[str]

    @property
    def board_path(self) -> Path:
        return self.common_dir / "agent-board" / "board.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise LightboardError(f"{label} must be text")
    value = value.strip()
    if not value:
        raise LightboardError(f"{label} must not be empty")
    if "\x00" in value:
        raise LightboardError(f"{label} contains a NUL character")
    return value


def _task_id(value: str) -> str:
    return _text(value, "task id")


def _optional_text(value: Optional[str], label: str) -> Optional[str]:
    return None if value is None else _text(value, label)


def _enum(value: str, label: str, allowed: Sequence[str]) -> str:
    normalized = _text(value, label).upper()
    if normalized not in allowed:
        raise LightboardError(f"invalid {label} {normalized}; expected one of {', '.join(allowed)}")
    return normalized


def _sha(value: str, label: str) -> str:
    normalized = _text(value, label)
    if any(character.isspace() for character in normalized):
        raise LightboardError(f"{label} contains whitespace")
    return normalized


def _finding_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LightboardError("finding id must be a positive integer")
    return value


def _finding_ids(values: Sequence[Any]) -> list[int]:
    normalized = [_finding_id(value) for value in values]
    if len(set(normalized)) != len(normalized):
        raise LightboardError("finding ids must be unique")
    return normalized


def _json_list(value: Any, label: str) -> list[Any]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, ValueError) as exc:
        raise LightboardError(f"database error: invalid {label}") from exc
    if not isinstance(decoded, list):
        raise LightboardError(f"database error: invalid {label}")
    return decoded


def _string_list(value: Any, label: str) -> list[str]:
    decoded = _json_list(value, label)
    if any(not isinstance(item, str) for item in decoded):
        raise LightboardError(f"database error: invalid {label}")
    return decoded


def _int_list(value: Any, label: str) -> list[int]:
    decoded = _json_list(value, label)
    if any(not isinstance(item, int) or isinstance(item, bool) for item in decoded):
        raise LightboardError(f"database error: invalid {label}")
    return decoded


def _mutation(message: str, task_id: str, **fields: Any) -> dict:
    result = {"ok": True, "message": message, "task_id": task_id}
    result.update(fields)
    return result


def _normalize_repo_paths(context: GitContext, paths: Sequence[str], label: str) -> list[str]:
    normalized: list[str] = []
    root = context.repo_root
    for raw in paths:
        value = _text(raw, f"{label} path")
        path = Path(value)
        if path.is_absolute():
            raise LightboardError(f"{label} path must be repo-relative: {value}")
        resolved = (root / path).resolve(strict=False)
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise LightboardError(f"{label} path escapes repository root: {value}") from exc
        relative_value = relative.as_posix()
        if relative_value in ("", "."):
            raise LightboardError(f"{label} path must name a repo-relative file: {value}")
        if relative_value not in normalized:
            normalized.append(relative_value)
    return normalized


def _normalize_touches(context: GitContext, touches: Sequence[str]) -> list[str]:
    return _normalize_repo_paths(context, touches, "touch")


def _normalize_scope_paths(context: GitContext, scope_paths: Sequence[str]) -> list[str]:
    return _normalize_repo_paths(context, scope_paths, "scope")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise LightboardError(f"database error: invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _time_value(value: Optional[Any] = None) -> Tuple[str, datetime]:
    if value is None:
        current = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
    elif isinstance(value, str):
        current = _parse_time(value)
    else:
        raise LightboardError("invalid current time")
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z"), current


def _age_info(updated_at: str, current: datetime, active: bool = True) -> dict:
    seconds = max(0, int((current - _parse_time(updated_at)).total_seconds()))
    stale = active and seconds > STALE_THRESHOLDS["stale_seconds"]
    warning = active and seconds > STALE_THRESHOLDS["warning_seconds"]
    if stale:
        level = "stale"
    elif warning:
        level = "warning"
    else:
        level = "fresh"
    return {
        "age_seconds": seconds,
        "age": _format_age(seconds),
        "warning": warning,
        "stale": stale,
        "stale_level": level,
    }


def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _task_is_stale(task: dict, current: Optional[datetime] = None) -> bool:
    if task["status"] not in ACTIVE_STATUSES:
        return False
    if current is None:
        _, current = _time_value()
    return _age_info(task["updated_at"], current)["stale"]


def review_stale(reviewed_target_head_sha: Optional[str], current_target_head_sha: Optional[str]) -> bool:
    """Return whether a review was recorded against a different target HEAD."""
    return reviewed_target_head_sha != current_target_head_sha
