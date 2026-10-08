#!/usr/bin/env python3
"""Executable entrypoint and compatibility facade for the Lightboard API."""

from __future__ import annotations

import sys
from pathlib import Path


_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from cli import (  # noqa: E402,F401
    _execute,
    _parse,
    _parser,
    _positive_int,
    _watch,
    main,
)
from db import Board, FINDING_MIGRATION_COLUMNS, MIGRATION_COLUMNS, SCHEMA, _database_error, _migrate  # noqa: E402,F401
from git import (  # noqa: E402,F401
    _current_task_head,
    _current_target_head,
    _git_commit_status,
    _git_diff_paths,
    _git_is_ancestor,
    _git_run,
    _git_value,
    _is_production_path,
    _read_only_violation,
    _verify_commit_lineage,
    resolve_context,
    verify_integration,
)
from lifecycle import (  # noqa: E402,F401
    _ensure_mutable,
    add_entry,
    add_task,
    block_task,
    claim_task,
    done_task,
    review_task,
    set_status,
)
from models import *  # noqa: E402,F401,F403
from repository import (  # noqa: E402,F401
    entry_from_row as _entry_from_row,
    event_from_row as _event_from_row,
    finding_from_row as _finding_from_row,
    load_snapshot_records,
    record_event as _record_event,
    require_finding as _require_finding,
    require_task as _require_task,
    require_task_finding_relation as _require_task_finding_relation,
    task_from_row as _task_from_row,
)
from render import (  # noqa: E402,F401
    _contract_title,
    _deps_label,
    _detail_lines,
    _event_text,
    _render_integration,
    _render_plain,
    _render_review_gate,
    _rich_console,
    _rich_imports,
    _rich_renderable,
    _rich_status,
    _rich_table,
    _short_sha,
    _status_label,
    _visible_for_height,
    render_detail,
    render_list,
)
from review import (  # noqa: E402,F401
    _add_audit_task,
    _add_fix_task,
    _add_verify_task,
    _audit_reviewer,
    _audit_result,
    _finding_list_result,
    _has_event_for_target_head,
    _infer_verify_task,
    _review_gate_result,
    _review_task_summary,
    _review_aggregate,
    _target_review_values,
    _text_values,
    _validate_related_findings,
    _validate_target_binding,
    _validate_task_definition,
    add_finding,
    create_finding,
    fix_finding,
    get_finding,
    list_findings,
    reopen_finding,
    review_gate,
    review_state,
    review_staleness,
    verify_finding,
)
from snapshot import (  # noqa: E402,F401
    _compact_snapshot,
    _compact_task,
    _detail_task,
    _copy_task,
    _decorate_snapshot,
    _dependency_order,
    _filtered_snapshot,
    _hidden_sibling_audits,
    _ready_snapshot,
    _sort_tasks,
    _summary,
    _verify_dependency,
    _protocol,
    build_snapshot,
    get_task,
    list_tasks,
    ready_tasks,
)


if __name__ == "__main__":
    raise SystemExit(main())
