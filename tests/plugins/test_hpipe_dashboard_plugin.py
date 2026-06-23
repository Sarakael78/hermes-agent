"""Tests for the Hpipe dashboard plugin backend and shipped bundle."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

NOW = 200_000
REPO_ROOT = Path(__file__).resolve().parents[2]
HPIPE_ROOT = REPO_ROOT / "plugins" / "hpipe" / "dashboard"


def _load_plugin_module():
    plugin_file = HPIPE_ROOT / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hpipe_test", plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _seed_board(base: Path, slug: str, *, name: str, description: str, created_at: int, tasks: list[tuple], runs: list[tuple], comments: list[tuple], attachments: list[tuple], events: list[tuple] | None = None) -> None:
    board_dir = base / "kanban" / "boards" / slug
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "name": name,
                "description": description,
                "created_at": created_at,
                "default_workdir": str(REPO_ROOT),
                "archived": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(board_dir / "kanban.db")
    try:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT,
                body TEXT,
                assignee TEXT,
                status TEXT,
                created_at INTEGER,
                started_at INTEGER,
                completed_at INTEGER,
                result TEXT
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY,
                task_id TEXT,
                profile TEXT,
                status TEXT,
                outcome TEXT,
                started_at INTEGER,
                ended_at INTEGER,
                summary TEXT,
                metadata TEXT,
                error TEXT
            );
            CREATE TABLE task_comments (
                id INTEGER PRIMARY KEY,
                task_id TEXT,
                author TEXT,
                body TEXT,
                created_at INTEGER
            );
            CREATE TABLE task_attachments (
                id INTEGER PRIMARY KEY,
                task_id TEXT,
                filename TEXT,
                size INTEGER,
                created_at INTEGER
            );
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY,
                task_id TEXT,
                run_id INTEGER,
                kind TEXT,
                payload TEXT,
                created_at INTEGER
            );
            """
        )
        for row in tasks:
            conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
        for row in runs:
            conn.execute("INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
        for row in comments:
            conn.execute("INSERT INTO task_comments VALUES (?, ?, ?, ?, ?)", row)
        for row in attachments:
            conn.execute("INSERT INTO task_attachments VALUES (?, ?, ?, ?, ?)", row)
        for row in events or []:
            conn.execute("INSERT INTO task_events VALUES (?, ?, ?, ?, ?, ?)", row)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def hpipe_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    _seed_board(
        home,
        "hpipe-demo-alpha",
        name="Hpipe Alpha Cockpit",
        description="older board with blockers and failures",
        created_at=NOW - 200_000,
        tasks=[
            ("t_a1", "Implement cockpit payload", "needs smoke evidence", "coder01", "done", NOW - 1_500, NOW - 1_400, NOW - 1_000, "verified smoke tests"),
            ("t_a2", "Review stale board", "", "reviewer01", "blocked", -100_000, None, None, ""),
            ("t_a3", "Wire timeline", "", "coder02", "running", NOW - 190_000, NOW - 189_500, None, ""),
        ],
        runs=[
            (1, "t_a1", "coder01", "completed", "completed", NOW - 1_400, NOW - 1_000, "pytest pass", "{}", None),
            (2, "t_a2", "reviewer01", "crashed", "crashed", NOW - 130_000, NOW - 125_000, "", "{}", "protocol violation: did not call kanban_complete"),
            (3, "t_a3", "coder02", "timed_out", "timed_out", NOW - 110_000, NOW - 108_000, "", "{}", "timed out waiting for worker"),
        ],
        comments=[(1, "t_a1", "reviewer", "verification output captured", NOW - 900)],
        attachments=[(1, "t_a1", "evidence.txt", 512, NOW - 850)],
        events=[
            (1, "t_a1", 1, "claimed", '{"lock":"abc"}', NOW - 1_410),
            (2, "t_a1", 1, "heartbeat", '{"note":"smoke running"}', NOW - 1_200),
            (3, "t_a1", 1, "completed", '{"status":"done"}', NOW - 1_000),
        ],
    )
    _seed_board(
        home,
        "hpipe-demo-beta",
        name="Hpipe Beta Cockpit",
        description="newer board with recent activity",
        created_at=NOW - 5_000,
        tasks=[
            ("t_b1", "Document cockpit layout", "", "coder01", "done", NOW - 90_000, NOW - 89_500, NOW - 89_000, "manual readback"),
            ("t_b2", "Review freshness deltas", "", "reviewer01", "todo", NOW - 2_500, None, None, ""),
        ],
        runs=[
            (1, "t_b1", "coder01", "completed", "completed", NOW - 89_800, NOW - 89_000, "review sign-off", "{}", None),
            (2, "t_b2", "reviewer01", "failed", "failed", NOW - 1_300, NOW - 500, "", "{}", "dependency install missing"),
        ],
        comments=[(1, "t_b1", "reviewer", "readback captured", NOW - 88_500)],
        attachments=[],
        events=[
            (1, "t_b1", 1, "claimed", '{"lock":"def"}', NOW - 89_850),
            (2, "t_b1", 1, "spawned", '{"pid":123}', NOW - 89_790),
            (3, "t_b1", 1, "heartbeat", '{"note":"readback"}', NOW - 89_400),
            (4, "t_b1", 1, "completed", '{"status":"done"}', NOW - 89_000),
        ],
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


@pytest.fixture
def client(hpipe_home, monkeypatch):
    app = FastAPI()
    mod = _load_plugin_module()
    monkeypatch.setattr(mod.time, "time", lambda: NOW)
    app.include_router(mod.router, prefix="/api/plugins/hpipe")
    return TestClient(app)


def test_snapshot_exposes_hpipe_control_plane_metrics(client):
    r = client.get("/api/plugins/hpipe/snapshot")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["summary"]["board_count"] == 2
    assert "run_reconciliation" in data
    assert "historical_stats" in data
    assert "full_run_reconciliation" in data


RUN_INDEX_FIXTURE = """# hpipe RUN_INDEX.md convention

## Current entries

- 2026-06-10 — hpipe-alpha-board — code — /tmp/alpha
  - Goal: first alpha tranche
  - Implemented: parser and tests
  - Verification: pytest
  - Evidence: board hpipe-alpha-board
- 2026-06-11 — hpipe-beta-board — stage — /tmp/beta
  - Goal: stage follow-up
  - Implemented: staging only
- 2026-06-12 — hpipe-gamma-board — code — /tmp/gamma
  - Goal: gamma tranche
  - Deferred: live mutation
- 2026-06-13 — hpipe-delta-board — spec — /tmp/delta
  - Goal: spec only
"""


def test_parse_run_index_file_extracts_full_history_and_source_metadata(hpipe_home, monkeypatch, tmp_path):
    run_index_path = tmp_path / "RUN_INDEX.md"
    run_index_path.write_text(RUN_INDEX_FIXTURE, encoding="utf-8")
    mod = _load_plugin_module()
    monkeypatch.setattr(mod, "_RUN_INDEX_FALLBACK", run_index_path)
    monkeypatch.setattr(mod, "_run_hpipe_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "_run_index_source_from_cli", lambda: None)

    payload = mod._load_run_index()
    assert payload["entry_count_all"] == 4
    assert payload["all_entry_count"] == 4
    assert len(payload["entries"]) <= payload["entries_preview_limit"]
    assert len(payload["entries_all"]) == 4
    first = payload["entries_all"][0]
    assert first["board_or_run"] == "hpipe-alpha-board"
    assert first["goal"] == "first alpha tranche"
    assert first["summary"]
    assert first["source_section"] == "Current entries"
    assert first["source_line"]


def test_snapshot_exposes_historical_stats_and_full_run_reconciliation(hpipe_home, monkeypatch, tmp_path):
    run_index_path = tmp_path / "RUN_INDEX.md"
    run_index_path.write_text(RUN_INDEX_FIXTURE, encoding="utf-8")
    mod = _load_plugin_module()
    monkeypatch.setattr(mod, "_RUN_INDEX_FALLBACK", run_index_path)
    monkeypatch.setattr(mod, "_run_hpipe_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "_run_index_source_from_cli", lambda: None)
    monkeypatch.setattr(mod.time, "time", lambda: NOW)

    archived_root = hpipe_home / "kanban" / "boards" / "_archived"
    archived_dir = archived_root / "hpipe-beta-board-1780000000"
    archived_dir.mkdir(parents=True)
    (archived_dir / "board.json").write_text(
        json.dumps({"slug": "hpipe-beta-board", "name": "Beta archived", "archived": True}),
        encoding="utf-8",
    )
    (archived_dir / "kanban.db").write_bytes(b"")

    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/hpipe")
    r = TestClient(app).get("/api/plugins/hpipe/snapshot")
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["run_index"]["entry_count_all"] == 4
    assert len(data["run_index"]["entries"]) <= data["run_index"]["entries_preview_limit"]
    stats = data["historical_stats"]
    assert stats["entry_count"] == 4
    assert stats["mode_distribution"]["code"] == 2
    assert stats["mode_distribution"]["stage"] == 1
    assert "entries_by_month" in stats

    full = data["full_run_reconciliation"]
    assert full["matched_count"] >= 0
    assert "archived_board_match_count" in full
    assert "index_only_count" in full
    assert full["counts"]["run_index_entries"] == 4
    assert data["run_reconciliation"] == full
    assert any(src["label"] == "Archived board telemetry" for src in data["data_sources"])