"""Tests for the bounded Kanban sweeper/reporting MVP."""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sweeper as sweeper
from hermes_cli.kanban_sweeper import apply_safe_actions, build_sweep_report


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_claimed_stale_task(conn) -> str:
    task_id = kb.create_task(conn, title="stale runner", assignee="alice")
    claimed = kb.claim_task(conn, task_id, ttl_seconds=1, claimer="test-host:123")
    assert claimed is not None
    old = int(time.time()) - 7200
    conn.execute(
        """
        UPDATE tasks
           SET started_at = ?, claim_expires = ?, last_heartbeat_at = NULL
         WHERE id = ?
        """,
        (old, old, task_id),
    )
    conn.execute(
        """
        UPDATE task_runs
           SET started_at = ?, claim_expires = ?
         WHERE task_id = ? AND ended_at IS NULL
        """,
        (old, old, task_id),
    )
    conn.commit()
    return task_id


def test_sweep_report_enumerates_non_archived_boards_and_classifies_open_work(kanban_home):
    kb.create_board("client-a")
    kb.create_board("old-board")
    kb.write_board_metadata("old-board", archived=True)

    with kb.scoped_current_board("client-a"), kb.connect_closing() as conn:
        stale_id = _create_claimed_stale_task(conn)
        ready_id = kb.create_task(conn, title="ready queue", assignee="bob")
        blocked_id = kb.create_task(conn, title="review item", assignee="reviewer")
        assert kb.block_task(conn, blocked_id, reason="review-required: needs human eyes")
        scheduled_id = kb.create_task(conn, title="wait for date", assignee="scheduler")
        assert kb.schedule_task(conn, scheduled_id, reason="waiting on Friday")

    report = build_sweep_report(stale_timeout_seconds=3600)

    board_slugs = {board["slug"] for board in report["boards"]}
    assert "client-a" in board_slugs
    assert "old-board" not in board_slugs

    client = next(board for board in report["boards"] if board["slug"] == "client-a")
    by_id = {item["task_id"]: item for item in client["items"]}

    assert by_id[stale_id]["category"] == "stale_running"
    assert by_id[stale_id]["safe_to_apply"] is False
    assert by_id[stale_id]["proposed_action"] == "escalate_reclaim"

    assert by_id[blocked_id]["category"] == "review_required"
    assert by_id[blocked_id]["proposed_action"] == "escalate_review"
    assert by_id[blocked_id]["safe_to_apply"] is False

    assert by_id[scheduled_id]["category"] == "scheduled_queue"
    assert by_id[ready_id]["category"] == "ready_queue"
    assert client["counts_by_category"]["stale_running"] == 1
    assert client["counts_by_category"]["review_required"] == 1


def test_sweep_report_has_deterministic_json_shape(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="plain ready", assignee="alice")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()

    report = build_sweep_report(now=123456789, stale_timeout_seconds=3600)

    assert list(report) == ["generated_at", "dry_run", "apply_supported_actions", "boards"]
    assert report["generated_at"] == 123456789
    assert report["dry_run"] is True
    assert report["apply_supported_actions"] == ["promote_ready_candidate"]
    default_board = report["boards"][0]
    assert list(default_board) == [
        "slug",
        "name",
        "task_counts",
        "counts_by_category",
        "open_task_count",
        "needs_escalation",
        "items",
    ]
    assert json.dumps(report, sort_keys=True) == json.dumps(
        build_sweep_report(now=123456789, stale_timeout_seconds=3600),
        sort_keys=True,
    )


def test_sweep_cli_defaults_to_dry_run_json_and_apply_is_limited_to_recompute(kanban_home):
    child = None
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        child = kb.create_task(conn, title="child", assignee="bob", parents=[parent])
        kb.complete_task(conn, parent, result="done")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
        conn.commit()

    dry = json.loads(kc.run_slash("sweep --json"))
    assert dry["dry_run"] is True
    assert dry["applied"] == []
    assert any(
        item["task_id"] == child and item["category"] == "ready_promotion_candidate"
        for board in dry["boards"]
        for item in board["items"]
    )

    with kb.connect_closing() as conn:
        assert kb.get_task(conn, child).status == "todo"

    applied = json.loads(kc.run_slash("sweep --apply --json --reason 'operator sweep test'"))
    assert applied["dry_run"] is False
    assert applied["applied"] == [
        {
            "board": "default",
            "action": "promote_ready_candidate",
            "changed": 1,
            "task_ids": [child],
            "refused": [],
            "reason": "operator sweep test",
        }
    ]
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, child).status == "ready"


