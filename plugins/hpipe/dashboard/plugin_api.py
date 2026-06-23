"""Hpipe dashboard plugin backend.

Mounted at /api/plugins/hpipe/ by the Hermes dashboard plugin system.
All routes are read-only and use local Kanban SQLite state plus optional hpipe
CLI output where available. No writes, dispatches, or external calls happen here.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

try:
    from hermes_constants import get_hermes_home
except ImportError:  # pragma: no cover - unit-test fallback
    import os as _os

    def get_hermes_home() -> Path:  # type: ignore[misc]
        val = (_os.environ.get("HERMES_HOME") or "").strip()
        return Path(val) if val else Path.home() / ".hermes"

try:
    from fastapi import APIRouter as FastAPIRouter, Query
except Exception:  # pragma: no cover - lets pure import tests run without FastAPI
    class FastAPIRouter:  # type: ignore
        def get(self, *_args, **_kwargs):
            return lambda fn: fn

    def Query(default=None, **_kwargs):  # type: ignore
        return default

router = FastAPIRouter()

HPIPE_SLUG_RE = re.compile(r"(^|[-_])hpipe([-_]|$)|hpcode|hpharden|hpself|hprun|hpstage", re.I)
EVIDENCE_TERMS = (
    "evidence",
    "verify",
    "verification",
    "test",
    "tests",
    "smoke",
    "py_compile",
    "pytest",
    "diff --check",
    "review",
    "pass",
    "readback",
)
PROOF_TERMS = (
    "proof",
    "proofed",
    "sign-off",
    "signoff",
    "test",
    "tests",
    "pytest",
    "smoke",
    "readback",
    "verification",
    "render",
)
IMPLEMENTATION_TERMS = (
    "implement",
    "implementation",
    "build",
    "backend",
    "frontend",
    "plugin",
    "dashboard",
    "render",
    "payload",
    "bundle",
    "fix",
    "code",
)
REVIEW_TERMS = (
    "review",
    "critique",
    "audit",
    "verify",
    "verification",
    "evidence",
    "closeout",
    "smoke",
    "reviewer",
    "qa",
)
_FAILURE_STATES = {"failed", "crashed", "timed_out", "timeout", "error"}
_OPEN_TASK_STATES = {"todo", "triage", "ready", "running", "blocked", "scheduled", "review"}
_WINDOW_SECONDS = 24 * 60 * 60
_LINEAGE_RUN_LIMIT = 8
_LINEAGE_COMMENT_LIMIT = 8
_LINEAGE_ATTACHMENT_LIMIT = 8
_LINEAGE_EVENT_LIMIT = 20
_LINEAGE_HEARTBEAT_LIMIT = 8
_DEFAULT_THRESHOLDS = {
    "blocked_tasks_warn": 1,
    "failed_runs_warn": 1,
    "evidence_coverage_warn": 0.75,
    "stale_open_24h_warn": 1,
    "stale_open_72h_blocked": 1,
    "stale_open_7d_blocked": 1,
    "last_activity_warn_seconds": _WINDOW_SECONDS,
}
_THRESHOLD_POLICY = {
    "read_only": True,
    "mutation_surface": "none",
    "tuning_surface": "plugins/hpipe/dashboard/plugin_api.py::_DEFAULT_THRESHOLDS",
}
_RUN_INDEX_FALLBACK = Path(
    "/home/david/workspace/skills/autonomous-ai-agents/agent-pipeline-shorthand/RUN_INDEX.md"
)
_RUN_INDEX_ENTRY_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) — (.+?) — (.+?) — (.+)$")
_RUN_INDEX_UI_PREVIEW_LIMIT = 10
_RUN_INDEX_DETAIL_FIELDS = (
    "goal",
    "implemented",
    "deferred",
    "verification",
    "evidence",
    "residual_risk",
)
_RUN_INDEX_DETAIL_RE = re.compile(r"^\s*-\s+([A-Za-z][A-Za-z /_-]*):\s*(.*)\s*$")
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "hpipe", "hpcode",
    "local", "only", "run", "runs", "board", "boards", "task", "tasks", "code", "mode",
    "after", "before", "while", "under", "using", "plus", "keep", "keeps", "remaining",
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value


def _board_root() -> Path:
    return get_hermes_home() / "kanban" / "boards"


def _is_hpipe_slug(slug: str) -> bool:
    return bool(HPIPE_SLUG_RE.search(slug or ""))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _read_rows(conn: sqlite3.Connection, table: str, columns: str = "*") -> list[dict[str, Any]]:
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT {columns} FROM {table}")]
    except sqlite3.Error:
        return []


def _table_available(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (table,),
        ).fetchone()
        return bool(row)
    except sqlite3.Error:
        return False


def _bounded(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    return rows[:limit]


def _source_state(*, available: bool, rows: list[dict[str, Any]], proxy: bool = False) -> str:
    if not available:
        return "unavailable"
    if rows:
        return "proxy" if proxy else "proof"
    return "missing"


def _jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _latest_ts(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> Optional[int]:
    values: list[int] = []
    for row in rows:
        for key in keys:
            ts = _safe_int(row.get(key), 0)
            if ts > 0:
                values.append(ts)
    return max(values) if values else None


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _task_text(task: dict[str, Any]) -> str:
    return "\n".join(
        str(task.get(key) or "")
        for key in ("title", "body", "result")
    ).lower()


def _load_board_meta(slug: str, db_path: Path) -> dict[str, Any]:
    meta = {
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "description": "",
        "default_workdir": None,
        "created_at": None,
        "archived": False,
    }
    board_json = db_path.parent / "board.json"
    if board_json.exists():
        try:
            raw = json.loads(board_json.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                meta["name"] = raw.get("name") or meta["name"]
                meta["description"] = raw.get("description") or meta["description"]
                meta["default_workdir"] = raw.get("default_workdir") or meta["default_workdir"]
                meta["created_at"] = raw.get("created_at") or meta["created_at"]
                meta["archived"] = bool(raw.get("archived", meta["archived"]))
        except Exception:
            pass
    return meta


def _time_window(ts: Any, now: int, window_seconds: int = _WINDOW_SECONDS) -> Optional[str]:
    val = _safe_int(ts, 0)
    if val <= 0:
        return None
    age = now - val
    if age < 0:
        return None
    if age < window_seconds:
        return "current"
    if age < 2 * window_seconds:
        return "previous"
    return None


def _evidence_haystack(task: dict[str, Any], comments: list[dict[str, Any]], runs: list[dict[str, Any]], attachments: list[dict[str, Any]]) -> str:
    chunks = [str(task.get("body") or ""), str(task.get("result") or "")]
    chunks.extend(str(c.get("body") or "") for c in comments)
    chunks.extend(str(r.get("summary") or "") for r in runs)
    chunks.extend(str(r.get("metadata") or "") for r in runs)
    chunks.extend(str(a.get("filename") or "") for a in attachments)
    return "\n".join(chunks).lower()


def _has_activity_proxy(task: dict[str, Any], comments: list[dict[str, Any]], runs: list[dict[str, Any]], attachments: list[dict[str, Any]]) -> bool:
    haystack = _evidence_haystack(task, comments, runs, attachments)
    return any(term in haystack for term in EVIDENCE_TERMS) or bool(runs) or bool(attachments)


def _has_proof_signals(task: dict[str, Any], comments: list[dict[str, Any]], runs: list[dict[str, Any]], attachments: list[dict[str, Any]]) -> bool:
    haystack = _evidence_haystack(task, comments, runs, attachments)
    return any(term in haystack for term in PROOF_TERMS)


def _has_evidence(task: dict[str, Any], comments: list[dict[str, Any]], runs: list[dict[str, Any]], attachments: list[dict[str, Any]]) -> bool:
    return _has_activity_proxy(task, comments, runs, attachments)


def _coverage_payloads(*, tasks_total: int, activity_proxy_count: int, proof_evidence_count: int) -> dict[str, dict[str, Any]]:
    activity_proxy_ratio = (activity_proxy_count / tasks_total) if tasks_total else 0.0
    proof_evidence_ratio = (proof_evidence_count / tasks_total) if tasks_total else 0.0
    activity = {
        "label": "activity/proxy evidence coverage",
        "tasks_total": tasks_total,
        "tasks_with_evidence_signals": activity_proxy_count,
        "tasks_with_activity_proxy": activity_proxy_count,
        "coverage_ratio": activity_proxy_ratio,
        "activity_proxy_ratio": activity_proxy_ratio,
    }
    proof = {
        "label": "strong proof coverage",
        "tasks_total": tasks_total,
        "tasks_with_proof_signals": proof_evidence_count,
        "coverage_ratio": proof_evidence_ratio,
        "proof_coverage_ratio": proof_evidence_ratio,
    }
    legacy = {
        "tasks_total": tasks_total,
        "tasks_with_evidence_signals": activity_proxy_count,
        "tasks_with_proof_signals": proof_evidence_count,
        "tasks_with_activity_proxy": activity_proxy_count,
        "coverage_ratio": proof_evidence_ratio,
        "proof_coverage_ratio": proof_evidence_ratio,
        "activity_proxy_ratio": activity_proxy_ratio,
    }
    return {
        "evidence_coverage": legacy,
        "activity_evidence_coverage": activity,
        "proxy_evidence_coverage": dict(activity),
        "proof_coverage": proof,
        "strong_proof_coverage": dict(proof),
    }


def _proof_state(task: dict[str, Any], comments: list[dict[str, Any]], runs: list[dict[str, Any]], attachments: list[dict[str, Any]]) -> dict[str, str]:
    if not _has_activity_proxy(task, comments, runs, attachments):
        return {"state": "missing", "detail": "no evidence signals"}
    if _has_proof_signals(task, comments, runs, attachments):
        return {"state": "proof", "detail": "explicit proof signals present"}
    return {"state": "proxy", "detail": "activity evidence proxy present; proof not explicit"}


def _lineage_payload(
    task: dict[str, Any],
    runs: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
    events: list[dict[str, Any]] | None = None,
    *,
    source_available: dict[str, bool] | None = None,
) -> dict[str, Any]:
    events = events or []
    source_available = source_available or {}
    task_id = str(task.get("id") or "")
    sorted_runs = sorted(runs, key=lambda row: (_safe_int(row.get("started_at"), 0), _safe_int(row.get("ended_at"), 0), _safe_int(row.get("id"), 0)))
    sorted_comments = sorted(comments, key=lambda row: (_safe_int(row.get("created_at"), 0), _safe_int(row.get("id"), 0)))
    sorted_attachments = sorted(attachments, key=lambda row: (_safe_int(row.get("created_at"), 0), _safe_int(row.get("id"), 0)))
    sorted_events = sorted(events, key=lambda row: (_safe_int(row.get("created_at"), 0), _safe_int(row.get("id"), 0)))
    heartbeat_events = [row for row in sorted_events if str(row.get("kind") or "").lower() == "heartbeat"]
    run_heartbeats = [
        {
            "run_id": row.get("id"),
            "profile": str(row.get("profile") or "unknown"),
            "created_at": _safe_int(row.get("last_heartbeat_at"), 0) or None,
            "source": "task_runs.last_heartbeat_at",
        }
        for row in sorted_runs
        if _safe_int(row.get("last_heartbeat_at"), 0) > 0
    ]
    heartbeats = sorted(
        [
            {
                "run_id": row.get("run_id"),
                "payload": _jsonish(row.get("payload")),
                "created_at": _safe_int(row.get("created_at"), 0) or None,
                "source": "task_events.heartbeat",
            }
            for row in heartbeat_events
        ]
        + run_heartbeats,
        key=lambda row: (_safe_int(row.get("created_at"), 0), _safe_int(row.get("run_id"), 0)),
    )
    run_rows = _bounded(sorted_runs, _LINEAGE_RUN_LIMIT)
    comment_rows = _bounded(sorted_comments, _LINEAGE_COMMENT_LIMIT)
    event_rows = _bounded(sorted_events, _LINEAGE_EVENT_LIMIT)
    attachment_rows = _bounded(sorted_attachments, _LINEAGE_ATTACHMENT_LIMIT)
    heartbeat_rows = _bounded(heartbeats, _LINEAGE_HEARTBEAT_LIMIT)
    has_task_row = bool(task_id)
    claim_state = "proof" if has_task_row else "unavailable"
    runs_available = source_available.get("task_runs", True)
    comments_available = source_available.get("task_comments", True)
    events_available = source_available.get("task_events", True)
    attachments_available = source_available.get("task_attachments", True)
    return {
        "claim_state": claim_state,
        "runs_state": _source_state(available=runs_available, rows=sorted_runs),
        "comments_state": _source_state(available=comments_available, rows=sorted_comments, proxy=True),
        "events_state": _source_state(available=events_available, rows=sorted_events),
        "heartbeats_state": _source_state(available=events_available or runs_available, rows=heartbeats),
        "attachments_state": _source_state(available=attachments_available, rows=sorted_attachments, proxy=True),
        "bounded": {
            "runs_limit": _LINEAGE_RUN_LIMIT,
            "comments_limit": _LINEAGE_COMMENT_LIMIT,
            "events_limit": _LINEAGE_EVENT_LIMIT,
            "heartbeats_limit": _LINEAGE_HEARTBEAT_LIMIT,
            "attachments_limit": _LINEAGE_ATTACHMENT_LIMIT,
            "runs_total": len(sorted_runs),
            "comments_total": len(sorted_comments),
            "events_total": len(sorted_events),
            "heartbeats_total": len(heartbeats),
            "attachments_total": len(sorted_attachments),
        },
        "claim": {
            "task_id": task_id,
            "title": str(task.get("title") or ""),
            "assignee": str(task.get("assignee") or ""),
            "status": str(task.get("status") or ("unavailable" if not has_task_row else "unknown")),
            "created_at": _safe_int(task.get("created_at"), 0) or None,
            "started_at": _safe_int(task.get("started_at"), 0) or None,
            "completed_at": _safe_int(task.get("completed_at"), 0) or None,
        },
        "runs": [
            {
                "id": row.get("id"),
                "profile": str(row.get("profile") or "unknown"),
                "status": str(row.get("status") or "unknown"),
                "outcome": str(row.get("outcome") or row.get("status") or ""),
                "claim_lock": row.get("claim_lock"),
                "claim_expires": _safe_int(row.get("claim_expires"), 0) or None,
                "worker_pid": row.get("worker_pid"),
                "last_heartbeat_at": _safe_int(row.get("last_heartbeat_at"), 0) or None,
                "started_at": _safe_int(row.get("started_at"), 0) or None,
                "ended_at": _safe_int(row.get("ended_at"), 0) or None,
                "summary": str(row.get("summary") or ""),
                "error": str(row.get("error") or ""),
            }
            for row in run_rows
        ],
        "comments": [
            {
                "author": str(row.get("author") or ""),
                "body": str(row.get("body") or ""),
                "created_at": _safe_int(row.get("created_at"), 0) or None,
            }
            for row in comment_rows
        ],
        "events": [
            {
                "kind": str(row.get("kind") or "event"),
                "run_id": row.get("run_id"),
                "payload": _jsonish(row.get("payload")),
                "created_at": _safe_int(row.get("created_at"), 0) or None,
            }
            for row in event_rows
        ],
        "heartbeats": heartbeat_rows,
        "attachments": [
            {
                "filename": str(row.get("filename") or ""),
                "size": _safe_int(row.get("size"), 0),
                "created_at": _safe_int(row.get("created_at"), 0) or None,
            }
            for row in attachment_rows
        ],
        "result": str(task.get("result") or ""),
    }


def _failure_category(run: dict[str, Any]) -> str:
    text = " ".join(
        str(run.get(field) or "")
        for field in ("status", "outcome", "summary", "error", "metadata")
    ).lower()
    if any(term in text for term in ("protocol violation", "did not call kanban_complete", "did not call kanban_block")):
        return "protocol_violation"
    if any(term in text for term in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(term in text for term in ("crash", "crashed", "panic", "segmentation fault")):
        return "crash"
    if any(term in text for term in ("auth", "permission", "forbidden", "unauthorized", "denied")):
        return "auth_permission"
    if any(term in text for term in ("dependency", "install", "module not found", "importerror")):
        return "dependency_install"
    if any(term in text for term in ("claim", "lock", "conflict", "already running")):
        return "board_claim_conflict"
    if any(term in text for term in ("tool", "environment", "exec format", "no such file or directory")):
        return "tool_environment_failure"
    if any(term in text for term in ("verification", "pytest", "smoke", "test", "check")):
        return "verification_failure"
    return "other"


def _role_match(task_text: str, profile: str, terms: tuple[str, ...], prefixes: tuple[str, ...]) -> bool:
    p = profile.lower()
    if any(p.startswith(prefix) for prefix in prefixes):
        return True
    return any(term in task_text for term in terms)


def _max_concurrency(runs: list[dict[str, Any]], now: Optional[int] = None) -> dict[str, Any]:
    now_ts = int(now or time.time())
    points: list[tuple[int, int]] = []
    for run in runs:
        start = _safe_int(run.get("started_at"), 0)
        end = _safe_int(run.get("ended_at"), 0) or now_ts
        if start <= 0 or end < start:
            continue
        points.append((start, 1))
        points.append((end, -1))
    points.sort(key=lambda item: (item[0], -item[1]))
    active = 0
    peak = 0
    first_seen = None
    timeline: list[dict[str, Any]] = []
    for ts, delta in points:
        active += delta
        timeline.append({"ts": ts, "active": max(active, 0)})
        if active > peak:
            peak = active
            first_seen = ts
    return {
        "max": max(peak, 0),
        "first_seen": first_seen,
        "interval_count": len(points) // 2,
        "timeline": timeline,
    }


def _board_health(*, task_count: int, blocked_tasks: int, failed_runs: int, evidence_ratio: float, stale_24h: int, stale_72h: int, stale_7d: int, last_activity_age_seconds: Optional[int], thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    thresholds = thresholds or _DEFAULT_THRESHOLDS
    reasons: list[str] = []
    state = "healthy"

    if task_count == 0:
        return {
            "state": "degraded",
            "reasons": ["no tasks recorded"],
            "thresholds": thresholds,
        }

    if blocked_tasks:
        reasons.append(f"{blocked_tasks} blocked task{'s' if blocked_tasks != 1 else ''}")
    if failed_runs:
        reasons.append(f"{failed_runs} failed run{'s' if failed_runs != 1 else ''}")
    if stale_24h:
        reasons.append(f"{stale_24h} open task{'s' if stale_24h != 1 else ''} untouched for 24h")
    if stale_72h:
        reasons.append(f"{stale_72h} open task{'s' if stale_72h != 1 else ''} untouched for 72h")
    if stale_7d:
        reasons.append(f"{stale_7d} open task{'s' if stale_7d != 1 else ''} untouched for 7d")
    if evidence_ratio < float(thresholds["evidence_coverage_warn"]):
        reasons.append(f"evidence coverage {evidence_ratio:.0%} below target")
    if last_activity_age_seconds is not None and last_activity_age_seconds > int(thresholds["last_activity_warn_seconds"]):
        reasons.append(f"last activity {last_activity_age_seconds // 3600}h ago")

    if blocked_tasks and stale_72h:
        state = "blocked"
    elif failed_runs or stale_24h or evidence_ratio < float(thresholds["evidence_coverage_warn"]):
        state = "attention"
    elif last_activity_age_seconds is not None and last_activity_age_seconds > int(thresholds["last_activity_warn_seconds"]):
        state = "degraded"

    if not reasons:
        reasons = ["all tracked metrics are within threshold"]

    return {"state": state, "reasons": reasons, "thresholds": thresholds}


def _board_snapshot(slug: str, db_path: Path, now: Optional[int] = None) -> Optional[dict[str, Any]]:
    if not db_path.exists():
        return None
    now_ts = int(now or time.time())
    conn = sqlite3.connect(str(db_path))
    try:
        source_available = {
            "tasks": _table_available(conn, "tasks"),
            "task_runs": _table_available(conn, "task_runs"),
            "task_comments": _table_available(conn, "task_comments"),
            "task_attachments": _table_available(conn, "task_attachments"),
            "task_events": _table_available(conn, "task_events"),
        }
        tasks = _read_rows(conn, "tasks")
        runs = _read_rows(conn, "task_runs")
        comments = _read_rows(conn, "task_comments")
        attachments = _read_rows(conn, "task_attachments")
        events = _read_rows(conn, "task_events")
    finally:
        conn.close()

    meta = _load_board_meta(slug, db_path)
    task_statuses = Counter(str(t.get("status") or "unknown") for t in tasks)
    run_statuses = Counter(str(r.get("status") or "unknown") for r in runs)
    run_outcomes = Counter(str(r.get("outcome") or r.get("status") or "unknown") for r in runs)
    by_task_comments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_task_runs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_task_attachments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_task_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    task_lookup: dict[str, dict[str, Any]] = {}

    for row in comments:
        by_task_comments[str(row.get("task_id"))].append(row)
    for row in runs:
        by_task_runs[str(row.get("task_id"))].append(row)
    for row in attachments:
        by_task_attachments[str(row.get("task_id"))].append(row)
    for row in events:
        by_task_events[str(row.get("task_id"))].append(row)
    for task in tasks:
        task_lookup[str(task.get("id") or "")] = task

    proof_evidence_count = 0
    activity_proxy_count = 0
    open_task_ages: list[int] = []
    profile_counts: dict[str, dict[str, Any]] = {}
    failure_breakdown = Counter()
    failure_reasons = Counter()
    recent_runs: list[dict[str, Any]] = []
    last_activity = 0
    completed_current = completed_previous = 0
    failed_current = failed_previous = 0
    evidence_current = evidence_previous = 0
    board_active_current = board_active_previous = 0
    board_created_current = board_created_previous = 0

    board_created_window = _time_window(meta.get("created_at"), now_ts)
    if board_created_window == "current":
        board_created_current += 1
    elif board_created_window == "previous":
        board_created_previous += 1

    for task in tasks:
        task_id = str(task.get("id") or "")
        task_runs = by_task_runs[task_id]
        task_comments = by_task_comments[task_id]
        task_attachments = by_task_attachments[task_id]
        proof = _proof_state(task, task_comments, task_runs, task_attachments)
        profile = str(task.get("assignee") or "unassigned")
        bucket = profile_counts.setdefault(
            profile,
            {
                "profile": profile,
                "task_count": 0,
                "run_count": 0,
                "completed_runs": 0,
                "failed_runs": 0,
                "completed_tasks": 0,
                "blocked_tasks": 0,
                "implementation_hits": 0,
                "review_hits": 0,
            },
        )
        bucket["task_count"] += 1
        status = _normalize_text(task.get("status"))
        if status in {"done", "completed"}:
            bucket["completed_tasks"] += 1
        if status == "blocked":
            bucket["blocked_tasks"] += 1
        task_text = _task_text(task)
        if _role_match(task_text, profile, IMPLEMENTATION_TERMS, ("coder", "engineer", "dev", "impl")):
            bucket["implementation_hits"] += 1
        if _role_match(task_text, profile, REVIEW_TERMS, ("review", "auditor", "critic", "qa")):
            bucket["review_hits"] += 1

        if _has_activity_proxy(task, task_comments, task_runs, task_attachments):
            activity_proxy_count += 1
        if _has_proof_signals(task, task_comments, task_runs, task_attachments):
            proof_evidence_count += 1
            w = _time_window(task.get("completed_at") or task.get("started_at") or task.get("created_at"), now_ts)
            if w == "current":
                evidence_current += 1
            elif w == "previous":
                evidence_previous += 1

        if status in _OPEN_TASK_STATES:
            age_source = task.get("started_at") or task.get("created_at") or task.get("completed_at")
            age = _safe_int(now_ts - _safe_int(age_source, 0), 0)
            if age > 0:
                open_task_ages.append(age)

        task_activity = _latest_ts([task] + task_runs + task_comments + task_attachments, ("completed_at", "ended_at", "started_at", "created_at"))
        if task_activity and task_activity > last_activity:
            last_activity = task_activity

    for run in runs:
        profile = str(run.get("profile") or "unknown")
        bucket = profile_counts.setdefault(
            profile,
            {
                "profile": profile,
                "task_count": 0,
                "run_count": 0,
                "completed_runs": 0,
                "failed_runs": 0,
                "completed_tasks": 0,
                "blocked_tasks": 0,
                "implementation_hits": 0,
                "review_hits": 0,
            },
        )
        bucket["run_count"] += 1
        state = _normalize_text(run.get("outcome") or run.get("status"))
        if state in {"completed", "done"}:
            bucket["completed_runs"] += 1
        if state in _FAILURE_STATES or run.get("error"):
            bucket["failed_runs"] += 1
        cat = _failure_category(run)
        if state in _FAILURE_STATES or run.get("error"):
            failure_breakdown[cat] += 1
            reason = str(run.get("error") or run.get("summary") or run.get("outcome") or run.get("status") or "unknown")
            failure_reasons[reason] += 1

        started = _safe_int(run.get("started_at"), 0)
        ended = _safe_int(run.get("ended_at"), 0)
        run_time = ended or started
        w = _time_window(run_time, now_ts)
        if w == "current":
            if state in _FAILURE_STATES or run.get("error"):
                failed_current += 1
        elif w == "previous":
            if state in _FAILURE_STATES or run.get("error"):
                failed_previous += 1

        task_ref = task_lookup.get(str(run.get("task_id") or ""), {})
        task_comments = by_task_comments[str(run.get("task_id") or "")]
        task_runs = by_task_runs[str(run.get("task_id") or "")]
        task_attachments = by_task_attachments[str(run.get("task_id") or "")]
        task_events = by_task_events[str(run.get("task_id") or "")]
        proof = _proof_state(task_ref, task_comments, task_runs, task_attachments)
        run_item = {
            "board_slug": slug,
            "board_name": meta["name"],
            "task_id": str(run.get("task_id") or ""),
            "task_title": task_ref.get("title") or "",
            "task_status": task_ref.get("status") or "unknown",
            "profile": profile,
            "status": state or "unknown",
            "outcome": str(run.get("outcome") or run.get("status") or ""),
            "started_at": started or None,
            "ended_at": ended or None,
            "summary": str(run.get("summary") or ""),
            "error": str(run.get("error") or ""),
            "failure_category": cat,
            "proof_state": proof["state"],
            "proof_detail": proof["detail"],
            "lineage": _lineage_payload(task_ref, task_runs, task_comments, task_attachments, task_events, source_available=source_available),
            "links": {
                "kanban": f"/kanban?board={slug}&task={run.get('task_id')}",
                "report": f"hpipe report {slug}",
                "insights": f"hpipe insights --json {slug}",
                "run_index": "hpipe run-index --json",
            },
        }
        recent_runs.append(run_item)
        if run_time and run_time > last_activity:
            last_activity = run_time

        if w == "current":
            board_active_current = 1
        elif w == "previous":
            board_active_previous = 1

    open_tasks = sum(count for status, count in task_statuses.items() if status in _OPEN_TASK_STATES)
    blocked_tasks = task_statuses.get("blocked", 0)
    done_tasks = task_statuses.get("done", 0)
    failed_runs = sum(1 for r in runs if _normalize_text(r.get("status") or r.get("outcome")) in _FAILURE_STATES or r.get("error"))
    reclaimed_runs = sum(1 for r in runs if _normalize_text(r.get("status") or r.get("outcome")) == "reclaimed")
    completion_ratio = (done_tasks / len(tasks)) if tasks else 0.0
    concurrency = _max_concurrency(runs, now_ts)
    current_runs = [r for r in runs if _time_window(r.get("started_at"), now_ts) == "current"]
    previous_runs = [r for r in runs if _time_window(r.get("started_at"), now_ts) == "previous"]
    current_concurrency = _max_concurrency(current_runs, now_ts)["max"]
    previous_concurrency = _max_concurrency(previous_runs, now_ts)["max"]

    oldest_open_task_age_seconds = max(open_task_ages) if open_task_ages else None
    stale_open_24h = sum(1 for age in open_task_ages if age >= _WINDOW_SECONDS)
    stale_open_72h = sum(1 for age in open_task_ages if age >= 3 * _WINDOW_SECONDS)
    stale_open_7d = sum(1 for age in open_task_ages if age >= 7 * _WINDOW_SECONDS)
    last_activity_age_seconds = (now_ts - last_activity) if last_activity else None
    coverage_payloads = _coverage_payloads(
        tasks_total=len(tasks),
        activity_proxy_count=activity_proxy_count,
        proof_evidence_count=proof_evidence_count,
    )
    proof_evidence_ratio = coverage_payloads["proof_coverage"]["coverage_ratio"]
    health = _board_health(
        task_count=len(tasks),
        blocked_tasks=blocked_tasks,
        failed_runs=failed_runs,
        evidence_ratio=proof_evidence_ratio,
        stale_24h=stale_open_24h,
        stale_72h=stale_open_72h,
        stale_7d=stale_open_7d,
        last_activity_age_seconds=last_activity_age_seconds,
    )

    recent_runs.sort(key=lambda row: (row.get("ended_at") or row.get("started_at") or 0, row.get("started_at") or 0), reverse=True)
    profiles = sorted(profile_counts.values(), key=lambda p: (p["run_count"], p["task_count"], p["completed_tasks"]), reverse=True)

    delta = {
        "window_seconds": _WINDOW_SECONDS,
        "completed_tasks": {
            "current": sum(1 for task in tasks if _normalize_text(task.get("status")) in {"done", "completed"} and _time_window(task.get("completed_at"), now_ts) == "current"),
            "previous": sum(1 for task in tasks if _normalize_text(task.get("status")) in {"done", "completed"} and _time_window(task.get("completed_at"), now_ts) == "previous"),
        },
        "failed_runs": {"current": failed_current, "previous": failed_previous},
        "evidence_tasks": {"current": evidence_current, "previous": evidence_previous},
        "max_concurrency": {"current": current_concurrency, "previous": previous_concurrency},
        "new_boards": {"current": 1 if board_created_window == "current" else 0, "previous": 1 if board_created_window == "previous" else 0},
        "active_boards": {"current": board_active_current, "previous": board_active_previous},
    }

    return {
        "slug": slug,
        "name": meta["name"],
        "description": meta["description"],
        "default_workdir": meta["default_workdir"],
        "created_at": _safe_int(meta.get("created_at"), 0) or None,
        "last_worked_at": last_activity,
        "db_path": str(db_path),
        "task_count": len(tasks),
        "run_count": len(runs),
        "task_statuses": dict(task_statuses),
        "run_statuses": dict(run_statuses),
        "run_outcomes": dict(run_outcomes),
        "open_tasks": open_tasks,
        "blocked_tasks": blocked_tasks,
        "done_tasks": done_tasks,
        "failed_runs": failed_runs,
        "reclaimed_runs": reclaimed_runs,
        "completion_ratio": completion_ratio,
        **coverage_payloads,
        "concurrency": concurrency,
        "max_concurrent_task_runs": concurrency["max"],
        "max_concurrent_first_seen": concurrency["first_seen"],
        "profiles": profiles[:10],
        "leaderboards": {
            "profile_activity": profiles[:10],
            "implementation_heavy": sorted(profiles, key=lambda p: (p["implementation_hits"], p["run_count"], p["task_count"]), reverse=True)[:10],
            "review_heavy": sorted(profiles, key=lambda p: (p["review_hits"], p["run_count"], p["task_count"]), reverse=True)[:10],
            "run_volume": sorted(profiles, key=lambda p: (p["run_count"], p["completed_runs"], p["task_count"]), reverse=True)[:10],
            "completion": sorted(profiles, key=lambda p: (p["completed_tasks"], p["task_count"], p["run_count"]), reverse=True)[:10],
        },
        "coder_activity_proxy": {
            "top_profile": (sorted(profiles, key=lambda p: (p["implementation_hits"], p["run_count"], p["task_count"]), reverse=True)[0]["profile"] if profiles else None),
            "profiles": sorted(profiles, key=lambda p: (p["implementation_hits"], p["run_count"], p["task_count"]), reverse=True)[:8],
        },
        "review_activity_proxy": {
            "top_profile": (sorted(profiles, key=lambda p: (p["review_hits"], p["run_count"], p["task_count"]), reverse=True)[0]["profile"] if profiles else None),
            "profiles": sorted(profiles, key=lambda p: (p["review_hits"], p["run_count"], p["task_count"]), reverse=True)[:8],
        },
        "failure_breakdown": {
            "by_category": dict(failure_breakdown),
            "top_reasons": [
                {"reason": reason, "count": count}
                for reason, count in failure_reasons.most_common(5)
            ],
        },
        "staleness": {
            "oldest_open_task_age_seconds": oldest_open_task_age_seconds,
            "stale_open_tasks_24h": stale_open_24h,
            "stale_open_tasks_72h": stale_open_72h,
            "stale_open_tasks_7d": stale_open_7d,
            "last_activity_age_seconds": last_activity_age_seconds,
        },
        "health": health,
        "recent_runs": recent_runs[:10],
        "all_runs": recent_runs,
        "delta": delta,
        "thresholds": _DEFAULT_THRESHOLDS,
    }


def _iter_hpipe_board_dirs(*, include_archived: bool = False) -> list[tuple[str, Path, bool]]:
    root = _board_root()
    if not root.exists():
        return []
    rows: list[tuple[str, Path, bool]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("_") or not _is_hpipe_slug(child.name):
            continue
        rows.append((child.name, child, False))
    if include_archived:
        archived_root = root / "_archived"
        if archived_root.exists():
            for child in sorted(archived_root.iterdir()):
                if not child.is_dir():
                    continue
                slug = child.name
                # Archived board directories commonly carry a timestamp suffix; keep
                # both the physical slug and a base slug for reconciliation.
                if not _is_hpipe_slug(slug):
                    continue
                rows.append((slug, child, True))
    return rows


def _archived_slug_base(slug: str) -> str:
    return re.sub(r"-\d{8,}(?:T?\d{6,})?(?:Z)?$", "", slug or "")


def _load_hpipe_boards(limit: int = 20, now: Optional[int] = None) -> list[dict[str, Any]]:
    boards = []
    now_ts = int(now or time.time())
    for slug, child, archived in _iter_hpipe_board_dirs(include_archived=False):
        snap = _board_snapshot(slug, child / "kanban.db", now_ts)
        if snap:
            snap["archived"] = archived
            boards.append(snap)
    boards.sort(key=lambda b: b.get("last_worked_at") or b.get("created_at") or 0, reverse=True)
    return boards[:limit]


def _run_hpipe_json(args: list[str], timeout: int = 12) -> Optional[Any]:
    exe = shutil.which("hpipe")
    if not exe:
        return None
    try:
        cp = subprocess.run([exe, *args], text=True, capture_output=True, timeout=timeout, check=False)
    except Exception:
        return None
    if cp.returncode != 0:
        return None
    try:
        return json.loads(cp.stdout)
    except Exception:
        return None


def _run_index_source_from_cli() -> Optional[Path]:
    payload = _run_hpipe_json(["run-index", "--json"], timeout=10)
    if isinstance(payload, dict):
        path = payload.get("path") or payload.get("source")
        if path:
            return Path(str(path))
    return None


def _parse_run_index_text(text: str, source: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    current_detail: Optional[str] = None
    in_current = False

    def finish() -> None:
        nonlocal current, current_detail
        if current:
            summary = current.get("goal") or current.get("implemented") or current.get("summary") or ""
            current["summary"] = str(summary)
            entries.append(current)
        current = None
        current_detail = None

    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.startswith("## Current entries"):
            in_current = True
            continue
        if not in_current:
            continue
        if line.startswith("## ") and not line.startswith("## Current entries"):
            break
        if not line.strip():
            continue
        match = _RUN_INDEX_ENTRY_RE.match(line)
        if match:
            finish()
            date, slug, mode, target_path = [part.strip() for part in match.groups()]
            current = {
                "date": date,
                "board_or_run": slug,
                "slug": slug,
                "mode": mode,
                "target_path": target_path,
                "source_line": line_no,
                "source_section": "Current entries",
                "source": str(source),
            }
            current_detail = None
            continue
        if not current:
            continue
        detail = _RUN_INDEX_DETAIL_RE.match(line)
        if detail:
            key = re.sub(r"[^a-z0-9]+", "_", detail.group(1).strip().lower()).strip("_")
            current[key] = detail.group(2).strip()
            current_detail = key
        elif current_detail and line.startswith("  ") and line.strip():
            current[current_detail] = (str(current.get(current_detail) or "") + " " + line.strip()).strip()
    finish()
    return entries


def _load_run_index_full(display_limit: int = _RUN_INDEX_UI_PREVIEW_LIMIT) -> dict[str, Any]:
    cli_payload = _run_hpipe_json(["run-index", "--json"], timeout=10)
    source = _run_index_source_from_cli()
    candidates = [p for p in (source, _RUN_INDEX_FALLBACK) if p]
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if not path.exists():
                continue
            entries = _parse_run_index_text(path.read_text(encoding="utf-8"), path)
            if not entries and isinstance(cli_payload, dict):
                entries = [
                    {
                        **entry,
                        "board_or_run": str(entry.get("board_or_run") or entry.get("slug") or ""),
                        "summary": str(entry.get("goal") or entry.get("implemented") or entry.get("board_or_run") or ""),
                        "source_section": "Current entries",
                        "source": str(path),
                    }
                    for entry in (cli_payload.get("entries") or [])
                    if isinstance(entry, dict)
                ]
            strategy = "hpipe_cli" if source and path == source else "file_fallback"
            if isinstance(cli_payload, dict) and entries:
                strategy = "hpipe_cli+file" if source and path == source else "file_fallback"
            return {
                "available": bool(entries) or bool(path.exists()),
                "source": str(path),
                "discovery": strategy,
                "entries_all": entries,
                "entries": entries[:display_limit],
                "entry_count": len(entries),
                "entry_count_all": len(entries),
                "all_entry_count": len(entries),
                "unbounded_entry_count": len(entries),
                "bounded_entry_count": min(len(entries), display_limit),
                "bounded_display_limit": display_limit,
                "entries_preview_limit": display_limit,
                "source_strategy": strategy,
                "cli_ok": isinstance(cli_payload, dict),
                "file_ok": True,
            }
        except Exception as exc:
            return {
                "available": False,
                "source": str(path),
                "discovery": "error",
                "entries_all": [],
                "entries": [],
                "entry_count": 0,
                "entry_count_all": 0,
                "all_entry_count": 0,
                "unbounded_entry_count": 0,
                "bounded_entry_count": 0,
                "bounded_display_limit": display_limit,
                "entries_preview_limit": display_limit,
                "error": str(exc),
            }
    return {
        "available": False,
        "source": str(_RUN_INDEX_FALLBACK),
        "discovery": "unavailable",
        "entries_all": [],
        "entries": [],
        "entry_count": 0,
        "entry_count_all": 0,
        "all_entry_count": 0,
        "unbounded_entry_count": 0,
        "bounded_entry_count": 0,
        "bounded_display_limit": display_limit,
        "entries_preview_limit": display_limit,
    }


def _load_run_index() -> dict[str, Any]:
    # Backwards-compatible payload name; unlike the old implementation this is
    # backed by the full RUN_INDEX.md, with only the display list bounded.
    return _load_run_index_full(display_limit=_RUN_INDEX_UI_PREVIEW_LIMIT)


def _historical_stats(run_index: dict[str, Any], boards: list[dict[str, Any]], reconciliation: dict[str, Any]) -> dict[str, Any]:
    entries = [
        entry
        for entry in (run_index.get("entries_all") or run_index.get("entries") or [])
        if isinstance(entry, dict)
    ]
    all_count = int(run_index.get("all_entry_count") or run_index.get("entry_count") or len(entries))
    dates = sorted(str(entry.get("date") or "") for entry in entries if entry.get("date"))
    mode_counts = Counter(str(entry.get("mode") or "unknown") for entry in entries)
    entries_by_date = Counter(str(entry.get("date") or "undated") for entry in entries)
    entries_by_month = Counter(str(entry.get("date") or "undated")[:7] if entry.get("date") else "undated" for entry in entries)
    term_counter: Counter[str] = Counter()
    for entry in entries:
        text = " ".join(str(entry.get(key) or "") for key in ("goal", "implemented", "summary"))
        for term in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text.lower()):
            if term not in _STOPWORDS:
                term_counter[term] += 1
    return {
        "available": bool(run_index.get("available")),
        "source": run_index.get("source"),
        "entry_count": all_count,
        "all_entry_count": all_count,
        "unbounded_entry_count": all_count,
        "bounded_entry_count": int(
            run_index.get("bounded_entry_count")
            or min(all_count, int(run_index.get("bounded_display_limit") or _RUN_INDEX_UI_PREVIEW_LIMIT))
        ),
        "bounded_display_limit": run_index.get("bounded_display_limit"),
        "mode_counts": dict(mode_counts),
        "mode_distribution": dict(mode_counts),
        "entries_by_date": dict(sorted(entries_by_date.items())),
        "entries_by_month": dict(sorted(entries_by_month.items())),
        "first_seen": dates[0] if dates else None,
        "last_seen": dates[-1] if dates else None,
        "hpipe_slug_count": len({str(entry.get("board_or_run") or entry.get("slug") or "") for entry in entries if entry.get("board_or_run") or entry.get("slug")}),
        "index_only_count": reconciliation.get("index_only_count", 0),
        "board_only_count": reconciliation.get("live_board_only_count", reconciliation.get("board_only_count", 0)),
        "archived_board_only_count": reconciliation.get("archived_board_only_count", 0),
        "matched_count": reconciliation.get("matched_count", 0),
        "archived_board_match_count": reconciliation.get("archived_board_match_count", 0),
        "top_terms": [{"term": term, "count": count} for term, count in term_counter.most_common(12)],
        "goal_keyword_counts": dict(term_counter.most_common(12)),
        "entries": entries,
        "explanation": "Full historical stats are parsed from RUN_INDEX.md metadata. Board/task/run telemetry remains limited to local Kanban board databases; archived boards contribute only when their local board directories are present.",
    }


def _aggregate(boards: list[dict[str, Any]], now: Optional[int] = None) -> dict[str, Any]:
    now_ts = int(now or time.time())
    task_statuses: Counter[str] = Counter()
    run_statuses: Counter[str] = Counter()
    profiles: dict[str, dict[str, Any]] = {}
    failure_categories = Counter()
    failure_reasons = Counter()
    max_concurrent = 0
    max_board = None
    evidence_total = 0
    evidence_hit = 0
    activity_proxy_hit = 0
    failed_runs = 0
    reclaimed_runs = 0
    blocked_tasks = 0
    open_tasks = 0
    done_tasks = 0
    board_created_current = 0
    board_created_previous = 0
    board_active_current = 0
    board_active_previous = 0
    stale_board_24h = 0
    stale_board_72h = 0
    stale_board_7d = 0
    board_inventory: list[dict[str, Any]] = []
    recent_runs: list[dict[str, Any]] = []
    all_runs: list[dict[str, Any]] = []

    for board in boards:
        task_statuses.update(board.get("task_statuses", {}))
        run_statuses.update(board.get("run_statuses", {}))
        failed_runs += int(board.get("failed_runs") or 0)
        reclaimed_runs += int(board.get("reclaimed_runs") or 0)
        blocked_tasks += int(board.get("blocked_tasks") or 0)
        open_tasks += int(board.get("open_tasks") or 0)
        done_tasks += int(board.get("done_tasks") or 0)
        cov = board.get("evidence_coverage") or {}
        evidence_total += int(cov.get("tasks_total") or 0)
        evidence_hit += int(cov.get("tasks_with_proof_signals", cov.get("tasks_with_evidence_signals") or 0) or 0)
        activity_proxy_hit += int(cov.get("tasks_with_activity_proxy", cov.get("tasks_with_evidence_signals") or 0) or 0)
        if int(board.get("max_concurrent_task_runs") or 0) > max_concurrent:
            max_concurrent = int(board.get("max_concurrent_task_runs") or 0)
            max_board = board.get("slug")

        for p in board.get("profiles", []):
            name = p.get("profile") or "unknown"
            dest = profiles.setdefault(
                name,
                {
                    "profile": name,
                    "task_count": 0,
                    "run_count": 0,
                    "completed_runs": 0,
                    "failed_runs": 0,
                    "completed_tasks": 0,
                    "blocked_tasks": 0,
                    "implementation_hits": 0,
                    "review_hits": 0,
                },
            )
            for key in ("task_count", "run_count", "completed_runs", "failed_runs", "completed_tasks", "blocked_tasks", "implementation_hits", "review_hits"):
                dest[key] += int(p.get(key) or 0)

        health = board.get("health") or {"state": "unknown", "reasons": []}
        staleness = board.get("staleness") or {}
        board_failure = board.get("failure_breakdown") or {}
        failure_categories.update(board_failure.get("by_category") or {})
        for item in board_failure.get("top_reasons") or []:
            if isinstance(item, dict) and item.get("reason"):
                failure_reasons[str(item.get("reason"))] += int(item.get("count") or 0)
        board_created_window = _time_window(board.get("created_at"), now_ts)
        board_active_window = _time_window(board.get("last_worked_at"), now_ts)
        if board_created_window == "current":
            board_created_current += 1
        elif board_created_window == "previous":
            board_created_previous += 1
        if board_active_window == "current":
            board_active_current += 1
        elif board_active_window == "previous":
            board_active_previous += 1
        if int(staleness.get("stale_open_tasks_24h") or 0):
            stale_board_24h += 1
        if int(staleness.get("stale_open_tasks_72h") or 0):
            stale_board_72h += 1
        if int(staleness.get("stale_open_tasks_7d") or 0):
            stale_board_7d += 1

        board_inventory.append(
            {
                "slug": board.get("slug"),
                "name": board.get("name") or board.get("slug"),
                "description": board.get("description") or "",
                "default_workdir": board.get("default_workdir"),
                "created_at": board.get("created_at"),
                "last_worked_at": board.get("last_worked_at"),
                "task_count": board.get("task_count") or 0,
                "run_count": board.get("run_count") or 0,
                "open_tasks": board.get("open_tasks") or 0,
                "blocked_tasks": board.get("blocked_tasks") or 0,
                "done_tasks": board.get("done_tasks") or 0,
                "failed_runs": board.get("failed_runs") or 0,
                "completion_ratio": board.get("completion_ratio") or 0,
                "main_assignee_proxy": (board.get("profiles") or [{}])[0].get("profile") if board.get("profiles") else None,
                "task_statuses": board.get("task_statuses") or {},
                "run_statuses": board.get("run_statuses") or {},
                "run_outcomes": board.get("run_outcomes") or {},
                "evidence_coverage": board.get("evidence_coverage") or {},
                "activity_evidence_coverage": board.get("activity_evidence_coverage") or {},
                "proxy_evidence_coverage": board.get("proxy_evidence_coverage") or {},
                "proof_coverage": board.get("proof_coverage") or {},
                "strong_proof_coverage": board.get("strong_proof_coverage") or {},
                "health": health,
                "staleness": staleness,
                "links": {
                    "kanban": f"/kanban?board={board.get('slug')}",
                    "report": f"hpipe report {board.get('slug')}",
                    "insights": f"hpipe insights --json {board.get('slug')}",
                },
            }
        )

        recent_runs.extend(board.get("recent_runs") or [])
        all_runs.extend([
            {
                **run,
                "board_slug": board.get("slug"),
                "board_name": board.get("name") or board.get("slug"),
            }
            for run in (board.get("all_runs") or board.get("recent_runs") or [])
        ])

    profile_list = sorted(profiles.values(), key=lambda p: (p["run_count"], p["task_count"], p["completed_tasks"]), reverse=True)
    implementation_list = sorted(profile_list, key=lambda p: (p["implementation_hits"], p["run_count"], p["task_count"]), reverse=True)
    review_list = sorted(profile_list, key=lambda p: (p["review_hits"], p["run_count"], p["task_count"]), reverse=True)
    run_volume_list = sorted(profile_list, key=lambda p: (p["run_count"], p["completed_runs"], p["task_count"]), reverse=True)
    completion_list = sorted(profile_list, key=lambda p: (p["completed_tasks"], p["task_count"], p["run_count"]), reverse=True)
    coder_profiles = [p for p in implementation_list if p["implementation_hits"] > 0] or implementation_list
    reviewer_profiles = [p for p in review_list if p["review_hits"] > 0] or review_list

    all_concurrency = _max_concurrency(all_runs, now_ts)
    board_inventory.sort(key=lambda b: b.get("last_worked_at") or b.get("created_at") or 0, reverse=True)
    recent_runs.sort(key=lambda row: (row.get("ended_at") or row.get("started_at") or 0, row.get("started_at") or 0), reverse=True)
    recent_runs = recent_runs[:20]

    delta = {
        "window_seconds": _WINDOW_SECONDS,
        "completed_tasks": {
            "current": sum(int(board.get("delta", {}).get("completed_tasks", {}).get("current") or 0) for board in boards),
            "previous": sum(int(board.get("delta", {}).get("completed_tasks", {}).get("previous") or 0) for board in boards),
        },
        "failed_runs": {
            "current": sum(int(board.get("delta", {}).get("failed_runs", {}).get("current") or 0) for board in boards),
            "previous": sum(int(board.get("delta", {}).get("failed_runs", {}).get("previous") or 0) for board in boards),
        },
        "evidence_tasks": {
            "current": sum(int(board.get("delta", {}).get("evidence_tasks", {}).get("current") or 0) for board in boards),
            "previous": sum(int(board.get("delta", {}).get("evidence_tasks", {}).get("previous") or 0) for board in boards),
        },
        "max_concurrency": {
            "current": max((int(board.get("delta", {}).get("max_concurrency", {}).get("current") or 0) for board in boards), default=0),
            "previous": max((int(board.get("delta", {}).get("max_concurrency", {}).get("previous") or 0) for board in boards), default=0),
        },
        "new_boards": {"current": board_created_current, "previous": board_created_previous},
        "active_boards": {"current": board_active_current, "previous": board_active_previous},
    }
    for key in ("completed_tasks", "failed_runs", "evidence_tasks", "max_concurrency", "new_boards", "active_boards"):
        current = int(delta[key]["current"])
        previous = int(delta[key]["previous"])
        delta[key]["delta"] = current - previous

    current_all_runs = [r for r in all_runs if _time_window(r.get("started_at"), now_ts) == "current"]
    previous_all_runs = [r for r in all_runs if _time_window(r.get("started_at"), now_ts) == "previous"]
    delta["max_concurrency"] = {
        "current": _max_concurrency(current_all_runs, now_ts)["max"],
        "previous": _max_concurrency(previous_all_runs, now_ts)["max"],
    }
    delta["max_concurrency"]["delta"] = int(delta["max_concurrency"]["current"]) - int(delta["max_concurrency"]["previous"])

    coverage_payloads = _coverage_payloads(
        tasks_total=evidence_total,
        activity_proxy_count=activity_proxy_hit,
        proof_evidence_count=evidence_hit,
    )
    evidence_ratio = coverage_payloads["proof_coverage"]["coverage_ratio"]
    staleness = {
        "oldest_open_task_age_seconds": max((int(board.get("staleness", {}).get("oldest_open_task_age_seconds") or 0) for board in boards), default=0) or None,
        "stale_open_tasks_24h": sum(int(board.get("staleness", {}).get("stale_open_tasks_24h") or 0) for board in boards),
        "stale_open_tasks_72h": sum(int(board.get("staleness", {}).get("stale_open_tasks_72h") or 0) for board in boards),
        "stale_open_tasks_7d": sum(int(board.get("staleness", {}).get("stale_open_tasks_7d") or 0) for board in boards),
        "last_activity_age_seconds": max((int(board.get("staleness", {}).get("last_activity_age_seconds") or 0) for board in boards), default=0) or None,
        "stale_boards_24h": stale_board_24h,
        "stale_boards_72h": stale_board_72h,
        "stale_boards_7d": stale_board_7d,
    }

    health = _board_health(
        task_count=sum(int(board.get("task_count") or 0) for board in boards),
        blocked_tasks=blocked_tasks,
        failed_runs=failed_runs,
        evidence_ratio=evidence_ratio,
        stale_24h=staleness["stale_open_tasks_24h"],
        stale_72h=staleness["stale_open_tasks_72h"],
        stale_7d=staleness["stale_open_tasks_7d"],
        last_activity_age_seconds=staleness["last_activity_age_seconds"],
    )

    failure_breakdown = {
        "by_category": dict(failure_categories),
        "top_reasons": [
            {"reason": reason, "count": count}
            for reason, count in failure_reasons.most_common(8)
        ],
    }

    summary = {
        "board_count": len(boards),
        "open_tasks": open_tasks,
        "done_tasks": done_tasks,
        "blocked_tasks": blocked_tasks,
        "failed_runs": failed_runs,
        "reclaimed_runs": reclaimed_runs,
        "max_concurrent_task_runs": all_concurrency["max"],
        "max_concurrent_board": max_board,
        **coverage_payloads,
        "task_statuses": dict(task_statuses),
        "run_statuses": dict(run_statuses),
        "profiles": profile_list[:15],
        "leaderboards": {
            "profile_activity": profile_list[:10],
            "implementation_heavy": implementation_list[:10],
            "review_heavy": review_list[:10],
            "run_volume": run_volume_list[:10],
            "completion": completion_list[:10],
        },
        "coder_activity_proxy": {
            "top_profile": coder_profiles[0]["profile"] if coder_profiles else None,
            "profiles": coder_profiles[:8],
        },
        "review_activity_proxy": {
            "top_profile": reviewer_profiles[0]["profile"] if reviewer_profiles else None,
            "profiles": reviewer_profiles[:8],
        },
        "failure_breakdown": failure_breakdown,
        "staleness": staleness,
        "health": health,
        "delta": delta,
        "concurrency": {
            "max": all_concurrency["max"],
            "first_seen": all_concurrency["first_seen"],
            "interval_count": all_concurrency["interval_count"],
            "timeline": all_concurrency["timeline"],
            "board_peaks": [
                {
                    "slug": board.get("slug"),
                    "name": board.get("name") or board.get("slug"),
                    "peak": board.get("concurrency", {}).get("max") or 0,
                    "first_seen": board.get("concurrency", {}).get("first_seen"),
                }
                for board in sorted(boards, key=lambda b: b.get("concurrency", {}).get("max") or 0, reverse=True)[:10]
            ],
        },
        "board_inventory_count": len(board_inventory),
        "recent_run_count": len(recent_runs),
        "thresholds": _DEFAULT_THRESHOLDS,
        "threshold_policy": _THRESHOLD_POLICY,
    }

    return {
        "summary": summary,
        "board_inventory": board_inventory,
        "recent_boards": board_inventory[:8],
        "recent_runs": recent_runs,
        "failure_breakdown": failure_breakdown,
        "staleness": staleness,
        "delta": delta,
        "health": health,
        "concurrency_timeline": all_concurrency["timeline"],
        "thresholds": _DEFAULT_THRESHOLDS,
        "threshold_policy": _THRESHOLD_POLICY,
    }


def _board_operator_payload(board: dict[str, Any]) -> dict[str, Any]:
    slug = str(board.get("slug") or "")
    task_count = int(board.get("task_count") or 0)
    run_count = int(board.get("run_count") or 0)
    failed_runs = int(board.get("failed_runs") or 0)
    blocked_tasks = int(board.get("blocked_tasks") or 0)
    open_tasks = int(board.get("open_tasks") or 0)
    proof = board.get("proof_coverage") or board.get("strong_proof_coverage") or board.get("evidence_coverage") or {}
    proof_total = int(proof.get("tasks_total") or (board.get("evidence_coverage") or {}).get("tasks_total") or 0)
    proof_hits = int(proof.get("tasks_with_proof_signals") or (board.get("evidence_coverage") or {}).get("tasks_with_proof_signals") or 0)

    if task_count == 0 and run_count == 0:
        verdict = "empty board"
        recommended = f"Inspect the board shell, then run hpipe board-lint {slug} to decide whether it is a ghost, staged shell, or archival candidate."
        severity = "attention"
    elif failed_runs:
        verdict = "failed runs need triage"
        recommended = f"Run hpipe fail-log {slug}, then hpipe board-lint {slug}."
        severity = "blocked"
    elif blocked_tasks:
        verdict = "blocked work"
        recommended = f"Open /kanban?board={slug} and inspect blocked cards before resuming."
        severity = "blocked"
    elif open_tasks:
        verdict = "active work"
        recommended = f"Run hpipe report {slug} or hpipe resume {slug} after confirming authority."
        severity = "healthy"
    elif task_count and proof_total and proof_hits < proof_total:
        verdict = "done/mixed but proof incomplete"
        recommended = f"Run hpipe board-lint {slug} and inspect missing proof before closeout."
        severity = "attention"
    else:
        verdict = "healthy or complete"
        recommended = f"Run hpipe report {slug} for closeout context."
        severity = "healthy"

    commands = [
        {"label": "Report", "command": f"hpipe report {slug}", "read_only": True},
        {"label": "Insights JSON", "command": f"hpipe insights --json {slug}", "read_only": True},
        {"label": "Board lint", "command": f"hpipe board-lint {slug}", "read_only": True},
        {"label": "Fail log", "command": f"hpipe fail-log {slug}", "read_only": True},
        {"label": "Kanban show/list", "command": f"hermes kanban --board {slug} list --json", "read_only": True},
        {"label": "Resume", "command": f"hpipe resume {slug}", "read_only": False},
        {"label": "Amend", "command": f"hpipe amend {slug} '<scoped change>'", "read_only": False},
    ]
    return {
        "verdict": verdict,
        "severity": severity,
        "recommended_action": recommended,
        "commands": commands,
        "evidence_display": {
            "state": "not_applicable" if proof_total == 0 else ("complete" if proof_hits >= proof_total else "incomplete"),
            "label": "N/A" if proof_total == 0 else f"{proof_hits}/{proof_total}",
            "detail": "No task rows in scope; evidence percentage is not meaningful." if proof_total == 0 else f"{proof_hits} of {proof_total} tasks have strong proof signals.",
        },
    }


def _reconcile_boards_and_run_index(boards: list[dict[str, Any]], run_index: dict[str, Any]) -> dict[str, Any]:
    live_board_slugs = {str(board.get("slug") or "") for board in boards if board.get("slug")}
    archived_physical: set[str] = set()
    archived_base: set[str] = set()
    for slug, _child, archived in _iter_hpipe_board_dirs(include_archived=True):
        if archived:
            archived_physical.add(slug)
            archived_base.add(_archived_slug_base(slug))
    entries = run_index.get("entries_all") or run_index.get("entries") or []
    index_slugs = {
        str(entry.get("board_or_run") or entry.get("board") or entry.get("slug") or "")
        for entry in entries
        if isinstance(entry, dict) and (entry.get("board_or_run") or entry.get("board") or entry.get("slug"))
    }
    matched = sorted(live_board_slugs & index_slugs)
    archived_matches = sorted(index_slugs & (archived_physical | archived_base))
    live_board_only = sorted(live_board_slugs - index_slugs)
    archived_board_only = sorted(archived_base - index_slugs - live_board_slugs)
    index_only = sorted(index_slugs - live_board_slugs - set(archived_matches))
    return {
        "matched_count": len(matched),
        "live_board_only_count": len(live_board_only),
        "board_only_count": len(live_board_only),
        "archived_board_only_count": len(archived_board_only),
        "archived_board_match_count": len(archived_matches),
        "index_only_count": len(index_only),
        "matched": matched[:50],
        "live_board_only": live_board_only[:50],
        "board_only": live_board_only[:50],
        "archived_board_only": archived_board_only[:50],
        "archived_board_matches": archived_matches[:50],
        "index_only": index_only[:50],
        "counts": {
            "matched": len(matched),
            "live_board_only": len(live_board_only),
            "archived_board_only": len(archived_board_only),
            "archived_board_matches": len(archived_matches),
            "index_only": len(index_only),
            "run_index_entries": int(run_index.get("all_entry_count") or run_index.get("entry_count") or len(index_slugs)),
            "live_hpipe_boards": len(live_board_slugs),
            "archived_hpipe_boards": len(archived_physical),
            "archived_hpipe_board_bases": len(archived_base),
        },
        "explanation": "Full-history reconciliation compares all RUN_INDEX.md board/run slugs with current live hpipe-looking Kanban board directories and archived board directories in both directions. Index-only means historical metadata has no visible live/archived local board directory; live-board-only means live telemetry exists without a run-index metadata row; archived-board-only means archived evidence exists without a run-index metadata row.",
    }


def _diagnostics_payload(boards: list[dict[str, Any]], run_index: dict[str, Any], reconciliation: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    total_tasks = int((summary.get("proof_coverage") or summary.get("evidence_coverage") or {}).get("tasks_total") or 0)
    has_zero_state = bool(boards) and total_tasks == 0 and int(summary.get("failed_runs") or 0) == 0
    reasons = []
    if has_zero_state:
        reasons.extend([
            "Hpipe-looking board directories exist, but no task rows/runs are visible in the telemetry scope.",
            "This can be a healthy idle state, an empty staged board, a ghost board, archived/missing task data, or run-index metadata that has drifted from Kanban storage.",
        ])
    if reconciliation.get("board_only_count"):
        reasons.append(f"{reconciliation['board_only_count']} live board(s) have no matching run-index entry.")
    if reconciliation.get("archived_board_only_count"):
        reasons.append(f"{reconciliation['archived_board_only_count']} archived board(s) have no matching run-index entry.")
    if reconciliation.get("index_only_count"):
        reasons.append(f"{reconciliation['index_only_count']} run-index entr(y/ies) have no matching visible board.")
    if not reasons:
        reasons.append("Telemetry has populated task/run evidence or no reconciliation anomalies were found.")

    commands = [
        "hpipe status",
        "hpipe run-index --json",
        "hpipe insights --json <board>",
        "hpipe report <board>",
        "hpipe board-lint <board>",
        "hpipe fail-log <board>",
        "hermes kanban --board <board> list --json",
    ]
    return {
        "zero_state": has_zero_state,
        "reasons": reasons,
        "why_zeros": "Zeros with non-zero board count mean the dashboard found hpipe-looking board containers but no task/run rows to aggregate; evidence is N/A rather than 0% failure when the denominator is zero.",
        "suggested_checks": commands,
    }


def _next_actions_payload(boards: list[dict[str, Any]], reconciliation: dict[str, Any], summary: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if int((summary.get("proof_coverage") or summary.get("evidence_coverage") or {}).get("tasks_total") or 0) == 0 and boards:
        actions.append({"priority": "high", "label": "Diagnose zero-state boards", "command": "hpipe board-lint <board>", "read_only": True})
    if reconciliation.get("board_only_count") or reconciliation.get("archived_board_only_count") or reconciliation.get("index_only_count"):
        actions.append({"priority": "high", "label": "Reconcile boards and run index", "command": "hpipe run-index --json && hpipe status", "read_only": True})
    if int(summary.get("failed_runs") or 0):
        actions.append({"priority": "high", "label": "Inspect failed runs", "command": "hpipe fail-log <board>", "read_only": True})
    if not actions:
        actions.append({"priority": "medium", "label": "Open latest meaningful board report", "command": "hpipe report <board>", "read_only": True})
    actions.append({"priority": "medium", "label": "Create a new staged workflow", "command": "hpipe --print stage '<task>'", "read_only": True})
    return actions


def _workflow_builder_payload() -> dict[str, Any]:
    return {
        "read_only_default": True,
        "safety_banner": "Preview/spec/status/report/board-lint/fail-log are read-only. Stage writes board state only. Code/run/resume/amend may mutate local files and require explicit authority; this dashboard does not dispatch or mutate.",
        "modes": [
            {"mode": "spec", "authority": "read-only", "template": "hpipe --print spec '<task>'"},
            {"mode": "stage", "authority": "board-only after explicit launch", "template": "hpipe --print stage --name '<name>' '<task>'"},
            {"mode": "code-preview", "authority": "read-only preview", "template": "hpipe --print code --require-path <path> --tests '<command>' '<task>'"},
            {"mode": "verify-only", "authority": "read-only/diagnostic", "template": "hpipe --print code --verify-only --require-path <path> '<task>'"},
            {"mode": "legal-stage", "authority": "read-first/draft-first", "template": "hpipe --print legal-stage '<matter/task>'"},
        ],
        "knobs": ["--risk", "--lane", "--bar", "--require-path", "--tests", "--worktree", "--diff-review", "--verify-only", "--max-files", "--deps", "--evidence-dir"],
    }


@router.get("/snapshot")
def snapshot(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    boards = _load_hpipe_boards(limit=int(limit or 20))
    payload = _aggregate(boards)
    run_index = _load_run_index()
    cli_insights = _run_hpipe_json(["insights", "--json", "latest"], timeout=10)

    for board in payload.get("board_inventory", []):
        board["operator"] = _board_operator_payload(board)
        board["commands"] = board["operator"]["commands"]
        board["verdict"] = board["operator"]["verdict"]
        board["recommended_action"] = board["operator"]["recommended_action"]
        board["evidence_display"] = board["operator"]["evidence_display"]

    reconciliation = _reconcile_boards_and_run_index(payload.get("board_inventory", []), run_index)
    historical_stats = _historical_stats(run_index, payload.get("board_inventory", []), reconciliation)
    diagnostics = _diagnostics_payload(payload.get("board_inventory", []), run_index, reconciliation, payload.get("summary", {}))
    next_actions = _next_actions_payload(payload.get("board_inventory", []), reconciliation, payload.get("summary", {}))
    generated_at = int(time.time())
    return _json_safe(
        {
            "ok": True,
            "generated_at": generated_at,
            **payload,
            "recent_boards": payload.get("board_inventory", [])[:8],
            "run_index": run_index,
            "historical_stats": historical_stats,
            "run_reconciliation": reconciliation,
            "full_run_reconciliation": reconciliation,
            "diagnostics": diagnostics,
            "next_actions": next_actions,
            "workflow_builder": _workflow_builder_payload(),
            "latest_cli_insights": cli_insights if isinstance(cli_insights, dict) else None,
            "freshness": {
                "last_refreshed_at": generated_at,
                "query_limit": int(limit or 20),
                "data_age_note": "Local snapshot from Hermes Kanban board directories plus hpipe run-index; refresh re-reads local state only.",
            },
            "data_sources": [
                {"label": "Current live board telemetry", "path": str(_board_root()), "role": "current live board/task/run telemetry"},
                {"label": "Archived board telemetry", "path": str(_board_root() / "_archived"), "role": "archived local board directories when present"},
                {"label": "Run-index historical metadata", "path": run_index.get("source"), "role": "full historical hpipe metadata from RUN_INDEX.md", "available": run_index.get("available")},
                {"label": "hpipe CLI", "path": shutil.which("hpipe"), "role": "read-only drill-down commands"},
            ],
            "links": {
                "kanban": "/kanban",
                "hpipe_report_hint": "hpipe report <board>",
                "hpipe_insights_hint": "hpipe insights --json <board>",
                "hpipe_board_lint_hint": "hpipe board-lint <board>",
                "hpipe_fail_log_hint": "hpipe fail-log <board>",
                "run_index_hint": "hpipe run-index --json",
                "kanban_show_hint": "hermes kanban --board <board> show <task_id> --json",
            },
        }
    )


@router.get("/health")
def health() -> dict[str, Any]:
    boards = _load_hpipe_boards(limit=100)
    payload = _aggregate(boards)
    return {"ok": True, **payload["health"], "summary": payload["summary"]}
