# Hpipe Dashboard V2 Cockpit Layout Mini-Spec

Source basis: `references/hpipe-dashboard-v2-spec.md`, `plugins/hpipe/dashboard/plugin_api.py`, `plugins/hpipe/dashboard/dist/index.js`, `plugins/hpipe/dashboard/dist/style.css`, `plugins/hpipe/dashboard/manifest.json`, and `tests/plugins/test_hpipe_dashboard_plugin.py`.

BLUF: V2 turns the Hpipe tab into a local-only, read-only operator cockpit. The frontend must render the backend `/api/plugins/hpipe/snapshot` payload in the order below, keep proxy-derived metrics explicit, and remain readable without default horizontal scrolling at normal browser widths.

## Section order and priority

1. Hero summary + refresh control
   - Purpose: identify the Hpipe control plane and keep refresh visible.
   - Backend fields: `generated_at`, `links`, `summary.board_count` where useful.

2. Health strip
   - Purpose: show overall board health before detailed KPIs.
   - Backend fields: `health.state`, `health.reasons`, `summary.health` fallback.
   - States: `healthy`, `attention`, `degraded`, `blocked`; every non-healthy state must display reasons.

3. KPI row
   - Purpose: first-screen operating snapshot.
   - Backend fields: `summary.board_count`, `summary.open_tasks`, `summary.done_tasks`, `summary.blocked_tasks`, `summary.failed_runs`, `summary.reclaimed_runs`, `summary.max_concurrent_task_runs`, `summary.max_concurrent_board`, `summary.evidence_coverage.coverage_ratio`, `summary.evidence_coverage.tasks_with_evidence_signals`, `summary.evidence_coverage.tasks_total`, `summary.coder_activity_proxy.top_profile`, `summary.review_activity_proxy.top_profile`.

4. Concurrency timeline
   - Purpose: show swarm shape and peak activity.
   - Backend fields: `summary.concurrency.timeline[]`, `concurrency_timeline[]` fallback, `summary.concurrency.board_peaks[]`, `summary.concurrency.max`, `summary.concurrency.first_seen`, `summary.concurrency.interval_count`, `delta.max_concurrency`.

5. Leaderboards and attribution
   - Purpose: expose who is doing implementation/review work.
   - Backend fields: `summary.leaderboards.profile_activity[]`, `implementation_heavy[]`, `review_heavy[]`, `run_volume[]`, `completion[]`, `summary.profiles[]`, `summary.coder_activity_proxy.profiles[]`, `summary.review_activity_proxy.profiles[]`.

6. Board health and staleness
   - Purpose: make stalled boards/tasks obvious.
   - Backend fields: `staleness.oldest_open_task_age_seconds`, `staleness.stale_open_tasks_24h`, `staleness.stale_open_tasks_72h`, `staleness.stale_open_tasks_7d`, `staleness.stale_boards_24h`, `staleness.stale_boards_72h`, `staleness.stale_boards_7d`, `staleness.last_activity_age_seconds`, plus per-board `board_inventory[].staleness`, `board_inventory[].health`, `board_inventory[].task_statuses`, `board_inventory[].run_statuses`.

7. Evidence / proof coverage
   - Purpose: separate proof-backed work from merely active work.
   - Backend fields: `summary.evidence_coverage`, `board_inventory[].evidence_coverage`, `recent_runs[].proof_state`, `recent_runs[].summary`, `recent_runs[].metadata`, `recent_runs[].links`.
   - Proof states: `proof`, `proxy`, `missing`.

8. Failure analysis
   - Purpose: split failures into actionable categories.
   - Backend fields: `failure_breakdown.by_category`, `failure_breakdown.top_reasons[]`, `summary.failure_breakdown`, `summary.run_statuses`, `summary.run_outcomes`, `delta.failed_runs`.
   - Categories expected from backend: `timeout`, `crash`, `auth_permission`, `dependency_install`, `protocol_violation`, `board_claim_conflict`, `tool_environment_failure`, `verification_failure`, `other`.

9. Recent boards and recent runs
   - Purpose: compact drill-down inventory without leaving the tab.
   - Backend fields: `board_inventory[]`, `recent_boards[]`, `recent_runs[]`, including `slug`, `name`, `description`, `default_workdir`, `created_at`, `last_worked_at`, `task_count`, `run_count`, `open_tasks`, `blocked_tasks`, `done_tasks`, `failed_runs`, `completion_ratio`, `main_assignee_proxy`, `health`, `staleness`, `links`, `task_id`, `task_title`, `profile`, `status`, `outcome`, `started_at`, `ended_at`, `error`, `failure_category`, `proof_state`.

10. Run lineage / drill-down helpers
    - Purpose: provide immediate next commands for deeper inspection.
    - Backend fields: top-level `links.kanban`, `links.hpipe_report_hint`, `links.hpipe_insights_hint`, `links.run_index_hint`, `links.kanban_show_hint`, and per-board/per-run `links`.

11. Alert thresholds and freshness deltas
    - Purpose: show what changed and which thresholds are currently risky.
    - Backend fields: `summary.thresholds`, top-level `thresholds` fallback, `delta.completed_tasks`, `delta.failed_runs`, `delta.evidence_tasks`, `delta.max_concurrency`, `delta.new_boards`, `delta.active_boards`.

## Proxy-label rules

- Any metric derived heuristically from task text, assignee counts, run counts, or evidence keywords must include `proxy` in the visible label or detail text.
- Required visible proxy labels: `Top coder` detail `implementation proxy`, `Top reviewer` detail `review proxy`, board owner `Owner proxy`, `main_assignee_proxy`, `coder_activity_proxy`, and `review_activity_proxy`.
- `proof_state=proxy` must render as proxy evidence, not proof; `proof_state=missing` must not be styled as successful proof.
- Do not infer unavailable diff/commit attribution. If real attribution is not in the payload, keep proxy wording.

## Readability and responsive acceptance rules

- Default dashboard state must not cause horizontal page scrolling at normal browser width; chips, board names, task titles, errors and commands must wrap (`overflow-wrap: anywhere` or equivalent).
- Desktop may use two- or three-column grids; tablet collapses to two columns; mobile collapses to one column.
- Minimum readable text remains at or above the existing dashboard floor: body/chip text no smaller than `0.75rem` unless decorative.
- Use compact cards and wrapped chip lines; avoid fixed-width tables for the default cockpit.
- Health, failure and stale indicators must be visually distinguishable without relying on colour alone: include textual state/count labels.

## Acceptance criteria for implementation cards

- The shipped bundle registers `REG.register("hpipe", HpipePage)` and fetches `/api/plugins/hpipe/snapshot` only through the local plugin API.
- All sections above render from the documented payload fields with safe fallbacks for empty arrays/missing optional fields.
- V1 behaviour is preserved: local-only, read-only, refreshable, no external calls, no mutation endpoints.
- Proxy metrics are explicitly labelled in the rendered text.
- The UI remains readable without default horizontal scrolling.
- Minimum validation includes the hpipe plugin test, manifest/bundle readback, and syntax/readability inspection; browser/DOM evidence is preferred when available.