def test_sweep_report_and_apply_use_per_board_db_when_env_db_is_pinned(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")
    alpha_db = kb.kanban_db_path(board="alpha")
    beta_db = kb.kanban_db_path(board="beta")

    with kb.scoped_current_board("alpha"), kb.connect_closing() as conn:
        alpha_blocked = kb.create_task(conn, title="alpha blocked", assignee="reviewer")
        assert kb.block_task(conn, alpha_blocked, reason="review-required: alpha only")

    with kb.scoped_current_board("beta"), kb.connect_closing() as conn:
        beta_parent = kb.create_task(conn, title="beta parent", assignee="alice")
        beta_child = kb.create_task(conn, title="beta child", assignee="bob", parents=[beta_parent])
        kb.complete_task(conn, beta_parent, result="done")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (beta_child,))
        conn.commit()

    monkeypatch.setenv("HERMES_KANBAN_DB", str(alpha_db))

    report = build_sweep_report(now=123456789, stale_timeout_seconds=3600)

    by_board = {board["slug"]: board for board in report["boards"]}
    assert {"alpha", "beta"} <= set(by_board)
    alpha_items = {item["task_id"]: item for item in by_board["alpha"]["items"]}
    beta_items = {item["task_id"]: item for item in by_board["beta"]["items"]}

    assert alpha_items[alpha_blocked]["title"] == "alpha blocked"
    assert alpha_items[alpha_blocked]["category"] == "review_required"
    assert beta_items[beta_child]["title"] == "beta child"
    assert beta_items[beta_child]["category"] == "ready_promotion_candidate"
    assert alpha_blocked not in beta_items
    assert beta_child not in alpha_items

    applied = apply_safe_actions(report, reason="pinned env per-board apply")

    assert applied == [
        {
            "board": "beta",
            "action": "promote_ready_candidate",
            "changed": 1,
            "task_ids": [beta_child],
            "refused": [],
            "reason": "pinned env per-board apply",
        }
    ]
    with kb.connect_closing(db_path=alpha_db) as conn:
        alpha_task = kb.get_task(conn, alpha_blocked)
        assert alpha_task is not None
        assert alpha_task.status == "blocked"
    with kb.connect_closing(db_path=beta_db) as conn:
        beta_task = kb.get_task(conn, beta_child)
        assert beta_task is not None
        assert beta_task.status == "ready"


def test_apply_mode_does_not_call_destructive_external_or_worker_actions(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        child = kb.create_task(conn, title="child", assignee="bob", parents=[parent])
        kb.complete_task(conn, parent, result="done")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
        blocked = kb.create_task(conn, title="blocked", assignee="reviewer")
        assert kb.block_task(conn, blocked, reason="review-required: human sign-off")
        stale = _create_claimed_stale_task(conn)
        conn.commit()

    forbidden_calls = {
        "reclaim_task",
        "unblock_task",
        "archive_task",
        "delete_archived_task",
        "delete_task",
        "dispatch_once",
        "claim_task",
        "claim_review_task",
        "complete_task",
        "assign_task",
        "schedule_task",
        "remove_board",
    }
    apply_tree = ast.parse(textwrap.dedent(inspect.getsource(sweeper.apply_safe_actions)))
    called_attrs = {node.attr for node in ast.walk(apply_tree) if isinstance(node, ast.Attribute)}
    assert called_attrs & forbidden_calls == set()
    assert "promote_task" in called_attrs

    def fail_if_called(name):
        def _fail(*args, **kwargs):
            raise AssertionError(f"unsafe action {name} was called")
        return _fail

    for name in forbidden_calls:
        monkeypatch.setattr(kb, name, fail_if_called(name))

    report = build_sweep_report(stale_timeout_seconds=3600, now=int(time.time()))
    applied = apply_safe_actions(report, reason="safe apply test")

    assert applied == [
        {
            "board": "default",
            "action": "promote_ready_candidate",
            "changed": 1,
            "task_ids": [child],
            "refused": [],
            "reason": "safe apply test",
        }
    ]
    with kb.connect_closing() as conn:
        child_task = kb.get_task(conn, child)
        blocked_task = kb.get_task(conn, blocked)
        stale_task = kb.get_task(conn, stale)
        assert child_task is not None
        assert blocked_task is not None
        assert stale_task is not None
        assert child_task.status == "ready"
        assert blocked_task.status == "blocked"
        assert stale_task.status == "running"


def test_sweep_enumerates_boards_from_local_metadata_only(kanban_home, monkeypatch):
    kb.create_board("client-a")
    kb.create_board("client-b")
    observed_kwargs = []
    real_list_boards = kb.list_boards

    def spy_list_boards(*, include_archived=False):
        observed_kwargs.append(include_archived)
        return real_list_boards(include_archived=include_archived)

    monkeypatch.setattr(kb, "list_boards", spy_list_boards)

    report = build_sweep_report(now=42)

    assert observed_kwargs == [False]
    assert {board["slug"] for board in report["boards"]} >= {"default", "client-a", "client-b"}


def test_sweep_help_documents_report_only_default_and_apply_boundaries(kanban_home):
    help_text = kc.run_slash("sweep --help")

    assert "report-only by default" in help_text
    assert "JSON report" in help_text
    assert "does not reclaim, unblock, spawn, archive, delete" in help_text
    assert "does not touch profiles, config, gateway, deploys, or external services" in help_text
