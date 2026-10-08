import json
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "lightboard.py"
POSIX_LAUNCHER = PROJECT_ROOT / "lb"
WINDOWS_LAUNCHER = PROJECT_ROOT / "lb.cmd"
README = PROJECT_ROOT / "README.md"

LIGHTBOARD_SPEC = importlib.util.spec_from_file_location("lightboard_foundation", SCRIPT)
LIGHTBOARD = importlib.util.module_from_spec(LIGHTBOARD_SPEC)
sys.modules[LIGHTBOARD_SPEC.name] = LIGHTBOARD
LIGHTBOARD_SPEC.loader.exec_module(LIGHTBOARD)


def git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


def commit_file(cwd, name, content, message):
    path = Path(cwd) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "HEAD").stdout.strip()


def unrelated_commit(cwd):
    tree = git(cwd, "rev-parse", "HEAD^{tree}").stdout.strip()
    result = subprocess.run(
        ["git", "commit-tree", tree, "-m", "unrelated"],
        cwd=cwd,
        text=True,
        input="",
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def timestamp_ago(**kwargs):
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat(timespec="microseconds").replace("+00:00", "Z")


class LightboardCliTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.repo = root / "repo"
        self.worktree_a = root / "worktree-a"
        self.worktree_b = root / "worktree-b"
        self.outside = root / "outside"
        self.repo.mkdir()
        self.outside.mkdir()

        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "tests@example.invalid")
        git(self.repo, "config", "user.name", "Lightboard Tests")
        (self.repo / "README.md").write_text("test repository\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "initial")
        git(self.repo, "worktree", "add", "-q", "-b", "agent-a", str(self.worktree_a))
        git(self.repo, "worktree", "add", "-q", "-b", "agent-b", str(self.worktree_b))
        (self.worktree_a / "nested").mkdir()
        (self.worktree_b / "nested").mkdir()

    def tearDown(self):
        git(self.repo, "worktree", "remove", "--force", str(self.worktree_a), check=False)
        git(self.repo, "worktree", "remove", "--force", str(self.worktree_b), check=False)
        self.temp_dir.cleanup()

    def run_lb(self, cwd, *args):
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            env=env,
        )

    def run_launcher(self, cwd, *args):
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            return subprocess.run(
                [str(POSIX_LAUNCHER), *args],
                cwd=cwd,
                text=True,
                capture_output=True,
                env=env,
            )
        except FileNotFoundError as exc:
            self.fail(f"POSIX launcher is missing: {exc}")

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def json_result(self, cwd, *args):
        return json.loads(self.assert_ok(self.run_lb(cwd, *args)).stdout)

    def json_result_any_status(self, cwd, *args):
        result = self.run_lb(cwd, *args)
        self.assertTrue(result.stdout, result.stderr)
        return result.returncode, json.loads(result.stdout)

    def add_task(self, cwd, task_id, title, *extra):
        return self.assert_ok(self.run_lb(cwd, "add", task_id, "--title", title, *extra))

    def board_path(self, cwd):
        common_dir = git(cwd, "rev-parse", "--git-common-dir").stdout.strip()
        common_path = Path(common_dir)
        if not common_path.is_absolute():
            common_path = (Path(cwd) / common_path).resolve()
        return common_path / "agent-board" / "board.db"

    def context(self, cwd=None):
        return LIGHTBOARD.resolve_context(cwd or self.repo)

    def add_done_feature(self, task_id="feature"):
        context = self.context()
        LIGHTBOARD.add_task(context, task_id, "Feature", [])
        self.assert_ok(self.run_lb(self.repo, "claim", task_id, "--agent", "feature-agent", "--branch", "feature"))
        self.assert_ok(self.run_lb(self.repo, "status", task_id, "IN_PROGRESS"))
        head_sha = commit_file(self.repo, f"{task_id}.txt", f"{task_id}\n", task_id)
        LIGHTBOARD.done_task(context, task_id, head_sha, "feature complete")
        return LIGHTBOARD.get_task(context, task_id)

    def add_audit(self, audit_id, target, group=None, slot=None, lens="STATE"):
        context = self.context()
        return LIGHTBOARD.add_task(
            context,
            audit_id,
            audit_id,
            task_type="AUDIT",
            target_task_id=target["id"],
            target_base_sha=target["base_sha"],
            target_head_sha=target["head_sha"],
            lens=lens,
            review_group_id=group,
            review_slot=slot,
        )

    def add_in_progress(self, task_id):
        context = self.context()
        LIGHTBOARD.claim_task(context, task_id, "agent", "branch")
        LIGHTBOARD.set_status(context, task_id, "IN_PROGRESS")

    def test_review_task_types_and_target_sha_binding_persist(self):
        target = self.add_done_feature()
        context = self.context()

        self.add_audit("audit-a", target, "group-1", "A")
        audit = LIGHTBOARD.get_task(context, "audit-a")
        self.assertEqual(
            (audit["task_type"], audit["target_task_id"], audit["target_base_sha"], audit["target_head_sha"]),
            ("AUDIT", "feature", target["base_sha"], target["head_sha"]),
        )

        finding = LIGHTBOARD.create_finding(
            context,
            "audit-a",
            reviewer="reviewer-a",
            severity="HIGH",
            lens="STATE",
            claim="state can diverge",
            evidence="the update has two writers",
            scenario="two agents update the same row",
            required_property="the state transition is atomic",
        )
        fix = LIGHTBOARD.add_task(
            context,
            "fix-1",
            "fix finding",
            task_type="FIX",
            target_task_id="feature",
            target_base_sha=target["base_sha"],
            target_head_sha=target["head_sha"],
            finding_ids=[finding["id"]],
        )
        self.assertEqual(fix["task_type"], "FIX")
        self.add_in_progress("fix-1")
        fix_sha = commit_file(self.repo, "fix.txt", "fix\n", "fix")
        LIGHTBOARD.done_task(context, "fix-1", fix_sha, "fix complete")
        LIGHTBOARD.fix_finding(context, "fix-1", finding["id"])
        self.assertEqual(LIGHTBOARD.get_task(context, "fix-1")["finding_ids"], [finding["id"]])

        verify = LIGHTBOARD.add_task(
            context,
            "verify-1",
            "verify fix",
            task_type="VERIFY",
            target_task_id="feature",
            target_base_sha=target["base_sha"],
            target_head_sha=target["head_sha"],
            fix_task_id="fix-1",
            fix_head_sha=fix_sha,
            finding_ids=[finding["id"]],
        )
        self.assertEqual(verify["task_type"], "VERIFY")
        self.assertEqual(LIGHTBOARD.get_task(context, "verify-1")["fix_head_sha"], fix_sha)

    def test_findings_persist_review_shape_and_group_slots(self):
        target = self.add_done_feature()
        context = self.context()
        self.add_audit("audit-a", target, "group-1", "A")
        self.add_audit("audit-b", target, "group-1", "B")
        finding = LIGHTBOARD.create_finding(
            context,
            "audit-a",
            reviewer="reviewer-a",
            severity="CRITICAL",
            lens="CRITICAL",
            claim="Claim",
            evidence="Evidence",
            scenario="Failure scenario",
            required_property="Required property",
        )

        audit_a = LIGHTBOARD.get_task(context, "audit-a")
        audit_b = LIGHTBOARD.get_task(context, "audit-b")
        persisted = LIGHTBOARD.get_finding(context, finding["id"])
        self.assertEqual((audit_a["review_group_id"], audit_a["review_slot"]), ("group-1", "A"))
        self.assertEqual((audit_b["review_group_id"], audit_b["review_slot"]), ("group-1", "B"))
        self.assertEqual(
            {field: persisted[field] for field in ("audit_task_id", "target_task_id", "reviewer", "severity", "lens")},
            {
                "audit_task_id": "audit-a",
                "target_task_id": "feature",
                "reviewer": "reviewer-a",
                "severity": "CRITICAL",
                "lens": "CRITICAL",
            },
        )
        self.assertEqual(
            [persisted[field] for field in ("claim", "evidence", "scenario", "required_property")],
            ["Claim", "Evidence", "Failure scenario", "Required property"],
        )
        self.assertEqual(persisted["status"], "OPEN")

    def test_fix_only_transitions_open_or_reopened_to_fixed(self):
        target = self.add_done_feature()
        context = self.context()
        self.add_audit("audit-a", target, lens="FAILURE")
        finding = LIGHTBOARD.create_finding(
            context,
            "audit-a",
            reviewer="reviewer-a",
            severity="MEDIUM",
            lens="FAILURE",
            claim="claim",
            evidence="evidence",
            scenario="scenario",
            required_property="property",
        )
        LIGHTBOARD.add_task(
            context,
            "fix-1",
            "fix",
            task_type="FIX",
            target_task_id="feature",
            target_base_sha=target["base_sha"],
            target_head_sha=target["head_sha"],
            finding_ids=[finding["id"]],
        )
        self.add_in_progress("fix-1")
        fix_sha = commit_file(self.repo, "fix.txt", "fix\n", "fix")
        LIGHTBOARD.done_task(context, "fix-1", fix_sha, "fixed")
        fixed = LIGHTBOARD.fix_finding(context, "fix-1", finding["id"])
        self.assertEqual(fixed["status"], "FIXED")
        with self.assertRaises(LIGHTBOARD.LightboardError):
            LIGHTBOARD.fix_finding(context, "fix-1", finding["id"])
        self.assertEqual(LIGHTBOARD.get_finding(context, finding["id"])["status"], "FIXED")

    def test_verify_transitions_fixed_to_verified_or_reopened(self):
        target = self.add_done_feature()
        context = self.context()
        self.add_audit("audit-a", target, lens="TEST_ORACLE")
        finding = LIGHTBOARD.create_finding(
            context,
            "audit-a",
            reviewer="reviewer-a",
            severity="LOW",
            lens="TEST_ORACLE",
            claim="claim",
            evidence="evidence",
            scenario="scenario",
            required_property="property",
        )
        LIGHTBOARD.add_task(
            context,
            "fix-1",
            "fix",
            task_type="FIX",
            target_task_id="feature",
            target_base_sha=target["base_sha"],
            target_head_sha=target["head_sha"],
            finding_ids=[finding["id"]],
        )
        self.add_in_progress("fix-1")
        fix_sha = commit_file(self.repo, "fix.txt", "fix\n", "fix")
        LIGHTBOARD.done_task(context, "fix-1", fix_sha, "fixed")
        LIGHTBOARD.fix_finding(context, "fix-1", finding["id"])
        LIGHTBOARD.add_task(
            context,
            "verify-1",
            "verify",
            task_type="VERIFY",
            target_task_id="feature",
            target_base_sha=target["base_sha"],
            target_head_sha=target["head_sha"],
            fix_task_id="fix-1",
            fix_head_sha=fix_sha,
            finding_ids=[finding["id"]],
        )
        self.add_in_progress("verify-1")
        verify_sha = commit_file(self.repo, "verify.txt", "verify\n", "verify")
        LIGHTBOARD.done_task(context, "verify-1", verify_sha, "verified")
        self.assertEqual(LIGHTBOARD.verify_finding(context, "verify-1", finding["id"], "VERIFIED")["status"], "VERIFIED")
        with self.assertRaises(LIGHTBOARD.LightboardError):
            LIGHTBOARD.verify_finding(context, "verify-1", finding["id"], "REOPENED")

        reopened = LIGHTBOARD.create_finding(
            context,
            "audit-a",
            reviewer="reviewer-a",
            severity="LOW",
            lens="TEST_ORACLE",
            claim="claim 2",
            evidence="evidence 2",
            scenario="scenario 2",
            required_property="property 2",
        )
        LIGHTBOARD.add_task(
            context,
            "fix-2",
            "fix 2",
            task_type="FIX",
            target_task_id="feature",
            target_base_sha=target["base_sha"],
            target_head_sha=target["head_sha"],
            finding_ids=[reopened["id"]],
        )
        self.add_in_progress("fix-2")
        fix_two_sha = commit_file(self.repo, "fix-two.txt", "fix two\n", "fix two")
        LIGHTBOARD.done_task(context, "fix-2", fix_two_sha, "fixed")
        LIGHTBOARD.fix_finding(context, "fix-2", reopened["id"])
        LIGHTBOARD.add_task(
            context,
            "verify-2",
            "verify 2",
            task_type="VERIFY",
            target_task_id="feature",
            target_base_sha=target["base_sha"],
            target_head_sha=target["head_sha"],
            fix_task_id="fix-2",
            fix_head_sha=fix_two_sha,
            finding_ids=[reopened["id"]],
        )
        self.add_in_progress("verify-2")
        verify_two_sha = commit_file(self.repo, "verify-two.txt", "verify two\n", "verify two")
        LIGHTBOARD.done_task(context, "verify-2", verify_two_sha, "reopened")
        self.assertEqual(LIGHTBOARD.verify_finding(context, "verify-2", reopened["id"], "REOPENED")["status"], "REOPENED")

    def test_review_stale_is_bound_to_exact_target_head(self):
        self.assertFalse(LIGHTBOARD.review_stale("commit-a", "commit-a"))
        self.assertTrue(LIGHTBOARD.review_stale("commit-a", "commit-b"))
        target = self.add_done_feature()
        context = self.context()
        self.add_audit("audit-a", target, lens="GENERAL")
        self.assertFalse(LIGHTBOARD.review_staleness(context, "audit-a", target["head_sha"]))
        self.assertTrue(LIGHTBOARD.review_staleness(context, "audit-a", "new-descendant"))

    def add_done_feature_cli(self, task_id="feature"):
        self.assert_ok(self.run_lb(self.repo, "add", task_id, "--title", "Feature"))
        self.assert_ok(
            self.run_lb(
                self.repo,
                "claim",
                task_id,
                "--agent",
                "feature-agent",
                "--branch",
                "main",
            )
        )
        self.assert_ok(self.run_lb(self.repo, "status", task_id, "IN_PROGRESS"))
        head_sha = commit_file(self.repo, f"{task_id}.txt", f"{task_id}\n", task_id)
        self.assert_ok(
            self.run_lb(
                self.repo,
                "done",
                task_id,
                "--sha",
                head_sha,
                "--summary",
                "feature complete",
            )
        )
        return self.json_result(self.repo, "--json", "get", task_id)

    def add_audit_cli(self, target, audit_id="audit-a", slot="A", scope="src/main.py"):
        self.assert_ok(
            self.run_lb(
                self.repo,
                "audit",
                "add",
                audit_id,
                "--target",
                target["id"],
                "--lens",
                "STATE",
                "--group",
                "group-1",
                "--slot",
                slot,
                "--scope",
                scope,
            )
        )
        self.assert_ok(
            self.run_lb(
                self.worktree_a if slot == "A" else self.worktree_b,
                "claim",
                audit_id,
                "--agent",
                f"reviewer-{slot.lower()}",
                "--branch",
                "agent-a" if slot == "A" else "agent-b",
            )
        )
        self.assert_ok(
            self.run_lb(
                self.worktree_a if slot == "A" else self.worktree_b,
                "status",
                audit_id,
                "IN_PROGRESS",
            )
        )
        return self.json_result(self.repo, "--json", "get", audit_id)

    def finish_audit_cli(self, audit_id, head_sha, *result_args):
        command = [
            "done",
            audit_id,
            "--sha",
            head_sha,
            "--summary",
            "scope reviewed",
            *result_args,
        ]
        return self.assert_ok(self.run_lb(self.repo, *command))

    def add_finding_cli(self, audit_id, claim="state can diverge", severity="HIGH"):
        return self.json_result(
            self.worktree_a,
            "--json",
            "finding",
            "add",
            audit_id,
            "--severity",
            severity,
            "--lens",
            "STATE",
            "--claim",
            claim,
            "--evidence",
            "two writers can observe the same state",
            "--scenario",
            "two agents update the same row",
            "--required",
            "the state transition is atomic",
        )

    def test_review_clean_e2e_passes_gate(self):
        target = self.add_done_feature_cli()
        audit = self.add_audit_cli(target)
        self.finish_audit_cli(audit["id"], target["head_sha"], "--verified", "scope checked")

        _, gate = self.json_result_any_status(self.repo, "--json", "review", "gate", target["id"])

        self.assertTrue(gate["ok"], gate)
        self.assertEqual(gate["target_head_sha"], target["head_sha"])
        self.assertEqual(gate["state"], "CLEAN")

    def test_review_open_finding_fails_gate(self):
        target = self.add_done_feature_cli()
        audit = self.add_audit_cli(target)
        finding = self.add_finding_cli(audit["id"])
        self.finish_audit_cli(audit["id"], target["head_sha"], "--verified", "scope checked")

        _, gate = self.json_result_any_status(self.repo, "--json", "review", "gate", target["id"])

        self.assertFalse(gate["ok"])
        self.assertIn(str(finding["id"]), " ".join(gate["reasons"]))
        self.assertIn("OPEN", " ".join(gate["reasons"]))

    def create_fixed_finding_cli(self):
        target = self.add_done_feature_cli()
        audit = self.add_audit_cli(target)
        finding = self.add_finding_cli(audit["id"])
        self.finish_audit_cli(audit["id"], target["head_sha"], "--verified", "scope checked")
        self.assert_ok(
            self.run_lb(
                self.worktree_a,
                "fix",
                "add",
                "fix-1",
                "--target",
                target["id"],
                "--findings",
                str(finding["id"]),
            )
        )
        self.assert_ok(
            self.run_lb(
                self.worktree_a,
                "claim",
                "fix-1",
                "--agent",
                "fix-agent",
                "--branch",
                "agent-a",
            )
        )
        self.assert_ok(self.run_lb(self.worktree_a, "status", "fix-1", "IN_PROGRESS"))
        fix_sha = commit_file(self.worktree_a, "src/fix.py", "fixed\n", "fix finding")
        self.assert_ok(
            self.run_lb(
                self.worktree_a,
                "done",
                "fix-1",
                "--sha",
                fix_sha,
                "--summary",
                "finding fixed",
            )
        )
        return target, finding, fix_sha

    def add_verify_cli(self, target, finding, fix_sha, verify_id="verify-1"):
        self.assert_ok(
            self.run_lb(
                self.worktree_b,
                "verify",
                "add",
                verify_id,
                "--fix",
                "fix-1",
                "--findings",
                str(finding["id"]),
            )
        )
        self.assert_ok(
            self.run_lb(
                self.worktree_b,
                "claim",
                verify_id,
                "--agent",
                "verify-agent",
                "--branch",
                "agent-b",
            )
        )
        self.assert_ok(self.run_lb(self.worktree_b, "status", verify_id, "IN_PROGRESS"))
        verify_sha = commit_file(self.worktree_b, "tests/verify.txt", "verified\n", "verify fix")
        self.assert_ok(
            self.run_lb(
                self.worktree_b,
                "done",
                verify_id,
                "--sha",
                verify_sha,
                "--summary",
                "verification complete",
            )
        )
        return verify_sha

    def build_json_protocol_fixture(self):
        for index in range(3):
            task_id = f"history-{index}"
            self.add_task(self.repo, task_id, f"Historical task {index}")
            self.assert_ok(
                self.run_lb(self.repo, "claim", task_id, "--agent", "history-agent", "--branch", "main")
            )
            self.assert_ok(self.run_lb(self.repo, "status", task_id, "IN_PROGRESS"))
            self.assert_ok(
                self.run_lb(
                    self.repo,
                    "contract",
                    task_id,
                    f"historical contract {index} " + ("contract-history " * 400),
                )
            )
            head_sha = commit_file(self.repo, f"{task_id}.txt", f"{task_id}\n", task_id)
            self.assert_ok(
                self.run_lb(
                    self.repo,
                    "done",
                    task_id,
                    "--sha",
                    head_sha,
                    "--summary",
                    f"historical summary {index}",
                )
            )

        self.add_task(self.repo, "dependency", "Dependency")
        self.assert_ok(
            self.run_lb(self.repo, "claim", "dependency", "--agent", "dependency-agent", "--branch", "main")
        )
        self.assert_ok(self.run_lb(self.repo, "status", "dependency", "IN_PROGRESS"))
        dependency_contract = ("dependency contract " + ("dependency-context " * 400)).strip()
        self.assert_ok(self.run_lb(self.repo, "contract", "dependency", dependency_contract))
        dependency_sha = commit_file(self.repo, "dependency.txt", "dependency\n", "dependency")
        self.assert_ok(
            self.run_lb(
                self.repo,
                "done",
                "dependency",
                "--sha",
                dependency_sha,
                "--summary",
                "dependency complete",
            )
        )

        self.add_task(self.repo, "feature", "Feature", "--depends-on", "dependency")
        self.assert_ok(self.run_lb(self.repo, "claim", "feature", "--agent", "feature-agent", "--branch", "main"))
        self.assert_ok(self.run_lb(self.repo, "status", "feature", "IN_PROGRESS"))
        self.assert_ok(self.run_lb(self.repo, "note", "feature", "feature implementation note"))
        self.assert_ok(self.run_lb(self.repo, "contract", "feature", "feature contract"))
        feature_sha = commit_file(self.repo, "feature.txt", "feature\n", "feature")
        self.assert_ok(
            self.run_lb(
                self.repo,
                "done",
                "feature",
                "--sha",
                feature_sha,
                "--summary",
                "feature complete",
            )
        )
        target = self.json_result(self.repo, "--json", "get", "feature")

        audit = self.add_audit_cli(target)
        evidence = "long evidence " + ("evidence-detail " * 500)
        scenario = "long scenario " + ("scenario-step " * 500)
        finding = self.json_result(
            self.worktree_a,
            "--json",
            "finding",
            "add",
            audit["id"],
            "--severity",
            "HIGH",
            "--lens",
            "STATE",
            "--claim",
            "feature can diverge",
            "--evidence",
            evidence,
            "--scenario",
            scenario,
            "--required",
            "feature state remains atomic",
        )
        self.finish_audit_cli(audit["id"], feature_sha, "--verified", "feature state")

        self.assert_ok(
            self.run_lb(
                self.worktree_a,
                "fix",
                "add",
                "fix-1",
                "--target",
                "feature",
                "--findings",
                str(finding["id"]),
            )
        )
        self.assert_ok(self.run_lb(self.worktree_a, "claim", "fix-1", "--agent", "fix-agent", "--branch", "agent-a"))
        self.assert_ok(self.run_lb(self.worktree_a, "status", "fix-1", "IN_PROGRESS"))
        fix_sha = commit_file(self.worktree_a, "tests/fix.py", "fixed\n", "fix")
        self.assert_ok(
            self.run_lb(
                self.worktree_a,
                "done",
                "fix-1",
                "--sha",
                fix_sha,
                "--summary",
                "finding fixed",
            )
        )

        self.assert_ok(
            self.run_lb(
                self.worktree_b,
                "verify",
                "add",
                "verify-1",
                "--fix",
                "fix-1",
                "--findings",
                str(finding["id"]),
            )
        )
        self.assert_ok(self.run_lb(self.worktree_b, "claim", "verify-1", "--agent", "verify-agent", "--branch", "agent-b"))
        self.assert_ok(self.run_lb(self.worktree_b, "status", "verify-1", "IN_PROGRESS"))
        verify_sha = commit_file(self.worktree_b, "tests/verify.py", "verified\n", "verify")
        self.assert_ok(
            self.run_lb(
                self.worktree_b,
                "done",
                "verify-1",
                "--sha",
                verify_sha,
                "--summary",
                "verification complete",
            )
        )
        self.assert_ok(
            self.run_lb(
                self.worktree_b,
                "finding",
                "verify",
                str(finding["id"]),
                "--verify",
                "verify-1",
            )
        )
        return {
            "dependency_contract": dependency_contract,
            "feature_sha": feature_sha,
            "finding_id": finding["id"],
            "fix_sha": fix_sha,
            "verify_sha": verify_sha,
        }

    def test_json_list_is_shallow_and_history_independent(self):
        fixture = self.build_json_protocol_fixture()
        before_result = self.assert_ok(self.run_lb(self.repo, "list", "--json"))
        before_size = len(before_result.stdout.encode("utf-8"))
        listed = json.loads(before_result.stdout)

        self.assertEqual(
            set(listed),
            {
                "branch",
                "current_time",
                "filters",
                "project_root",
                "repo_root",
                "schema_version",
                "staleness",
                "summary",
                "tasks",
                "touch_conflicts",
            },
        )
        self.assertNotIn("contracts", listed)
        self.assertNotIn("events", listed)
        self.assertNotIn("last_events", listed)
        self.assertNotIn("findings", listed)
        self.assertNotIn("dependency_verification", listed)
        for task in listed["tasks"]:
            for field in (
                "entries",
                "contracts",
                "findings",
                "related_findings",
                "events",
                "dependency_tasks",
                "dependency_contracts",
                "inherited_contracts",
                "dependency_verification",
                "audit_result",
                "review_mode",
            ):
                self.assertNotIn(field, task, f"{task['id']} unexpectedly contains {field}")
        feature = next(task for task in listed["tasks"] if task["id"] == "feature")
        self.assertEqual(
            set(feature),
            {
                "active",
                "age",
                "age_seconds",
                "base_sha",
                "block_reason",
                "branch",
                "dependencies",
                "deps_verification",
                "head_sha",
                "id",
                "owner",
                "review_summary",
                "stale",
                "stale_level",
                "status",
                "summary",
                "task_type",
                "title",
                "touch_conflicts",
                "warning",
            },
        )
        self.assertEqual(feature["dependencies"], ["dependency"])
        self.assertEqual(feature["deps_verification"]["verified"], 1)
        self.assertEqual(feature["review_summary"]["state"], "CLEAN")
        audit = next(task for task in listed["tasks"] if task["id"] == "audit-a")
        self.assertEqual(audit["review_summary"]["target_task_id"], "feature")
        self.assertEqual(audit["review_summary"]["task_type"], "AUDIT")
        self.assertEqual(audit["review_summary"]["status"], "DONE")

        for index in range(3, 6):
            task_id = f"history-{index}"
            self.add_task(self.repo, task_id, f"Historical task {index}")
            self.assert_ok(
                self.run_lb(self.repo, "claim", task_id, "--agent", "history-agent", "--branch", "main")
            )
            self.assert_ok(self.run_lb(self.repo, "status", task_id, "IN_PROGRESS"))
            self.assert_ok(
                self.run_lb(
                    self.repo,
                    "contract",
                    task_id,
                    f"new historical contract {index} " + ("more-history " * 1200),
                )
            )
            head_sha = commit_file(self.repo, f"{task_id}.txt", f"{task_id}\n", task_id)
            self.assert_ok(
                self.run_lb(
                    self.repo,
                    "done",
                    task_id,
                    "--sha",
                    head_sha,
                    "--summary",
                    f"historical summary {index}",
                )
            )

        after_result = self.assert_ok(self.run_lb(self.repo, "list", "--json"))
        after_size = len(after_result.stdout.encode("utf-8"))
        self.assertLess(after_size - before_size, 5000, (before_size, after_size))
        self.assertLess(after_size, before_size * 2)
        self.assertGreater(fixture["finding_id"], 0)

    def test_json_get_has_canonical_detail_collections(self):
        fixture = self.build_json_protocol_fixture()
        detail = self.json_result(self.repo, "get", "feature", "--json")

        self.assertEqual(
            set(detail),
            {
                "active",
                "age",
                "age_seconds",
                "base_sha",
                "block_reason",
                "branch",
                "completed_at",
                "contracts",
                "created_at",
                "dependencies",
                "dependency_contracts",
                "dependency_tasks",
                "dependency_verification",
                "deps_verification",
                "events",
                "findings",
                "fix_head_sha",
                "fix_task_id",
                "follow_up_to",
                "head_sha",
                "id",
                "lens",
                "notes",
                "owner",
                "review",
                "review_group_id",
                "review_slot",
                "review_state",
                "review_stale",
                "reviewed_target_head_sha",
                "schema_version",
                "scope_paths",
                "stale",
                "stale_level",
                "status",
                "summary",
                "target_base_sha",
                "target_head_sha",
                "target_task_id",
                "task_type",
                "title",
                "touch_conflicts",
                "touches",
                "updated_at",
                "verified_contract_ids",
                "warning",
                "current_target_head_sha",
            },
        )
        self.assertEqual(detail["dependencies"], ["dependency"])
        self.assertEqual(
            set(detail["dependency_tasks"]["dependency"]),
            {"id", "status", "head_sha", "summary"},
        )
        self.assertEqual(detail["dependency_tasks"]["dependency"]["head_sha"], detail["dependency_verification"][0]["head_sha"])
        self.assertEqual(detail["dependency_contracts"]["dependency"][0]["body"], fixture["dependency_contract"])
        self.assertEqual(detail["contracts"][0]["body"], "feature contract")
        self.assertEqual([note["body"] for note in detail["notes"]], ["feature implementation note"])
        self.assertEqual([finding["id"] for finding in detail["findings"]], [fixture["finding_id"]])
        finding = detail["findings"][0]
        self.assertTrue(len(finding["evidence"]) > 5000)
        self.assertEqual(detail["review"]["finding_ids"], [fixture["finding_id"]])
        self.assertNotIn("findings", detail["review"])
        self.assertNotIn("events", detail["review"])
        self.assertEqual(detail["review"]["state"], "CLEAN")
        for field in (
            "entries",
            "related_findings",
            "inherited_contracts",
            "review_mode",
            "review_result",
            "fix_findings",
            "verification_results",
        ):
            self.assertNotIn(field, detail)
        self.assertTrue(any(event["type"] == "CONTRACT" for event in detail["events"]))
        self.assertTrue(all("body" not in event["payload"] for event in detail["events"]))

        for task_id, expected_type in (("audit-a", "AUDIT"), ("fix-1", "FIX"), ("verify-1", "VERIFY")):
            review_task = self.json_result(self.repo, "--json", "get", task_id)
            self.assertEqual(review_task["task_type"], expected_type)
            self.assertEqual(review_task["target_task_id"], "feature")
            self.assertEqual([item["id"] for item in review_task["findings"]], [fixture["finding_id"]])
            self.assertNotIn("related_findings", review_task)
            self.assertNotIn("fix_findings", review_task)
            self.assertNotIn("verification_results", review_task)

        swarm = self.json_result(self.repo, "--json", "swarm")
        self.assertTrue(any(item["body"] == fixture["dependency_contract"] for item in swarm["contracts"]))
        self.assertTrue(any(len(item["evidence"]) > 5000 for item in swarm["findings"]))
        self.assertTrue(any(event["type"] == "CONTRACT" and "body" in event["payload"] for event in swarm["events"]))
        historical = next(item for item in swarm["tasks"] if item["id"] == "history-0")
        self.assertTrue(any(event["type"] == "CONTRACT" and "body" in event["payload"] for event in historical["events"]))

        ready = self.json_result(self.repo, "--json", "ready")
        self.assertIn("tasks", ready)
        self.assertTrue(self.json_result(self.repo, "--json", "verify-integration", "feature")["ok"])
        self.assertTrue(self.json_result(self.repo, "--json", "review", "gate", "feature")["ok"])

    def test_review_fixed_without_verify_still_fails_gate(self):
        target, finding, _ = self.create_fixed_finding_cli()

        persisted = next(
            item
            for item in self.json_result(self.repo, "--json", "finding", "list", "--target", target["id"])["findings"]
            if item["id"] == finding["id"]
        )
        _, gate = self.json_result_any_status(self.repo, "--json", "review", "gate", target["id"])

        self.assertEqual(persisted["status"], "FIXED")
        self.assertFalse(gate["ok"])
        self.assertTrue(any("FIXED" in reason for reason in gate["reasons"]))

    def test_review_verified_finding_passes_gate(self):
        target, finding, fix_sha = self.create_fixed_finding_cli()
        verify_sha = self.add_verify_cli(target, finding, fix_sha)
        self.assert_ok(
            self.run_lb(
                self.worktree_b,
                "finding",
                "verify",
                str(finding["id"]),
                "--verify",
                "verify-1",
            )
        )

        persisted = next(
            item
            for item in self.json_result(self.repo, "--json", "finding", "list", "--target", target["id"])["findings"]
            if item["id"] == finding["id"]
        )
        _, gate = self.json_result_any_status(self.repo, "--json", "review", "gate", target["id"])

        self.assertEqual(persisted["status"], "VERIFIED")
        self.assertEqual(persisted["verified_by_task_id"], "verify-1")
        self.assertEqual(self.json_result(self.repo, "--json", "get", "verify-1")["fix_head_sha"], fix_sha)
        self.assertEqual(self.json_result(self.repo, "--json", "get", "verify-1")["head_sha"], verify_sha)
        self.assertTrue(gate["ok"], gate)

    def test_review_events_reconstruct_the_commit_chain(self):
        target, finding, fix_sha = self.create_fixed_finding_cli()
        self.add_verify_cli(target, finding, fix_sha)
        self.assert_ok(self.run_lb(self.worktree_b, "finding", "verify", str(finding["id"]), "--verify", "verify-1"))
        self.assertEqual(
            self.json_result_any_status(self.repo, "--json", "review", "gate", target["id"])[1]["ok"],
            True,
        )

        events = self.json_result(self.repo, "--json", "swarm")["events"]
        event_types = {event["type"] for event in events}
        self.assertTrue(
            {
                "AUDIT_CREATED",
                "AUDIT_DONE",
                "FINDING_CREATED",
                "FINDING_FIXED",
                "FINDING_VERIFIED",
                "REVIEW_GATE_PASS",
            }.issubset(event_types)
        )
        finding_created = next(event for event in events if event["type"] == "FINDING_CREATED")
        self.assertEqual(finding_created["payload"]["target_head_sha"], target["head_sha"])
        finding_fixed = next(event for event in events if event["type"] == "FINDING_FIXED")
        self.assertEqual(finding_fixed["payload"]["fix_head_sha"], fix_sha)

    def test_review_failed_verification_reopens_and_fails_gate(self):
        target, finding, fix_sha = self.create_fixed_finding_cli()
        self.add_verify_cli(target, finding, fix_sha)
        reopened = self.run_lb(
            self.worktree_b,
            "finding",
            "reopen",
            str(finding["id"]),
            "--verify",
            "verify-1",
            "--reason",
            "fix does not cover the race",
        )
        self.assert_ok(reopened)

        persisted = next(
            item
            for item in self.json_result(self.repo, "--json", "finding", "list", "--target", target["id"])["findings"]
            if item["id"] == finding["id"]
        )
        _, gate = self.json_result_any_status(self.repo, "--json", "review", "gate", target["id"])

        self.assertEqual(persisted["status"], "REOPENED")
        self.assertEqual(persisted["verification_reason"], "fix does not cover the race")
        self.assertFalse(gate["ok"])
        self.assertTrue(any("REOPENED" in reason for reason in gate["reasons"]))

    def test_review_becomes_stale_when_target_head_changes(self):
        target = self.add_done_feature_cli()
        audit = self.add_audit_cli(target)
        self.finish_audit_cli(audit["id"], target["head_sha"], "--verified", "scope checked")
        new_head = commit_file(self.repo, "new-target-code.py", "changed\n", "new target head")

        state = self.json_result(self.repo, "--json", "review", "state", target["id"])
        _, gate = self.json_result_any_status(self.repo, "--json", "review", "gate", target["id"])

        self.assertEqual(state["state"], "STALE")
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["target_head_sha"], new_head)
        self.assertTrue(any("stale" in reason.lower() for reason in gate["reasons"]))

    def test_audit_add_rejects_target_without_valid_head(self):
        self.add_task(self.repo, "unfinished", "Unfinished feature")
        result = self.run_lb(
            self.repo,
            "audit",
            "add",
            "audit-invalid",
            "--target",
            "unfinished",
            "--lens",
            "STATE",
            "--group",
            "group-1",
            "--slot",
            "A",
            "--scope",
            "src/main.py",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no valid commit", result.stderr.lower())
        self.assertNotIn(
            "audit-invalid",
            {task["id"] for task in self.json_result(self.repo, "--json", "list")["tasks"]},
        )

    def test_audit_completion_rejects_production_diff(self):
        target = self.add_done_feature_cli()
        audit = self.add_audit_cli(target)
        self.assert_ok(git(self.worktree_a, "merge", "--ff-only", "main"))
        changed_head = commit_file(self.worktree_a, "src/audit-change.py", "forbidden\n", "audit changed production")

        result = self.run_lb(
            self.worktree_a,
            "done",
            audit["id"],
            "--sha",
            changed_head,
            "--summary",
            "should be rejected",
            "--verified",
            "scope checked",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("read-only", result.stderr.lower())
        self.assertEqual(self.json_result(self.repo, "--json", "get", audit["id"])["status"], "IN_PROGRESS")

    def test_verify_completion_rejects_production_diff(self):
        target, finding, fix_sha = self.create_fixed_finding_cli()
        self.assert_ok(
            self.run_lb(
                self.worktree_b,
                "verify",
                "add",
                "verify-1",
                "--fix",
                "fix-1",
                "--findings",
                str(finding["id"]),
            )
        )
        self.assert_ok(
            self.run_lb(
                self.worktree_b,
                "claim",
                "verify-1",
                "--agent",
                "verify-agent",
                "--branch",
                "agent-b",
            )
        )
        self.assert_ok(self.run_lb(self.worktree_b, "status", "verify-1", "IN_PROGRESS"))
        changed_head = commit_file(
            self.worktree_b,
            "src/verify-change.py",
            "forbidden\n",
            "verify changed production",
        )

        result = self.run_lb(
            self.worktree_b,
            "done",
            "verify-1",
            "--sha",
            changed_head,
            "--summary",
            "should be rejected",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("read-only", result.stderr.lower())
        self.assertEqual(self.json_result(self.repo, "--json", "get", "verify-1")["status"], "IN_PROGRESS")

    def test_independent_audits_keep_findings_and_provenance_separate(self):
        target = self.add_done_feature_cli()
        audit_a = self.add_audit_cli(target, "audit-a", "A")
        audit_b = self.add_audit_cli(target, "audit-b", "B")
        finding_a = self.add_finding_cli(audit_a["id"], claim="A sees a state race")
        finding_b = self.json_result(
            self.worktree_b,
            "--json",
            "finding",
            "add",
            audit_b["id"],
            "--severity",
            "MEDIUM",
            "--lens",
            "FAILURE",
            "--claim",
            "B sees a failure path",
            "--evidence",
            "B evidence",
            "--scenario",
            "B scenario",
            "--required",
            "B property",
        )

        audit_a_view = self.json_result(self.repo, "--json", "get", "audit-a")
        audit_b_view = self.json_result(self.repo, "--json", "get", "audit-b")
        all_findings = self.json_result(self.repo, "--json", "finding", "list", "--target", target["id"])

        self.assertEqual([item["id"] for item in audit_a_view["findings"]], [finding_a["id"]])
        self.assertEqual([item["id"] for item in audit_b_view["findings"]], [finding_b["id"]])
        self.assertEqual(
            {item["reviewer"] for item in all_findings["findings"]},
            {"reviewer-a", "reviewer-b"},
        )
        self.assertEqual(
            {item["audit_task_id"] for item in all_findings["findings"]},
            {"audit-a", "audit-b"},
        )
        self.assertTrue(all(item["target_head_sha"] == target["head_sha"] for item in all_findings["findings"]))

    def test_incomplete_reviewers_cannot_see_sibling_findings(self):
        target = self.add_done_feature_cli()
        audit_a = self.add_audit_cli(target, "audit-a", "A")
        audit_b = self.add_audit_cli(target, "audit-b", "B")
        finding_a = self.add_finding_cli(audit_a["id"])
        finding_b = self.json_result(
            self.worktree_b,
            "--json",
            "finding",
            "add",
            audit_b["id"],
            "--severity",
            "MEDIUM",
            "--lens",
            "FAILURE",
            "--claim",
            "B sees a failure path",
            "--evidence",
            "B evidence",
            "--scenario",
            "B scenario",
            "--required",
            "B property",
        )

        visible_a = self.json_result(
            self.worktree_a, "--json", "finding", "list", "--target", target["id"]
        )
        visible_b = self.json_result(
            self.worktree_b, "--json", "finding", "list", "--target", target["id"]
        )

        self.assertEqual([finding["id"] for finding in visible_a["findings"]], [finding_a["id"]])
        self.assertEqual([finding["id"] for finding in visible_b["findings"]], [finding_b["id"]])

        self.finish_audit_cli(audit_a["id"], target["head_sha"], "--verified", "scope checked")
        visible_after_done = self.json_result(
            self.worktree_a, "--json", "finding", "list", "--target", target["id"]
        )
        self.assertEqual(
            {finding["id"] for finding in visible_after_done["findings"]},
            {finding_a["id"], finding_b["id"]},
        )

    def test_unverified_critical_scope_cannot_look_clean(self):
        target = self.add_done_feature_cli()
        audit = self.add_audit_cli(target, scope="src/critical.py")
        self.finish_audit_cli(
            audit["id"],
            target["head_sha"],
            "--verified",
            "state transition",
            "--unverified",
            "critical: transaction atomicity",
        )

        state = self.json_result(self.repo, "--json", "review", "state", target["id"])
        _, gate = self.json_result_any_status(self.repo, "--json", "review", "gate", target["id"])

        self.assertEqual(state["state"], "PARTIAL")
        self.assertFalse(gate["ok"])
        self.assertTrue(any("UNVERIFIED" in reason for reason in gate["reasons"]))

    def test_get_feature_and_list_hide_full_review_history(self):
        target = self.add_done_feature_cli()
        audit = self.add_audit_cli(target)
        finding = self.add_finding_cli(audit["id"])
        self.finish_audit_cli(audit["id"], target["head_sha"], "--verified", "scope checked")

        feature = self.json_result(self.repo, "--json", "get", target["id"])
        listed = self.json_result(self.repo, "--json", "list")
        audit_view = self.json_result(self.repo, "--json", "get", audit["id"])

        self.assertEqual(feature["review_state"], "FINDINGS")
        self.assertEqual(feature["review"]["audits"][0]["id"], audit["id"])
        self.assertEqual(feature["review"]["finding_ids"], [finding["id"]])
        self.assertEqual(feature["findings"][0]["id"], finding["id"])
        task = next(item for item in listed["tasks"] if item["id"] == target["id"])
        self.assertNotIn("findings", task)
        self.assertNotIn("entries", task)
        self.assertEqual(audit_view["scope_paths"], ["src/main.py"])
        self.assertEqual(audit_view["target_head_sha"], target["head_sha"])

    def test_invalid_review_relations_are_rejected(self):
        target = self.add_done_feature()
        context = self.context()
        with self.assertRaises(LIGHTBOARD.LightboardError):
            LIGHTBOARD.add_task(
                context,
                "bad-audit",
                "bad audit",
                task_type="AUDIT",
                target_task_id="feature",
                target_base_sha=target["base_sha"],
                target_head_sha=target["base_sha"],
                lens="STATE",
            )
        with self.assertRaises(LIGHTBOARD.LightboardError):
            LIGHTBOARD.add_task(
                context,
                "bad-verify",
                "bad verify",
                task_type="VERIFY",
                target_task_id="feature",
                target_base_sha=target["base_sha"],
                target_head_sha=target["head_sha"],
                finding_ids=[],
            )
        self.add_audit("audit-a", target, lens="STATE")
        with self.assertRaises(LIGHTBOARD.LightboardError):
            LIGHTBOARD.create_finding(
                context,
                "audit-a",
                reviewer="reviewer-a",
                severity="INVALID",
                lens="STATE",
                claim="claim",
                evidence="evidence",
                scenario="scenario",
                required_property="property",
            )

    def test_legacy_review_status_is_not_available_to_review_tasks(self):
        target = self.add_done_feature()
        context = self.context()
        self.add_audit("audit-a", target, lens="GENERAL")
        with self.assertRaises(LIGHTBOARD.LightboardError):
            LIGHTBOARD.review_task(context, "audit-a", "legacy status must stay feature-only")

    def test_worktrees_share_one_board_from_nested_directories(self):
        self.add_task(self.worktree_a / "nested", "task-a", "Shared task")

        tasks = self.json_result(self.worktree_b / "nested", "--json", "list")

        self.assertEqual([task["id"] for task in tasks["tasks"]], ["task-a"])
        self.assertEqual(self.board_path(self.worktree_a), self.board_path(self.worktree_b))

    @unittest.skipIf(os.name == "nt", "POSIX launcher is not executable on Windows")
    def test_posix_launcher_runs_from_nested_directory(self):
        result = self.run_launcher(self.worktree_a / "nested", "add", "task-a", "--title", "Shared task")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([task["id"] for task in self.json_result(self.worktree_b, "--json", "list")["tasks"]], ["task-a"])

    @unittest.skipIf(os.name == "nt", "POSIX symlinks are not portable to Windows")
    def test_posix_launcher_runs_through_external_symlink(self):
        launcher_link = Path(self.temp_dir.name) / "bin" / "lb"
        launcher_link.parent.mkdir()
        launcher_link.symlink_to(POSIX_LAUNCHER)

        result = subprocess.run(
            [str(launcher_link), "add", "task-a", "--title", "Shared task"],
            cwd=self.worktree_a / "nested",
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([task["id"] for task in self.json_result(self.worktree_b, "--json", "list")["tasks"]], ["task-a"])

    def test_windows_launcher_targets_lightboard_script(self):
        try:
            launcher = WINDOWS_LAUNCHER.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            self.fail(f"Windows launcher is missing: {exc}")

        self.assertIn("lightboard.py", launcher)

    def test_help_documents_json_output(self):
        result = self.run_lb(self.repo, "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--json", result.stdout)

    def test_json_mutation_output_has_canonical_schema_and_no_stderr(self):
        result = self.run_lb(self.repo, "--json", "add", "task-a", "--title", "JSON task")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {
                "finding_ids": [],
                "fix_head_sha": None,
                "fix_task_id": None,
                "follow_up_to": None,
                "message": "added task-a",
                "ok": True,
                "review_group_id": None,
                "review_slot": None,
                "schema_version": 3,
                "scope_paths": [],
                "target_base_sha": None,
                "target_head_sha": None,
                "target_task_id": None,
                "task_id": "task-a",
                "task_type": "FEATURE",
            },
        )

    def test_json_error_uses_exit_code_one_and_canonical_error_shape(self):
        result = self.run_lb(self.repo, "--json", "get", "missing")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], 3)
        self.assertFalse(payload["ok"])
        self.assertIn("task not found: missing", payload["error"])

    def test_import_has_no_database_or_cli_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            module_name = "lightboard_import_side_effect_test"
            spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            previous_cwd = Path.cwd()
            os.chdir(directory)
            try:
                spec.loader.exec_module(module)
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertTrue(callable(module.main))

    def test_argparse_allows_literal_json_text_with_option_terminator(self):
        self.add_task(self.repo, "task-a", "Literal text")

        self.assert_ok(self.run_lb(self.repo, "note", "task-a", "--", "--json"))
        self.assert_ok(self.run_lb(self.repo, "contract", "task-a", "--", "--json"))

        task = self.json_result(self.repo, "--json", "get", "task-a")
        self.assertEqual([note["body"] for note in task["notes"]], ["--json"])
        self.assertEqual([contract["body"] for contract in task["contracts"]], ["--json"])

    def test_generic_status_cannot_bypass_lifecycle(self):
        self.add_task(self.repo, "todo", "TODO task")
        for status in ("DONE", "CLAIMED", "TODO"):
            result = self.run_lb(self.repo, "status", "todo", status)
            self.assertNotEqual(result.returncode, 0)

        todo = self.json_result(self.repo, "--json", "get", "todo")
        self.assertEqual(todo["status"], "TODO")
        self.assertIsNone(todo["owner"])
        self.assertIsNone(todo["branch"])
        self.assertIsNone(todo["head_sha"])
        self.assertEqual(todo["summary"], "")
        self.assertIsNone(todo["completed_at"])

        self.add_task(self.repo, "claimed", "Claimed task")
        self.assert_ok(self.run_lb(self.repo, "claim", "claimed", "--agent", "agent-a", "--branch", "branch-a"))
        result = self.run_lb(self.repo, "status", "claimed", "TODO")
        self.assertNotEqual(result.returncode, 0)

        claimed = self.json_result(self.repo, "--json", "get", "claimed")
        self.assertEqual((claimed["status"], claimed["owner"], claimed["branch"]), ("CLAIMED", "agent-a", "branch-a"))

        self.assert_ok(self.run_lb(self.repo, "status", "claimed", "IN_PROGRESS"))

    def test_every_entry_updates_task_with_entry_timestamp(self):
        self.add_task(self.repo, "task-a", "Timestamped task")
        before = self.json_result(self.repo, "--json", "get", "task-a")["updated_at"]

        self.assert_ok(self.run_lb(self.repo, "note", "task-a", "note"))
        after_note = self.json_result(self.repo, "--json", "get", "task-a")
        self.assertNotEqual(after_note["updated_at"], before)
        self.assertEqual(after_note["updated_at"], after_note["notes"][-1]["created_at"])

        self.assert_ok(self.run_lb(self.repo, "contract", "task-a", "contract"))
        after_contract = self.json_result(self.repo, "--json", "get", "task-a")
        self.assertNotEqual(after_contract["updated_at"], after_note["updated_at"])
        self.assertEqual(after_contract["updated_at"], after_contract["contracts"][-1]["created_at"])

    def test_claim_is_atomic_across_processes(self):
        self.add_task(self.worktree_a, "task-a", "Claim me")
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        commands = [
            [sys.executable, str(SCRIPT), "claim", "task-a", "--agent", "agent-a", "--branch", "branch-a"],
            [sys.executable, str(SCRIPT), "claim", "task-a", "--agent", "agent-b", "--branch", "branch-b"],
        ]
        processes = [
            subprocess.Popen(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            for command, cwd in zip(commands, [self.worktree_a, self.worktree_b])
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate()
            results.append((process.returncode, stdout, stderr))

        self.assertEqual(sorted(result[0] for result in results), [0, 1])
        task = self.json_result(self.repo, "--json", "get", "task-a")
        self.assertIn(task["owner"], {"agent-a", "agent-b"})
        self.assertIn(task["branch"], {"branch-a", "branch-b"})

    def test_ready_waits_for_all_dependencies(self):
        self.add_task(self.repo, "base", "Base")
        self.add_task(self.repo, "child", "Child", "--depends-on", "base")

        self.assertEqual([task["id"] for task in self.json_result(self.repo, "--json", "ready")["tasks"]], ["base"])

        self.assert_ok(self.run_lb(self.repo, "claim", "base", "--agent", "agent-a", "--branch", "main"))
        self.assert_ok(self.run_lb(self.repo, "status", "base", "IN_PROGRESS"))
        base_sha = commit_file(self.repo, "base.txt", "base\n", "base")
        self.assert_ok(self.run_lb(self.repo, "done", "base", "--sha", base_sha, "--summary", "finished"))

        self.assertEqual([task["id"] for task in self.json_result(self.repo, "ready", "--json")["tasks"]], ["child"])

    def test_entries_and_completion_are_visible_between_worktrees(self):
        self.add_task(self.worktree_a, "task-a", "Shared task")
        self.assert_ok(self.run_lb(self.worktree_a, "claim", "task-a", "--agent", "agent-a", "--branch", "branch-a"))
        self.assert_ok(self.run_lb(self.worktree_a, "status", "task-a", "IN_PROGRESS"))
        self.assert_ok(self.run_lb(self.worktree_a, "note", "task-a", "shared note"))
        self.assert_ok(self.run_lb(self.worktree_a, "contract", "task-a", "use stable API"))
        self.assert_ok(self.run_lb(self.worktree_a, "block", "task-a", "waiting"))
        self.assert_ok(self.run_lb(self.worktree_a, "status", "task-a", "IN_PROGRESS"))
        self.assert_ok(self.run_lb(self.worktree_a, "review", "task-a", "check API"))

        task = self.json_result(self.worktree_b, "--json", "get", "task-a")
        self.assertEqual(
            [note["body"] for note in task["notes"]],
            ["shared note"],
        )
        self.assertEqual([contract["body"] for contract in task["contracts"]], ["use stable API"])
        self.assertIsNone(task["block_reason"])
        self.assertEqual(task["legacy_review"]["body"], "check API")
        self.assertEqual(task["status"], "REVIEW")

        self.assert_ok(self.run_lb(self.worktree_a, "review", "task-a", "resolve API"))
        head_sha = commit_file(self.worktree_a, "shared.txt", "shared\n", "shared")
        self.assert_ok(self.run_lb(self.worktree_b, "done", "task-a", "--sha", head_sha, "--summary", "merged"))
        task = self.json_result(self.worktree_a, "--json", "get", "task-a")
        self.assertEqual((task["head_sha"], task["summary"], task["status"]), (head_sha, "merged", "DONE"))

    def test_dependency_contracts_are_returned(self):
        self.add_task(self.repo, "base", "Base")
        self.add_task(self.repo, "child", "Child", "--depends-on", "base")
        self.assert_ok(self.run_lb(self.repo, "claim", "base", "--agent", "agent-a", "--branch", "main"))
        self.assert_ok(self.run_lb(self.repo, "status", "base", "IN_PROGRESS"))
        self.assert_ok(self.run_lb(self.repo, "contract", "base", "shared contract"))
        base_sha = commit_file(self.repo, "base.txt", "base\n", "base")
        self.assert_ok(self.run_lb(self.repo, "done", "base", "--sha", base_sha, "--summary", "base complete"))

        task = self.json_result(self.worktree_b, "--json", "get", "child")

        self.assertEqual(task["dependency_contracts"]["base"][0]["body"], "shared contract")
        self.assertEqual(task["dependency_tasks"]["base"]["head_sha"], base_sha)
        self.assertEqual(task["dependency_tasks"]["base"]["summary"], "base complete")

    def test_old_board_schema_migrates_without_losing_data(self):
        board = self.board_path(self.repo)
        board.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(board)
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                owner TEXT,
                status TEXT NOT NULL,
                branch TEXT,
                base_sha TEXT,
                head_sha TEXT,
                dependencies TEXT NOT NULL DEFAULT '[]',
                summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO tasks VALUES ('legacy', 'Legacy task', 'agent-a', 'IN_PROGRESS', 'branch-a', 'base', NULL, '[]', '', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z', NULL);
            INSERT INTO entries (task_id, kind, body, created_at) VALUES ('legacy', 'CONTRACT', 'keep me', '2026-09-01T00:01:00Z');
            """
        )
        connection.commit()
        connection.close()

        snapshot = self.json_result(self.repo, "--json", "swarm")

        self.assertEqual(snapshot["schema_version"], 3)
        task = snapshot["tasks"][0]
        self.assertEqual((task["id"], task["title"], task["owner"]), ("legacy", "Legacy task", "agent-a"))
        self.assertEqual(task["contracts"][0]["body"], "keep me")
        self.assertEqual(task["touches"], [])
        migrated = sqlite3.connect(board)
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(tasks)")}
        self.assertTrue(
            {
                "task_type",
                "follow_up_to",
                "touches",
                "block_reason",
                "started_at",
                "verified_contract_ids",
                "target_task_id",
                "target_base_sha",
                "target_head_sha",
                "reviewed_target_head_sha",
                "lens",
                "review_group_id",
                "review_slot",
                "fix_task_id",
                "fix_head_sha",
            }
            <= columns
        )
        self.assertIsNotNone(migrated.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'").fetchone())
        self.assertIsNotNone(migrated.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'findings'").fetchone())
        self.assertIsNotNone(migrated.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_findings'").fetchone())
        self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 3)
        migrated.close()

    def test_migration_preserves_legacy_review_status_and_event_history(self):
        board = self.board_path(self.repo)
        board.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(board)
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                owner TEXT,
                status TEXT NOT NULL,
                branch TEXT,
                base_sha TEXT,
                head_sha TEXT,
                dependencies TEXT NOT NULL DEFAULT '[]',
                summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                follow_up_to TEXT,
                touches TEXT NOT NULL DEFAULT '[]',
                block_reason TEXT,
                started_at TEXT,
                verified_contract_ids TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO tasks VALUES ('legacy-review', 'Legacy review', 'agent-a', 'REVIEW', 'branch-a', NULL, NULL, '[]', '', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z', NULL, NULL, '[]', NULL, NULL, '[]');
            INSERT INTO entries (task_id, kind, body, created_at) VALUES ('legacy-review', 'REVIEW_FINDING', 'keep review history', '2026-09-01T00:01:00Z');
            INSERT INTO events (task_id, type, payload, created_at) VALUES ('legacy-review', 'REVIEW', '{"finding":"keep review history"}', '2026-09-01T00:01:00Z');
            """
        )
        connection.commit()
        connection.close()

        task = self.json_result(self.repo, "--json", "get", "legacy-review")
        self.assertEqual((task["task_type"], task["status"]), ("FEATURE", "REVIEW"))
        self.assertEqual(task["legacy_review"]["body"], "keep review history")
        self.assertEqual(task["events"][0]["type"], "REVIEW")

    def test_done_rejects_invalid_and_unrelated_sha_without_mutation(self):
        self.add_task(self.repo, "task-a", "SHA task")
        self.assert_ok(self.run_lb(self.repo, "claim", "task-a", "--agent", "agent-a", "--branch", "main"))
        self.assert_ok(self.run_lb(self.repo, "status", "task-a", "IN_PROGRESS"))

        invalid = self.run_lb(self.repo, "done", "task-a", "--sha", "not-a-commit", "--summary", "bad")
        self.assertNotEqual(invalid.returncode, 0)
        unchanged = self.json_result(self.repo, "--json", "get", "task-a")
        self.assertEqual(unchanged["status"], "IN_PROGRESS")
        self.assertIsNone(unchanged["head_sha"])

        unrelated = unrelated_commit(self.repo)
        result = self.run_lb(self.repo, "done", "task-a", "--sha", unrelated, "--summary", "unrelated")
        self.assertNotEqual(result.returncode, 0)
        unchanged = self.json_result(self.repo, "--json", "get", "task-a")
        self.assertEqual(unchanged["status"], "IN_PROGRESS")
        self.assertFalse(any(event["type"] == "DONE" for event in unchanged["events"]))

    def test_dependency_is_ready_only_after_verified_done(self):
        self.add_task(self.repo, "base", "Base")
        self.add_task(self.repo, "child", "Child", "--depends-on", "base")
        self.assert_ok(self.run_lb(self.repo, "claim", "base", "--agent", "agent-a", "--branch", "main"))
        self.assert_ok(self.run_lb(self.repo, "status", "base", "IN_PROGRESS"))

        base_sha = commit_file(self.repo, "base.txt", "base\n", "base")
        self.assert_ok(self.run_lb(self.repo, "done", "base", "--sha", base_sha, "--summary", "finished"))

        ready = self.json_result(self.repo, "--json", "ready")
        self.assertEqual([task["id"] for task in ready["tasks"]], ["child"])
        dependency = self.json_result(self.repo, "--json", "get", "child")["dependency_verification"][0]
        self.assertTrue(dependency["verified"])

    def test_verify_integration_checks_direct_dependency_ancestors(self):
        self.add_task(self.repo, "base", "Base")
        self.add_task(self.repo, "merge", "Merge", "--depends-on", "base")
        self.assert_ok(self.run_lb(self.worktree_a, "claim", "base", "--agent", "agent-a", "--branch", "agent-a"))
        self.assert_ok(self.run_lb(self.worktree_a, "status", "base", "IN_PROGRESS"))
        base_sha = commit_file(self.worktree_a, "base.txt", "base\n", "base")
        self.assert_ok(self.run_lb(self.worktree_a, "done", "base", "--sha", base_sha, "--summary", "base complete"))

        failed = self.run_lb(self.repo, "verify-integration", "merge")
        self.assertNotEqual(failed.returncode, 0)
        failed_json = json.loads(self.run_lb(self.repo, "--json", "verify-integration", "merge").stdout)
        self.assertFalse(failed_json["ok"])
        self.assertFalse(failed_json["dependencies"][0]["integrated"])

        self.assert_ok(git(self.repo, "merge", "--ff-only", "agent-a"))
        passed = self.json_result(self.repo, "--json", "verify-integration", "merge")
        self.assertTrue(passed["ok"])
        self.assertTrue(passed["dependencies"][0]["integrated"])

    def test_stale_thresholds_and_force_reclaim(self):
        self.add_task(self.repo, "task-a", "Stale task")
        self.assert_ok(self.run_lb(self.repo, "claim", "task-a", "--agent", "old", "--branch", "old", "--touches", "src/a.py"))
        board = self.board_path(self.repo)
        connection = sqlite3.connect(board)
        connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp_ago(minutes=31), "task-a"))
        connection.commit()
        connection.close()
        task = next(task for task in self.json_result(self.repo, "--json", "swarm")["tasks"] if task["id"] == "task-a")
        self.assertEqual(task["stale_level"], "warning")
        self.assertFalse(task["stale"])

        fresh = self.run_lb(self.repo, "claim", "task-a", "--agent", "new", "--branch", "new", "--force")
        self.assertNotEqual(fresh.returncode, 0)

        connection = sqlite3.connect(board)
        connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp_ago(hours=3), "task-a"))
        connection.commit()
        connection.close()
        task = next(task for task in self.json_result(self.repo, "--json", "swarm")["tasks"] if task["id"] == "task-a")
        self.assertEqual(task["stale_level"], "stale")
        self.assertTrue(task["stale"])

        reclaimed = self.run_lb(self.repo, "claim", "task-a", "--agent", "new", "--branch", "new", "--force", "--touches", "src/a.py", "src/a.py")
        self.assert_ok(reclaimed)
        task = self.json_result(self.repo, "--json", "get", "task-a")
        self.assertEqual((task["owner"], task["status"], task["touches"]), ("new", "CLAIMED", ["src/a.py"]))
        self.assertEqual(task["events"][-1]["type"], "RECLAIM")

    def test_done_tasks_do_not_count_as_stale_alerts(self):
        self.add_task(self.repo, "done", "Done task")
        self.assert_ok(self.run_lb(self.repo, "claim", "done", "--agent", "owner", "--branch", "done"))
        self.assert_ok(self.run_lb(self.repo, "status", "done", "IN_PROGRESS"))
        sha = commit_file(self.repo, "done.txt", "done\n", "done")
        self.assert_ok(self.run_lb(self.repo, "done", "done", "--sha", sha, "--summary", "done"))
        board = self.board_path(self.repo)
        connection = sqlite3.connect(board)
        connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp_ago(hours=3), "done"))
        connection.commit()
        connection.close()

        snapshot = self.json_result(self.repo, "--json", "swarm")
        task = next(task for task in snapshot["tasks"] if task["id"] == "done")
        self.assertFalse(task["stale"])
        self.assertFalse(task["warning"])
        self.assertEqual(snapshot["summary"]["stale"], 0)

    def test_claim_rejects_touch_outside_repo_and_records_intersection(self):
        self.add_task(self.repo, "a", "A")
        self.add_task(self.repo, "b", "B")
        outside = self.run_lb(self.repo, "claim", "a", "--agent", "agent-a", "--branch", "a", "--touches", "../outside")
        self.assertNotEqual(outside.returncode, 0)
        self.assert_ok(self.run_lb(self.repo, "claim", "a", "--agent", "agent-a", "--branch", "a", "--touches", "src/../shared.kt", "shared.kt"))
        self.assert_ok(self.run_lb(self.repo, "claim", "b", "--agent", "agent-b", "--branch", "b", "--touches", "shared.kt", "--touches", "shared.kt"))

        snapshot = self.json_result(self.repo, "--json", "swarm")
        self.assertEqual(snapshot["touch_conflicts"], [{"path": "shared.kt", "tasks": ["a", "b"]}])
        task_a = next(task for task in snapshot["tasks"] if task["id"] == "a")
        self.assertEqual(task_a["touch_conflicts"], [{"path": "shared.kt", "task_id": "b"}])

    def test_review_and_blocked_transitions_keep_owner_and_current_reason(self):
        self.add_task(self.repo, "task-a", "Lifecycle")
        self.assert_ok(self.run_lb(self.repo, "claim", "task-a", "--agent", "owner", "--branch", "branch"))
        self.assert_ok(self.run_lb(self.repo, "status", "task-a", "IN_PROGRESS"))
        self.assert_ok(self.run_lb(self.repo, "block", "task-a", "waiting for API"))
        blocked = self.json_result(self.repo, "--json", "get", "task-a")
        self.assertEqual((blocked["status"], blocked["owner"], blocked["block_reason"]), ("BLOCKED", "owner", "waiting for API"))
        self.assert_ok(self.run_lb(self.repo, "status", "task-a", "IN_PROGRESS"))
        self.assertIsNone(self.json_result(self.repo, "--json", "get", "task-a")["block_reason"])
        self.assert_ok(self.run_lb(self.repo, "review", "task-a", "fix assertion"))
        review = self.json_result(self.repo, "--json", "get", "task-a")
        self.assertEqual((review["status"], review["owner"], review["legacy_review"]["body"]), ("REVIEW", "owner", "fix assertion"))
        self.assert_ok(self.run_lb(self.repo, "review", "task-a", "fixed"))
        self.assertEqual(self.json_result(self.repo, "--json", "get", "task-a")["status"], "IN_PROGRESS")

    def test_event_log_is_structured_and_mutations_are_atomic(self):
        self.add_task(self.repo, "task-a", "Events")
        self.assert_ok(self.run_lb(self.repo, "claim", "task-a", "--agent", "owner", "--branch", "branch"))
        self.assert_ok(self.run_lb(self.repo, "status", "task-a", "IN_PROGRESS"))
        self.assert_ok(self.run_lb(self.repo, "contract", "task-a", "stable API"))
        self.assert_ok(self.run_lb(self.repo, "block", "task-a", "blocked"))
        events = self.json_result(self.repo, "--json", "get", "task-a")["events"]
        self.assertEqual([event["type"] for event in events], ["CLAIM", "STATUS", "CONTRACT", "BLOCK"])
        self.assertEqual(events[2]["payload"]["contract_id"], 1)
        self.assertNotIn("rendered", events[0])

    def test_done_finalizes_contract_ids_and_is_terminal(self):
        self.add_task(self.repo, "task-a", "Terminal")
        self.assert_ok(self.run_lb(self.repo, "claim", "task-a", "--agent", "owner", "--branch", "branch"))
        self.assert_ok(self.run_lb(self.repo, "status", "task-a", "IN_PROGRESS"))
        self.assert_ok(self.run_lb(self.repo, "contract", "task-a", "first contract"))
        sha = commit_file(self.repo, "terminal.txt", "terminal\n", "terminal")
        done = self.run_lb(self.repo, "done", "task-a", "--sha", sha, "--summary", "finished")
        self.assert_ok(done)
        self.assertIn("verified contracts #1", done.stdout)
        task = self.json_result(self.repo, "--json", "get", "task-a")
        self.assertEqual(task["verified_contract_ids"], [1])
        self.assertTrue(task["contracts"][0]["verified"])
        event_count = len(task["events"])
        for command, text in (("contract", "late"), ("block", "late"), ("review", "late")):
            self.assertNotEqual(self.run_lb(self.repo, command, "task-a", text).returncode, 0)
        self.assertNotEqual(self.run_lb(self.repo, "status", "task-a", "IN_PROGRESS").returncode, 0)
        self.assertEqual(len(self.json_result(self.repo, "--json", "get", "task-a")["events"]), event_count)

    def test_follow_up_does_not_reopen_done_task(self):
        self.add_task(self.repo, "old", "Old")
        self.assert_ok(self.run_lb(self.repo, "claim", "old", "--agent", "owner", "--branch", "branch"))
        self.assert_ok(self.run_lb(self.repo, "status", "old", "IN_PROGRESS"))
        sha = commit_file(self.repo, "old.txt", "old\n", "old")
        self.assert_ok(self.run_lb(self.repo, "done", "old", "--sha", sha, "--summary", "done"))
        self.add_task(self.repo, "follow", "Follow-up", "--follow-up-to", "old")

        old = self.json_result(self.repo, "--json", "get", "old")
        follow = self.json_result(self.repo, "--json", "get", "follow")
        self.assertEqual(old["status"], "DONE")
        self.assertEqual(follow["follow_up_to"], "old")

    def test_json_schema_and_swarm_snapshot_are_canonical(self):
        self.add_task(self.repo, "task-a", "Snapshot")
        listed = self.json_result(self.repo, "--json", "list")
        swarm = self.json_result(self.repo, "--json", "swarm")
        detail = self.json_result(self.repo, "--json", "get", "task-a")
        ready = self.json_result(self.repo, "--json", "ready")
        for response in (listed, swarm, detail, ready):
            self.assertEqual(response["schema_version"], 3)
        self.assertEqual(listed["tasks"][0]["id"], swarm["tasks"][0]["id"])
        self.assertIn("summary", swarm)
        self.assertIn("staleness", swarm)
        self.assertIn("events", swarm)
        self.assertIn("touch_conflicts", swarm)
        self.assertIn("dependency_verification", detail)

        mutation = json.loads(self.assert_ok(self.run_lb(self.repo, "--json", "claim", "task-a", "--agent", "agent-a", "--branch", "main")).stdout)
        self.assertEqual(mutation["schema_version"], 3)

    def test_swarm_sorts_blocked_stale_review_active_and_todo(self):
        for task_id in ("todo", "claimed", "progress", "review", "blocked", "stale"):
            self.add_task(self.repo, task_id, task_id)
        self.assert_ok(self.run_lb(self.repo, "claim", "claimed", "--agent", "owner", "--branch", "claimed"))
        self.assert_ok(self.run_lb(self.repo, "claim", "progress", "--agent", "owner", "--branch", "progress"))
        self.assert_ok(self.run_lb(self.repo, "status", "progress", "IN_PROGRESS"))
        self.assert_ok(self.run_lb(self.repo, "claim", "review", "--agent", "owner", "--branch", "review"))
        self.assert_ok(self.run_lb(self.repo, "status", "review", "IN_PROGRESS"))
        self.assert_ok(self.run_lb(self.repo, "review", "review", "finding"))
        self.assert_ok(self.run_lb(self.repo, "claim", "blocked", "--agent", "owner", "--branch", "blocked"))
        self.assert_ok(self.run_lb(self.repo, "status", "blocked", "IN_PROGRESS"))
        self.assert_ok(self.run_lb(self.repo, "block", "blocked", "waiting"))
        self.assert_ok(self.run_lb(self.repo, "claim", "stale", "--agent", "owner", "--branch", "stale"))
        board = self.board_path(self.repo)
        connection = sqlite3.connect(board)
        connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp_ago(hours=3), "stale"))
        connection.commit()
        connection.close()

        task_ids = [task["id"] for task in self.json_result(self.repo, "--json", "swarm")["tasks"]]
        self.assertEqual(task_ids, ["blocked", "stale", "review", "progress", "claimed", "todo"])

    def test_static_list_filters_sorts_and_has_no_ansi_when_piped(self):
        for task_id in ("done", "todo", "claimed", "review", "blocked"):
            self.add_task(self.repo, task_id, task_id)
        self.assert_ok(self.run_lb(self.repo, "claim", "claimed", "--agent", "agent-a", "--branch", "claimed"))
        self.assert_ok(self.run_lb(self.repo, "claim", "review", "--agent", "agent-a", "--branch", "review"))
        self.assert_ok(self.run_lb(self.repo, "status", "review", "IN_PROGRESS"))
        self.assert_ok(self.run_lb(self.repo, "review", "review", "finding"))
        self.assert_ok(self.run_lb(self.repo, "claim", "blocked", "--agent", "agent-b", "--branch", "blocked"))
        self.assert_ok(self.run_lb(self.repo, "status", "blocked", "IN_PROGRESS"))
        self.assert_ok(self.run_lb(self.repo, "block", "blocked", "waiting"))
        self.assert_ok(self.run_lb(self.repo, "claim", "done", "--agent", "agent-a", "--branch", "done"))
        self.assert_ok(self.run_lb(self.repo, "status", "done", "IN_PROGRESS"))
        sha = commit_file(self.repo, "done.txt", "done\n", "done")
        self.assert_ok(self.run_lb(self.repo, "done", "done", "--sha", sha, "--summary", "done"))

        result = self.run_lb(self.repo, "list", "--active", "--agent", "agent-a", "--limit", "1")
        self.assert_ok(result)
        self.assertNotIn("\x1b[", result.stdout)
        self.assertIn("review", result.stdout.lower())
        self.assertNotIn("\nblocked ", result.stdout.lower().split("── events", 1)[0])

        watched = self.run_lb(self.repo, "list", "--watch", "--limit", "1")
        self.assert_ok(watched)
        self.assertNotIn("\x1b[", watched.stdout)

    def test_readme_documents_merge_ready_workflow(self):
        self.assertTrue(README.exists(), f"missing README: {README}")
        readme = README.read_text(encoding="utf-8")
        for fragment in ("merge", "--depends-on api runtime tests", "ready", "get merge"):
            self.assertIn(fragment, readme)

    def test_non_repository_has_clear_error(self):
        result = self.run_lb(self.outside, "list")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("git repository", result.stderr.lower())
        self.assertNotIn("traceback", result.stderr.lower())

    def test_corrupt_database_has_clear_error(self):
        self.add_task(self.repo, "task-a", "Corrupt me")
        board = self.board_path(self.repo)
        board.write_bytes(b"not a sqlite database")

        result = self.run_lb(self.repo, "list")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("database", result.stderr.lower())
        self.assertNotIn("traceback", result.stderr.lower())

    def test_database_open_error_suggests_sandbox_hint(self):
        import importlib.util

        module_name = "lightboard_for_error_test"
        spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        error = module._database_error(sqlite3.OperationalError("unable to open database file"))

        self.assertIn("unable to open database file", str(error))
        self.assertIn("sandbox", str(error).lower())

    def test_locked_database_has_clear_error(self):
        self.add_task(self.repo, "task-a", "Lock me")
        connection = sqlite3.connect(self.board_path(self.repo), timeout=0)
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = self.run_lb(self.repo, "status", "task-a", "REVIEW")
        finally:
            connection.rollback()
            connection.close()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("locked", result.stderr.lower())
        self.assertNotIn("traceback", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
