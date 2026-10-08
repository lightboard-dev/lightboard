"""Read-only Git context and verification operations."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from models import SCHEMA_VERSION, GitContext, LightboardError


def _git_value(cwd: Path, *args: str, required: bool = True) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        if required:
            raise LightboardError(f"git repository context unavailable: {exc}") from exc
        return None

    if result.returncode != 0:
        if required:
            detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
            raise LightboardError(f"git repository context unavailable: {detail}")
        return None
    value = result.stdout.strip()
    return value or None


def _git_run(context: GitContext, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(context.repo_root),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise LightboardError(f"git verification unavailable: {exc}") from exc


def resolve_context(cwd: Path) -> GitContext:
    cwd = cwd.resolve()
    repo_root_value = _git_value(cwd, "rev-parse", "--show-toplevel")
    common_dir_value = _git_value(cwd, "rev-parse", "--git-common-dir")
    if repo_root_value is None or common_dir_value is None:
        raise LightboardError("git repository context unavailable: git returned no repository path")

    common_dir = Path(common_dir_value)
    if not common_dir.is_absolute():
        common_dir = cwd / common_dir

    branch = _git_value(cwd, "rev-parse", "--abbrev-ref", "HEAD", required=False)
    if branch == "HEAD":
        branch = None

    return GitContext(
        repo_root=Path(repo_root_value).resolve(),
        common_dir=common_dir.resolve(),
        base_sha=_git_value(cwd, "rev-parse", "HEAD", required=False),
        branch=branch,
    )


def _git_commit_status(context: GitContext, value: str) -> Tuple[bool, str]:
    if any(character.isspace() for character in value):
        return False, "contains whitespace"
    result = _git_run(context, "cat-file", "-e", f"{value}^{{commit}}")
    if result.returncode == 0:
        return True, "verified commit"
    detail = result.stderr.strip() or "not a commit in this repository"
    return False, detail


def _git_is_ancestor(context: GitContext, ancestor: str, descendant: str) -> bool:
    result = _git_run(context, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = result.stderr.strip() or "git merge-base failed"
    raise LightboardError(f"git verification failed: {detail}")


def _verify_commit_lineage(context: GitContext, base_sha: Optional[str], head_sha: str) -> Tuple[bool, str]:
    if not base_sha:
        return False, "task has no base_sha"
    head_exists, head_reason = _git_commit_status(context, head_sha)
    if not head_exists:
        return False, f"head SHA is not a commit: {head_reason}"
    base_exists, base_reason = _git_commit_status(context, base_sha)
    if not base_exists:
        return False, f"base SHA is not a commit: {base_reason}"
    try:
        ancestor = _git_is_ancestor(context, base_sha, head_sha)
    except LightboardError as exc:
        return False, str(exc)
    if not ancestor:
        return False, "head SHA is not descended from base_sha"
    return True, "verified commit and base ancestry"


def _current_task_head(context: GitContext, task) -> Optional[str]:
    stored_head = task["head_sha"]
    branch = task["branch"]
    if branch:
        live_head = _git_value(
            context.repo_root,
            "rev-parse",
            "--verify",
            f"{branch}^{{commit}}",
            required=False,
        )
        if live_head:
            return live_head
    return stored_head


def _current_target_head(context: GitContext, target) -> Optional[str]:
    return _current_task_head(context, target)


def _is_production_path(path: str) -> bool:
    # ponytail: conservative source/config heuristic; add repository-specific policy when projects need finer classification.
    path_value = Path(path)
    parts = {part.lower() for part in path_value.parts}
    name = path_value.name.lower()
    if parts & {"test", "tests", "__tests__", "spec", "specs", "fixture", "fixtures", "doc", "docs"}:
        return False
    if name.startswith("test_") or ".test." in name or name.endswith("_test.py"):
        return False
    if path_value.parts and path_value.parts[0].lower() in {"src", "app", "lib", "main"}:
        return True
    if name in {
        "build.gradle",
        "build.gradle.kts",
        "cargo.toml",
        "dockerfile",
        "makefile",
        "package.json",
        "package-lock.json",
        "plugin.yml",
        "pom.xml",
        "requirements.txt",
        "settings.gradle",
        "settings.gradle.kts",
    }:
        return True
    return path_value.suffix.lower() in {
        ".c",
        ".cc",
        ".cpp",
        ".go",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".py",
        ".rb",
        ".rs",
        ".swift",
        ".ts",
        ".tsx",
        ".gradle",
        ".json",
        ".kts",
        ".properties",
        ".sql",
        ".toml",
        ".xml",
        ".yaml",
        ".yml",
    }


def _git_diff_paths(context: GitContext, base_sha: str, head_sha: str) -> list[str]:
    result = _git_run(context, "diff", "--name-only", f"{base_sha}..{head_sha}")
    if result.returncode != 0:
        detail = result.stderr.strip() or "git diff failed"
        raise LightboardError(f"git verification failed: {detail}")
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def _read_only_violation(context: GitContext, base_sha: str, head_sha: str) -> list[str]:
    return [path for path in _git_diff_paths(context, base_sha, head_sha) if _is_production_path(path)]


def verify_integration(context: GitContext, task_id: str) -> dict:
    from snapshot import get_task

    task = get_task(context, task_id)
    integration_head = _git_value(context.repo_root, "rev-parse", "HEAD", required=False)
    dependencies = []
    for verification in task["dependency_verification"]:
        item = copy.deepcopy(verification)
        integrated = False
        reason = item["reason"]
        if item["verified"] and integration_head:
            try:
                integrated = _git_is_ancestor(context, item["head_sha"], integration_head)
            except LightboardError as exc:
                reason = str(exc)
            if not integrated and reason == "verified":
                reason = "head SHA is not an ancestor of integration HEAD"
        elif not integration_head:
            reason = "integration HEAD is missing"
        item["integration_head"] = integration_head
        item["integrated"] = integrated
        item["reason"] = "integrated" if integrated else reason
        dependencies.append(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "integration_head": integration_head,
        "dependencies": dependencies,
        "ok": all(item["integrated"] for item in dependencies),
    }
