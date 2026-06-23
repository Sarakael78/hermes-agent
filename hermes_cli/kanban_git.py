"""Repo-linked Kanban git closeout policy helpers.

This module is deliberately conservative.  It stores repo metadata on a board
and builds auditable closeout plans, but it does not execute git commit, push,
or PR commands.  Callers can use the plan as a review artefact or a future
operator hand-off.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import shlex
import subprocess
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb


VALID_POLICIES = {"dry-run", "local-branch", "local-commit", "push-branch", "draft-pr"}
CONFIDENTIAL_MARKERS = {"legal", "client", "confidential", "privileged"}
DEFAULT_BRANCH_PREFIX = "kanban"


@dataclass
class RepoPolicy:
    board: str
    repo_path: str
    closeout_policy: str = "dry-run"
    remote: str = "origin"
    default_branch: str = "main"
    branch_prefix: str = DEFAULT_BRANCH_PREFIX
    allowed_paths: list[str] = field(default_factory=list)
    confidentiality: str = "normal"
    allow_confidential: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CloseoutPlan:
    board: str
    task_id: str
    repo_path: str
    policy: str
    status: str
    branch_name: str
    dirty_paths: list[str] = field(default_factory=list)
    scoped_paths: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    commit_message: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _resolve_repo(path: str | Path) -> Path:
    repo = Path(path).expanduser().resolve()
    proc = _run_git(repo, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        raise ValueError(f"{repo} is not a git repository: {proc.stderr.strip()}")
    return Path(proc.stdout.strip()).resolve()


def _normalise_policy(policy: str) -> str:
    value = (policy or "dry-run").strip().lower()
    if value not in VALID_POLICIES:
        raise ValueError(f"unknown repo closeout policy {policy!r}; expected one of {sorted(VALID_POLICIES)}")
    return value


def _normalise_paths(paths: Optional[Iterable[str]]) -> list[str]:
    out: list[str] = []
    for raw in paths or []:
        value = str(raw).strip()
        if not value:
            continue
        if value.startswith("/") or ".." in Path(value).parts:
            raise ValueError(f"allowed/scope paths must be relative to the repo: {value!r}")
        out.append(value)
    return out


def _metadata_without_db(board: str) -> dict[str, Any]:
    meta = kb.read_board_metadata(board)
    meta.pop("db_path", None)
    return meta


def _write_repo_metadata(board: str, policy: RepoPolicy) -> dict[str, Any]:
    meta = _metadata_without_db(board)
    meta["repo"] = policy.to_dict()
    path = kb.board_metadata_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return kb.read_board_metadata(board)


def link_board_repo_policy(
    board: str,
    *,
    repo_path: str,
    closeout_policy: str = "dry-run",
    remote: str = "origin",
    default_branch: str = "main",
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
    allowed_paths: Optional[Iterable[str]] = None,
    confidentiality: str = "normal",
    allow_confidential: bool = False,
) -> RepoPolicy:
    """Attach repo closeout metadata to a Kanban board.

    The default policy is intentionally report-only.  Higher tiers are merely
    policy data for the planner unless a future explicit executor consumes the
    plan.
    """
    slug = kb._normalize_board_slug(board)
    if not slug:
        raise ValueError("board slug is required")
    if not kb.board_exists(slug):
        raise ValueError(f"board {slug!r} does not exist")
    repo = _resolve_repo(repo_path)
    policy = RepoPolicy(
        board=slug,
        repo_path=str(repo),
        closeout_policy=_normalise_policy(closeout_policy),
        remote=remote or "origin",
        default_branch=default_branch or "main",
        branch_prefix=(branch_prefix or DEFAULT_BRANCH_PREFIX).strip("/"),
        allowed_paths=_normalise_paths(allowed_paths),
        confidentiality=(confidentiality or "normal").strip().lower(),
        allow_confidential=bool(allow_confidential),
    )
    _write_repo_metadata(slug, policy)
    return policy


def read_board_repo_policy(board: str) -> RepoPolicy:
    slug = kb._normalize_board_slug(board)
    if not slug:
        raise ValueError("board slug is required")
    raw = kb.read_board_metadata(slug).get("repo")
    if not isinstance(raw, dict):
        raise ValueError(f"board {slug!r} has no repo policy; run `hermes kanban --board {slug} repo link --path <repo>`")
    data = dict(raw)
    data.setdefault("board", slug)
    data.setdefault("closeout_policy", "dry-run")
    data.setdefault("remote", "origin")
    data.setdefault("default_branch", "main")
    data.setdefault("branch_prefix", DEFAULT_BRANCH_PREFIX)
    data.setdefault("allowed_paths", [])
    data.setdefault("confidentiality", "normal")
    data.setdefault("allow_confidential", False)
    data["closeout_policy"] = _normalise_policy(str(data["closeout_policy"]))
    data["repo_path"] = str(_resolve_repo(data["repo_path"]))
    data["allowed_paths"] = _normalise_paths(data.get("allowed_paths") or [])
    return RepoPolicy(**data)


def _dirty_paths(repo: Path) -> list[str]:
    proc = _run_git(repo, "status", "--porcelain=v1", "-uall")
    if proc.returncode != 0:
        raise ValueError(f"could not inspect git status: {proc.stderr.strip()}")
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            paths.append(path)
    return sorted(set(paths))


def _is_under(path: str, roots: Iterable[str]) -> bool:
    for root in roots:
        r = root.rstrip("/")
        if not r:
            continue
        if path == r or path.startswith(r + "/") or path.startswith(r.rstrip("/") + "/"):
            return True
        if root.endswith("/") and path.startswith(root):
            return True
    return False


def _commit_message(board: str, task_id: str, task_title: str = "Kanban closeout") -> str:
    title = " ".join((task_title or "Kanban closeout").split())
    return f"kanban: {board}/{task_id} {title}"[:240]


def _task_title(board: str, task_id: str) -> str:
    with kb.connect_closing(board=board) as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"task {task_id!r} does not exist on board {board!r}")
    return task.title


def build_closeout_plan(
    board: str,
    task_id: str,
    *,
    scopes: Optional[Iterable[str]] = None,
    verification_commands: Optional[Iterable[str]] = None,
    verification_passed: bool = False,
    branch_name: Optional[str] = None,
) -> CloseoutPlan:
    """Build a safe, auditable git closeout plan for a board task.

    This function never mutates git state.  It reports planned commands only.
    """
    slug = kb._normalize_board_slug(board)
    if not slug:
        raise ValueError("board slug is required")
    policy = read_board_repo_policy(slug)
    repo = Path(policy.repo_path)
    task_title = _task_title(slug, task_id)
    dirty = _dirty_paths(repo)
    scoped = _normalise_paths(scopes)
    verify_cmds = [str(c).strip() for c in verification_commands or [] if str(c).strip()]
    branch = (branch_name or f"{policy.branch_prefix}/{slug}/{task_id}").strip()
    commit_msg = _commit_message(slug, task_id, task_title)
    blockers: list[str] = []
    notes: list[str] = []
    commands: list[str] = []

    if policy.confidentiality in CONFIDENTIAL_MARKERS and not policy.allow_confidential:
        blockers.append(
            f"board confidentiality is {policy.confidentiality!r}; git closeout is refused unless allow_confidential is set"
        )

    if branch == policy.default_branch:
        blockers.append("refusing closeout plan that targets the default branch")

    if policy.closeout_policy in {"push-branch", "draft-pr"}:
        blockers.append(
            f"{policy.closeout_policy} is an external GitHub action and is not executed by this MVP; create a gated follow-up"
        )

    if policy.closeout_policy in {"local-commit", "push-branch", "draft-pr"}:
        if not verify_cmds or not verification_passed:
            blockers.append("local commit or higher policy requires successful verification commands")
        if dirty and not scoped:
            blockers.append("dirty repo paths require explicit --scope values; refusing broad staging")
        if scoped:
            outside_scope = [p for p in dirty if not _is_under(p, scoped)]
            if outside_scope:
                blockers.append("dirty paths outside requested scope: " + ", ".join(outside_scope))
        if policy.allowed_paths:
            outside_allowed = [p for p in dirty if not _is_under(p, policy.allowed_paths)]
            if outside_allowed:
                blockers.append("dirty paths outside board allowed paths: " + ", ".join(outside_allowed))

    if policy.closeout_policy == "dry-run":
        notes.append("dry-run policy: report only; no git commands are planned")
    elif policy.closeout_policy == "local-branch":
        commands.append(f"git switch -c {shlex.quote(branch)}")
    elif policy.closeout_policy in {"local-commit", "push-branch", "draft-pr"} and not blockers:
        commands.append(f"git switch -c {shlex.quote(branch)}")
        for path in dirty:
            commands.append(f"git add -- {shlex.quote(path)}")
        commands.append(f"git commit -m {shlex.quote(commit_msg)}")
        if policy.closeout_policy == "push-branch":
            commands.append(f"git push -u {shlex.quote(policy.remote)} {shlex.quote(branch)}")
        elif policy.closeout_policy == "draft-pr":
            commands.append(f"git push -u {shlex.quote(policy.remote)} {shlex.quote(branch)}")
            commands.append("gh pr create --draft --fill")

    # Keep the MVP fail-closed for external policies even if commands were built
    # by a future edit path above.
    if policy.closeout_policy in {"push-branch", "draft-pr"}:
        commands = [cmd for cmd in commands if not (cmd.startswith("git push") or cmd.startswith("gh pr"))]

    status = "blocked" if blockers else "ready"
    return CloseoutPlan(
        board=slug,
        task_id=task_id,
        repo_path=str(repo),
        policy=policy.closeout_policy,
        status=status,
        branch_name=branch,
        dirty_paths=dirty,
        scoped_paths=scoped,
        blockers=blockers,
        commands=[] if blockers else commands,
        commit_message=commit_msg,
        notes=notes,
    )


def render_plan(plan: CloseoutPlan) -> str:
    lines = [
        f"Repo closeout plan for {plan.board}/{plan.task_id}",
        f"Status: {plan.status}",
        f"Policy: {plan.policy}",
        f"Repo: {plan.repo_path}",
        f"Branch: {plan.branch_name}",
    ]
    if plan.dirty_paths:
        lines.append("Dirty paths:")
        lines.extend(f"  - {p}" for p in plan.dirty_paths)
    if plan.blockers:
        lines.append("Blockers:")
        lines.extend(f"  - {b}" for b in plan.blockers)
    if plan.commands:
        lines.append("Planned commands (not executed):")
        lines.extend(f"  {cmd}" for cmd in plan.commands)
    if plan.notes:
        lines.append("Notes:")
        lines.extend(f"  - {n}" for n in plan.notes)
    return "\n".join(lines)
