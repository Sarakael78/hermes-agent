"""Bounded safe Kanban sweeper/reporting helpers.

The sweeper is intentionally conservative: it reports maintenance candidates
across local, non-archived boards and only mutates state for a narrow,
auditable recompute-ready action when explicitly asked to apply.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import time
from typing import Any, Optional

from hermes_cli import kanban_db as kb

DEFAULT_STALE_TIMEOUT_SECONDS = 4 * 60 * 60
_HEARTBEAT_STALE_SECONDS = 60 * 60

_QUEUE_STATUSES = {"ready", "todo", "scheduled"}
_REVIEW_MARKERS = ("review-required", "review required", "review_required")


def _task_base(board_slug: str, task: kb.Task) -> dict[str, Any]:
    return {
        "board": board_slug,
        "task_id": task.id,
        "title": task.title,
        "status": task.status,
        "assignee": task.assignee,
        "tenant": task.tenant,
    }


def _latest_reason(conn, task_id: str) -> Optional[str]:
    for event in reversed(kb.list_events(conn, task_id)):
        payload = event.payload or {}
        reason = payload.get("reason") if isinstance(payload, dict) else None
        if reason:
            return str(reason)
    comments = kb.list_comments(conn, task_id)
    if comments:
        return comments[-1].body
    return None


def _contains_review_required(*parts: Optional[str]) -> bool:
    text = "\n".join(part for part in parts if part).casefold()
    return any(marker in text for marker in _REVIEW_MARKERS)


def _active_started_at(conn, task: kb.Task) -> Optional[int]:
    if task.current_run_id is not None:
        row = conn.execute(
            "SELECT started_at FROM task_runs WHERE id = ?",
            (task.current_run_id,),
        ).fetchone()
        if row and row["started_at"] is not None:
            return int(row["started_at"])
    return int(task.started_at) if task.started_at is not None else None


def _dependency_status(conn, task_id: str) -> tuple[bool, list[str]]:
    rows = conn.execute(
        """
        SELECT p.id, p.status
          FROM task_links l
          JOIN tasks p ON p.id = l.parent_id
         WHERE l.child_id = ?
        """,
        (task_id,),
    ).fetchall()
    blockers = [f"{row['id']}:{row['status']}" for row in rows if row["status"] not in {"done", "archived"}]
    return (not blockers, blockers)


def _classify_task(
    conn,
    *,
    board_slug: str,
    task: kb.Task,
    now: int,
    stale_timeout_seconds: int,
) -> Optional[dict[str, Any]]:
    base = _task_base(board_slug, task)

    if task.status == "running":
        started_at = _active_started_at(conn, task)
        elapsed = (now - started_at) if started_at is not None else None
        hb_age = (now - int(task.last_heartbeat_at)) if task.last_heartbeat_at is not None else None
        claim_age = (now - int(task.claim_expires)) if task.claim_expires is not None else None
        stale_by_runtime = (
            stale_timeout_seconds > 0
            and elapsed is not None
            and elapsed >= stale_timeout_seconds
            and (hb_age is None or hb_age >= _HEARTBEAT_STALE_SECONDS)
        )
        expired_claim = task.claim_expires is not None and int(task.claim_expires) < now
        if stale_by_runtime:
            return {
                **base,
                "category": "stale_running",
                "proposed_action": "escalate_reclaim",
                "safe_to_apply": False,
                "reason": "running task exceeded stale timeout without a recent heartbeat",
                "elapsed_seconds": elapsed,
                "last_heartbeat_at": task.last_heartbeat_at,
                "heartbeat_age_seconds": hb_age,
                "claim_expires": task.claim_expires,
                "claim_expired_seconds": claim_age,
            }
        if expired_claim:
            return {
                **base,
                "category": "expired_claim",
                "proposed_action": "escalate_reclaim",
                "safe_to_apply": False,
                "reason": "running task claim has expired; reclaim can terminate a worker and needs an operator decision",
                "elapsed_seconds": elapsed,
                "last_heartbeat_at": task.last_heartbeat_at,
                "heartbeat_age_seconds": hb_age,
                "claim_expires": task.claim_expires,
                "claim_expired_seconds": claim_age,
            }
        return {
            **base,
            "category": "running_active",
            "proposed_action": "observe",
            "safe_to_apply": False,
            "reason": "running task is not stale under the configured threshold",
            "elapsed_seconds": elapsed,
            "last_heartbeat_at": task.last_heartbeat_at,
            "heartbeat_age_seconds": hb_age,
            "claim_expires": task.claim_expires,
        }

    if task.status == "blocked":
        reason = _latest_reason(conn, task.id)
        review_required = _contains_review_required(task.title, task.body, task.result, reason)
        return {
            **base,
            "category": "review_required" if review_required else "blocked_escalation",
            "proposed_action": "escalate_review" if review_required else "escalate_blocked",
            "safe_to_apply": False,
            "reason": reason or "blocked tasks require human/operator input before unblocking",
        }

    if task.status == "scheduled":
        return {
            **base,
            "category": "scheduled_queue",
            "proposed_action": "observe",
            "safe_to_apply": False,
            "reason": _latest_reason(conn, task.id) or "scheduled tasks are parked until time/external condition is met",
        }

    if task.status == "todo":
        deps_done, blockers = _dependency_status(conn, task.id)
        if deps_done:
            return {
                **base,
                "category": "ready_promotion_candidate",
                "proposed_action": "promote_ready_candidate",
                "safe_to_apply": True,
                "reason": "todo task has no unfinished parents and can be safely promoted",
            }
        return {
            **base,
            "category": "todo_queue",
            "proposed_action": "observe",
            "safe_to_apply": False,
            "reason": "todo task is waiting on unfinished parents",
            "blocking_parents": blockers,
        }

    if task.status == "ready":
        return {
            **base,
            "category": "ready_queue",
            "proposed_action": "dispatch_queue",
            "safe_to_apply": False,
            "reason": "ready task is dispatchable; sweeper does not spawn workers",
        }

    if task.status in {"triage", "review"}:
        return {
            **base,
            "category": f"{task.status}_queue",
            "proposed_action": "escalate_triage" if task.status == "triage" else "escalate_review",
            "safe_to_apply": False,
            "reason": f"{task.status} status requires a specialist or human decision",
        }

    return None


def _selected_boards(board: Optional[str]) -> list[dict[str, Any]]:
    if board:
        normed = kb._normalize_board_slug(board)
        if normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
            raise ValueError(f"board {normed!r} does not exist")
        return [kb.read_board_metadata(normed)]
    return kb.list_boards(include_archived=False)


def _db_path_for_board_slug(slug: str) -> Path:
    """Return the physical DB path for ``slug`` without env DB pin overrides."""
    normed = kb._normalize_board_slug(slug) or kb.DEFAULT_BOARD
    if normed == kb.DEFAULT_BOARD:
        return kb.kanban_home() / "kanban.db"
    return kb.board_dir(normed) / "kanban.db"


def build_sweep_report(
    *,
    board: Optional[str] = None,
    stale_timeout_seconds: int = DEFAULT_STALE_TIMEOUT_SECONDS,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Return a dry-run maintenance report for local non-archived boards."""
    now_i = int(time.time() if now is None else now)
    boards_out: list[dict[str, Any]] = []

    for board_meta in _selected_boards(board):
        slug = str(board_meta.get("slug") or kb.DEFAULT_BOARD)
        db_path = _db_path_for_board_slug(slug)
        kb.init_db(db_path=db_path)
        with kb.connect_closing(db_path=db_path) as conn:
            tasks = kb.list_tasks(conn, include_archived=False)
            items = [
                item
                for task in tasks
                for item in [_classify_task(
                    conn,
                    board_slug=slug,
                    task=task,
                    now=now_i,
                    stale_timeout_seconds=int(stale_timeout_seconds),
                )]
                if item is not None
            ]
        counts_by_status = Counter(task.status for task in tasks)
        counts_by_category = Counter(item["category"] for item in items)
        boards_out.append(
            {
                "slug": slug,
                "name": board_meta.get("name") or slug,
                "task_counts": dict(sorted(counts_by_status.items())),
                "counts_by_category": dict(sorted(counts_by_category.items())),
                "open_task_count": len(tasks),
                "needs_escalation": any(not item.get("safe_to_apply") and item.get("proposed_action", "").startswith("escalate") for item in items),
                "items": items,
            }
        )

    return {
        "generated_at": now_i,
        "dry_run": True,
        "apply_supported_actions": ["promote_ready_candidate"],
        "boards": boards_out,
    }


