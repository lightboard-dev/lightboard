# Lightboard

Lightboard is an open-source orchestration project for AI coding teams. It provides a shared coordination layer for agents working across Git worktrees, keeping tasks, dependencies, ownership, implementation contracts, and code reviews in one place.

The project is built around a local-first architecture with SQLite for persistent state, Git for change tracking, and a command-line interface that can be used by both developers and coding agents.

Lightboard is in early development. Its task coordination and review infrastructure is already implemented, with automated agent execution and team orchestration forming the next stage of development.

## Features

**Task coordination.** A shared task board with atomic claiming, ownership tracking, dependencies, status updates, and execution history. Multiple agents can coordinate work without relying on shared conversation context.

**Git integration.** Commit-based task completion, dependency verification, branch tracking, and integration checks. Lightboard uses Git history to associate completed work with actual repository changes.

**Code review workflows.** Structured Audit → Fix → Verify cycles with independent review assignments, tracked findings, and commit-bound verification. Review gates prevent unresolved findings from being treated as verified work.

**Implementation contracts.** Agents can publish interface contracts, document assumptions, and track affected files. Conflicting file assignments are visible through the task board.

**Local state.** Each repository maintains its own SQLite database, shared across its Git worktrees. Task records, findings, and events remain available between sessions.

**CLI and JSON interface.** Lightboard provides commands for task management, inspection, and reviews, alongside structured JSON output for integration with coding agents and external tooling.

## Getting Started

Requires Python 3.10+ and Git.

Clone the repository and make the `lb` command available on your PATH.

On macOS and Linux:

```bash
git clone https://github.com/lightboard-dev/lightboard.git
cd lightboard

mkdir -p "$HOME/.local/bin"
ln -sfn "$(pwd)/lb" "$HOME/.local/bin/lb"
```

For Windows, use the included `lb.cmd` launcher or invoke `lightboard.py` directly with Python.

Lightboard operates on the Git repository in the current working directory. Switch to the project you want to manage before running the examples below:

```bash
cd /path/to/your/git-project
```

### Task Management

Create and assign a task:

```bash
lb add api --title "Implement API"
lb claim api --agent coder-1 --branch coder-1/api --touches src/api.py
lb status api IN_PROGRESS
```

Record implementation details and contracts:

```bash
lb contract api "API names and payloads are stable"
lb note api "Implementation in progress"
```

Once the changes have been committed:

```bash
lb done api --sha "$(git rev-parse HEAD)" --summary "API implemented"
```

### Dependencies and Integration

Tasks can depend on other tasks, allowing multiple agents to work independently while maintaining an explicit integration order.

```bash
lb add api --title "Implement API"
lb add runtime --title "Implement runtime"
lb add tests --title "Integration tests"
lb add merge --title "Integrate changes" --depends-on api runtime tests
```

Inspect task readiness and verify integration:

```bash
lb --json ready
lb --json get merge
lb verify-integration merge
```

Integration verification checks that the commits associated with direct dependencies are present in the current Git history.

### Review Workflow

Lightboard supports independent code reviews tied to specific commits.

```bash
lb audit add api-audit --target api --lens STATE \
  --group api-review --slot A --scope src/api.py

lb claim api-audit --agent reviewer --branch reviewer/api
lb status api-audit IN_PROGRESS
```

Reviewers can record structured findings, identify required changes, and provide verification evidence. Findings are tracked through `OPEN`, `FIXED`, and `VERIFIED` states.

```bash
lb finding list --target api
lb review state api
lb review gate api
```

Reviews are associated with the target commit, keeping verification results traceable to the code that was inspected.

## Architecture

Lightboard separates task coordination from agent execution. The existing core manages persistent state, task ownership, dependencies, contracts, and review workflows. Execution providers and supervisory components are being developed around these interfaces.

The architecture uses Python and SQLite, with Git as the source of truth for implementation and integration history.

Repository state is stored under the common Git directory, allowing worktrees belonging to the same repository to access a shared board.

### CLI

