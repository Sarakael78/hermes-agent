"""Tests for repo-linked Kanban git closeout policy planning."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_git as kg


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


def _create_task(board: str, title: str = "Implement feature") -> str:
    kb.create_board(board)
    with kb.connect_closing(board=board) as conn:
        return kb.create_task(conn, title=title, body="body", assignee="coder")


def _cli(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_WORKTREE)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban"] + args,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_WORKTREE),
        timeout=30,
    )


def test_link_board_repo_policy_defaults_to_safe_dry_run(fresh_home, git_repo):
    kb.create_board("repo-board")

    policy = kg.link_board_repo_policy("repo-board", repo_path=str(git_repo))

    assert policy.closeout_policy == "dry-run"
    assert policy.repo_path == str(git_repo.resolve())
    stored = kb.read_board_metadata("repo-board")["repo"]
    assert stored["closeout_policy"] == "dry-run"
    assert stored["allow_confidential"] is False


def test_local_commit_plan_uses_scoped_add_and_board_task_commit_message(fresh_home, git_repo):
    task_id = _create_task("repo-board", title="Wire closeout policy")
    (git_repo / "src").mkdir()
    (git_repo / "src" / "feature.py").write_text("print('x')\n", encoding="utf-8")
    kg.link_board_repo_policy(
        "repo-board",
        repo_path=str(git_repo),
        closeout_policy="local-commit",
        allowed_paths=["src/"],
    )

    plan = kg.build_closeout_plan(
        "repo-board",
        task_id,
        scopes=["src/"],
        verification_commands=["pytest tests/hermes_cli/test_kanban_git.py"],
        verification_passed=True,
    )

    assert plan.status == "ready"
    assert any(cmd == "git add -- src/feature.py" for cmd in plan.commands)
    assert all("git add ." not in cmd for cmd in plan.commands)
    assert any(cmd.startswith("git commit -m ") for cmd in plan.commands)
    assert f"repo-board/{task_id}" in plan.commit_message


def test_local_commit_refuses_dirty_paths_without_scope(fresh_home, git_repo):
    task_id = _create_task("repo-board")
    (git_repo / "feature.py").write_text("print('x')\n", encoding="utf-8")
    kg.link_board_repo_policy(
        "repo-board",
        repo_path=str(git_repo),
        closeout_policy="local-commit",
    )

    plan = kg.build_closeout_plan(
        "repo-board",
        task_id,
        verification_commands=["pytest tests/hermes_cli/test_kanban_git.py"],
        verification_passed=True,
    )

    assert plan.status == "blocked"
    assert any("scope" in reason.lower() for reason in plan.blockers)
    assert plan.commands == []


def test_local_commit_requires_successful_verification(fresh_home, git_repo):
    task_id = _create_task("repo-board")
    (git_repo / "feature.py").write_text("print('x')\n", encoding="utf-8")
    kg.link_board_repo_policy(
        "repo-board",
        repo_path=str(git_repo),
        closeout_policy="local-commit",
        allowed_paths=["feature.py"],
    )

    plan = kg.build_closeout_plan("repo-board", task_id, scopes=["feature.py"])

    assert plan.status == "blocked"
    assert any("verification" in reason.lower() for reason in plan.blockers)
    assert plan.commands == []


def test_confidential_board_refuses_unless_whitelisted(fresh_home, git_repo):
    task_id = _create_task("legal-board")
    kg.link_board_repo_policy(
        "legal-board",
        repo_path=str(git_repo),
        closeout_policy="local-branch",
        confidentiality="legal",
    )

    plan = kg.build_closeout_plan("legal-board", task_id)

    assert plan.status == "blocked"
    assert any("confidential" in reason.lower() or "legal" in reason.lower() for reason in plan.blockers)


def test_external_push_and_pr_are_gated_followups_not_executed(fresh_home, git_repo):
    task_id = _create_task("repo-board")
    kg.link_board_repo_policy(
        "repo-board",
        repo_path=str(git_repo),
        closeout_policy="draft-pr",
    )

    plan = kg.build_closeout_plan(
        "repo-board",
        task_id,
        verification_commands=["pytest tests/hermes_cli/test_kanban_git.py"],
        verification_passed=True,
    )

    assert plan.status == "blocked"
    assert any("not implemented" in reason.lower() or "external" in reason.lower() for reason in plan.blockers)
    assert not any(cmd.startswith("git push") or cmd.startswith("gh pr") for cmd in plan.commands)


def test_refuses_default_branch_for_push_policy(fresh_home, git_repo):
    task_id = _create_task("repo-board")
    kg.link_board_repo_policy(
        "repo-board",
        repo_path=str(git_repo),
        closeout_policy="push-branch",
        default_branch="main",
    )

    plan = kg.build_closeout_plan(
        "repo-board",
        task_id,
        branch_name="main",
        verification_commands=["pytest tests/hermes_cli/test_kanban_git.py"],
        verification_passed=True,
    )

    assert plan.status == "blocked"
    assert any("default branch" in reason.lower() for reason in plan.blockers)


def test_repo_link_cli_writes_policy_json(fresh_home, git_repo):
    env = {"HERMES_HOME": str(fresh_home)}
    assert _cli(["boards", "create", "repo-board"], env_extra=env).returncode == 0

    res = _cli(
        [
            "--board",
            "repo-board",
            "repo",
            "link",
            "--path",
            str(git_repo),
            "--policy",
            "local-commit",
            "--allowed-path",
            "src/",
            "--json",
        ],
        env_extra=env,
    )

    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["closeout_policy"] == "local-commit"
    assert data["allowed_paths"] == ["src/"]