def apply_safe_actions(report: dict[str, Any], *, reason: Optional[str] = None) -> list[dict[str, Any]]:
    """Apply only narrow, safe local actions surfaced by the report.

    The MVP deliberately limits apply mode to dependency-satisfied ``todo``
    tasks already surfaced as safe candidates. It promotes those exact task
    ids through ``promote_task`` for an audit event. It does not reclaim,
    unblock, spawn, archive, delete, touch profiles/config, or send anything
    externally.
    """
    applied: list[dict[str, Any]] = []
    for board in report.get("boards", []):
        slug = str(board.get("slug") or kb.DEFAULT_BOARD)
        candidates = [
            item
            for item in board.get("items", [])
            if item.get("category") == "ready_promotion_candidate"
            and item.get("proposed_action") == "promote_ready_candidate"
            and item.get("safe_to_apply")
        ]
        if not candidates:
            continue
        promoted: list[str] = []
        refused: list[dict[str, str]] = []
        db_path = _db_path_for_board_slug(slug)
        with kb.connect_closing(db_path=db_path) as conn:
            for item in candidates:
                task_id = str(item["task_id"])
                ok, refusal = kb.promote_task(
                    conn,
                    task_id,
                    actor="kanban-sweeper",
                    reason=reason or "kanban sweep safe apply: dependency-satisfied todo promotion",
                    force=False,
                    dry_run=False,
                )
                if ok:
                    promoted.append(task_id)
                elif refusal:
                    refused.append({"task_id": task_id, "reason": refusal})
        if promoted or refused:
            applied.append(
                {
                    "board": slug,
                    "action": "promote_ready_candidate",
                    "changed": len(promoted),
                    "task_ids": promoted,
                    "refused": refused,
                    "reason": reason,
                }
            )
    return applied


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "Kanban sweep report",
        f"Dry run: {str(report.get('dry_run', True)).lower()}",
        "Safe apply actions: " + ", ".join(report.get("apply_supported_actions", [])),
    ]
    applied = report.get("applied") or []
    if applied:
        lines.append("Applied:")
        for item in applied:
            lines.append(f"  - {item['board']}: {item['action']} changed={item['changed']}")
    for board in report.get("boards", []):
        lines.append("")
        lines.append(
            f"Board {board['slug']} — open={board['open_task_count']} "
            f"escalation={str(board['needs_escalation']).lower()}"
        )
        if board.get("counts_by_category"):
            cats = ", ".join(f"{k}={v}" for k, v in board["counts_by_category"].items())
            lines.append(f"  Categories: {cats}")
        for item in board.get("items", []):
            safe = "safe" if item.get("safe_to_apply") else "report-only"
            lines.append(
                f"  - {item['task_id']} [{item['status']}] {item['category']} -> "
                f"{item['proposed_action']} ({safe}): {item['title']}"
            )
            if item.get("reason"):
                lines.append(f"      reason: {item['reason']}")
    return "\n".join(lines)