| Command | Description |
|---|---|
| `lb list` | View tasks and their current status |
| `lb list --watch` | Monitor the task board in the terminal |
| `lb get TASK_ID` | Inspect a task and its associated records |
| `lb --json ready` | Retrieve task readiness information |
| `lb --json state` | Retrieve the current board state |
| `lb --json swarm` | Inspect board diagnostics and history |
| `lb review state TASK_ID` | Inspect review status |
| `lb review gate TASK_ID` | Evaluate review requirements |
| `lb verify-integration TASK_ID` | Verify dependency integration |

JSON output is available for programmatic access, while Rich provides an optional terminal view.

## Development

Lightboard uses Python's standard library for its core functionality, with Rich as an optional dependency for terminal rendering.

Install dependencies and run the test suite:

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```


## Roadmap

The following phases are planned; the current CLI provides manual task coordination and review, not automated agent execution.

### Phase 1 — Agent Execution Engine

- Implement an asynchronous agent runtime using Python subprocess management.
- Define a provider-independent agent execution interface.
- Implement the Codex CLI execution adapter.
- Add Claude Code CLI support through the same provider interface.
- Introduce persistent agent runs, session IDs, execution states, and logs.
- Implement structured event processing for provider output.
- Add isolated Git worktrees for individual agents.
- Implement dependency-aware task scheduling.
- Support concurrent execution with configurable limits.
- Create an end-to-end execution workflow for manually defined tasks.

### Phase 2 — Modular Teams, Roles & Skills

- Define YAML schemas for teams, roles, and application configuration.
- Implement a role registry with inheritance and configurable overrides.
- Provide built-in roles for coding, reviewing, planning, security auditing, and integration.
- Implement reusable team presets with multiple instances of the same role.
- Support different providers, models, and execution settings within a single team.
- Add project-level and global configuration resolution.
- Implement selective loading of Markdown-based agent skills.
- Provide configuration validation, import, and export.
- Add CLI commands for managing and launching teams.
- Introduce basic terminal-based team configuration.

### Phase 3 — Supervision & Reliability

- Implement an agent supervisor with persistent execution state.
- Monitor process health, activity, and task progress independently.
- Detect potential stalls, timeouts, and unexpected process termination.
- Support recovery and session resumption when provided by the underlying CLI.
- Implement configurable recovery attempts and escalation policies.
- Add manual controls for stopping, restarting, and resuming execution.
- Prevent duplicate task execution and conflicting agent ownership.
- Preserve logs, task state, and Git changes during failures.
- Restore consistent application state following unexpected shutdowns.
- Extend automated tests with process failures, race conditions, and recovery scenarios.

### Phase 4 — Autonomous Orchestration

- Introduce a Lead Agent responsible for decomposing user requests into structured tasks.
- Define validated planning schemas with assignments, dependencies, contracts, and acceptance criteria.
- Add plan preview, validation, and user approval before execution.
- Implement autonomous task creation and dependency-aware execution.
- Integrate Claude as a planning backend, including an optional first-party Claude API integration.
- Automate review assignment after implementation tasks.
- Implement bounded Audit → Fix → Verify workflows.
- Add execution of project-specific tests and verification commands.
- Implement Git integration and merge coordination.
- Develop an AI Team Builder for generating and editing team presets through natural language.
- Generate structured execution reports containing results, findings, and integration status.

### Phase 5 — Developer Experience & Ecosystem

- Develop a full-featured terminal UI for task boards, teams, agents, and sessions.
- Add live execution monitoring, logs, Git diffs, and review findings.
- Introduce execution analytics for duration, tokens, failures, and recovery attempts.
- Implement provider-aware resource limits and configurable execution budgets.
- Extend provider support with Antigravity CLI and additional compatible tools.
- Develop Team Doctor for analyzing execution history and recommending improvements.
- Support community-distributed team configurations, roles, and skills.
- Evaluate desktop application requirements and packaging options.
- Explore a Tauri-based desktop interface backed by the existing Python core.
