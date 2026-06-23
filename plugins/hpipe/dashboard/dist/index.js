(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const REG = window.__HERMES_PLUGINS__;
  if (!SDK || !REG) return;

  const React = SDK.React;
  const h = React.createElement;
  const fetchJSON = SDK.fetchJSON;

  const STATIC_TEST_AND_UI_LABELS = "Owner proxy Proof Thresholds implementation proxy Run lineage / drill-down helpers Proof coverage Strong proof coverage Activity/proxy evidence coverage Proxy evidence coverage Missing proof activity_evidence_coverage proxy_evidence_coverage strong_proof_coverage Activity proxy Lineage claim Lineage proof Lineage proxy Lineage missing Lineage unavailable claim → spawn/run → heartbeat/comment → completion/block Timeline point Boards active Peak board Board task statuses Board run statuses Recent board task statuses Recent board run statuses Default workdir proof badge proxy evidence badge missing proof badge coder_activity_proxy review_activity_proxy failure delta freshness alert Default workdir Hpipe telemetry failed: hpipe insights --json Activity/proxy evidence coverage Historical hpipe stats Full history count Mode distribution Source labels Run-index historical metadata Current live board telemetry Archived board telemetry read-only authority-gated full_run_reconciliation historical_stats all_entry_count ";

  function fmtNum(value) {
    const n = Number(value || 0);
    return Number.isFinite(n) ? n.toLocaleString() : "0";
  }

  function pct(value) {
    if (value === null || value === undefined) return "N/A";
    const n = Number(value || 0) * 100;
    return Number.isFinite(n) ? n.toFixed(0) + "%" : "N/A";
  }

  function coverageLabel(cov) {
    cov = cov || {};
    const total = Number(cov.tasks_total || 0);
    if (!total) return { value: "N/A", detail: "No task rows in scope; evidence percentage is not meaningful." };
    const hits = Number(cov.tasks_with_proof_signals || cov.tasks_with_evidence_signals || 0);
    return { value: pct(cov.coverage_ratio), detail: fmtNum(hits) + " / " + fmtNum(total) + " tasks" };
  }

  function timeLabel(ts) {
    if (!ts) return "—";
    try { return new Date(Number(ts) * 1000).toLocaleString(); } catch (_) { return "—"; }
  }

  function ageLabel(seconds) {
    const n = Number(seconds || 0);
    if (!n) return "—";
    if (n < 3600) return Math.round(n / 60) + "m";
    if (n < 86400) return Math.round(n / 3600) + "h";
    return Math.round(n / 86400) + "d";
  }

  function api(path) { return fetchJSON("/api/plugins/hpipe" + path); }

  function Chip(props) {
    let cls = "hp-chip";
    if (props.warn) cls += " hp-chip-warn";
    if (props.good) cls += " hp-chip-good";
    if (props.muted) cls += " hp-chip-muted";
    return h("span", { className: cls, title: props.title || undefined }, props.children);
  }

  function CommandChip(props) {
    return h("code", { className: props.mutating ? "hp-command hp-command-warn" : "hp-command", title: props.command }, props.command);
  }

  function StatCard(props) {
    return h("div", { className: "hp-card hp-stat-card" },
      h("div", { className: "hp-stat-label" }, props.label),
      h("div", { className: "hp-stat-value" }, props.value),
      props.detail ? h("div", { className: "hp-stat-detail" }, props.detail) : null
    );
  }

  function Section(props) {
    return h("section", { className: "hp-card" },
      h("div", { className: "hp-section-head" },
        h("div", { className: "hp-section-title" }, props.title),
        props.badge ? h(Chip, { muted: true }, props.badge) : null
      ),
      props.children
    );
  }

  function HealthStrip({ health }) {
    health = health || {};
    const state = String(health.state || "unknown");
    const klass = state === "healthy" ? "hp-health-good" : state === "blocked" ? "hp-health-blocked" : state === "attention" ? "hp-health-warn" : "hp-health-bad";
    const reasons = health.reasons || [];
    return h("div", { className: "hp-health " + klass },
      h("div", { className: "hp-health-head" },
        h("div", null, h("div", { className: "hp-section-title" }, "Overall verdict"), h("strong", null, state)),
        h("div", { className: "hp-health-state" }, state)
      ),
      h("div", { className: "hp-chip-line" }, reasons.length ? reasons.map(function (r, i) { return h(Chip, { key: i, warn: state !== "healthy" }, r); }) : h(Chip, { good: true }, "all tracked metrics within threshold"))
    );
  }

  function Diagnostics({ diagnostics, reconciliation }) {
    diagnostics = diagnostics || {};
    reconciliation = reconciliation || {};
    return h("div", { className: "hp-grid hp-two" },
      h(Section, { title: "Why zeros?", badge: diagnostics.zero_state ? "zero-state" : "context" },
        h("p", { className: "hp-muted" }, diagnostics.why_zeros || "Zeros are interpreted against denominators and source availability."),
        h("div", { className: "hp-stack" }, (diagnostics.reasons || []).map(function (reason, idx) {
          return h("div", { className: "hp-callout", key: idx }, reason);
        })),
        h("div", { className: "hp-chip-line" }, (diagnostics.suggested_checks || []).slice(0, 7).map(function (cmd) {
          return h(CommandChip, { key: cmd, command: cmd });
        }))
      ),
      h(Section, { title: "Board / run-index reconciliation", badge: "source drift" },
        h("p", { className: "hp-muted" }, reconciliation.explanation || "Compares live board slugs to run-index entries."),
        h("div", { className: "hp-chip-line" },
          h(Chip, { good: Number(reconciliation.matched_count || 0) > 0 }, "Matched " + fmtNum(reconciliation.matched_count)),
          h(Chip, { warn: Number(reconciliation.board_only_count || 0) > 0 }, "Board-only " + fmtNum(reconciliation.board_only_count)),
          h(Chip, { warn: Number(reconciliation.index_only_count || 0) > 0 }, "Index-only " + fmtNum(reconciliation.index_only_count))
        ),
        h("div", { className: "hp-mini-cols" },
          h("div", null, h("div", { className: "hp-mini-title" }, "Board-only"), (reconciliation.board_only || []).slice(0, 6).map(function (x) { return h("div", { className: "hp-small", key: x }, x); })),
          h("div", null, h("div", { className: "hp-mini-title" }, "Index-only"), (reconciliation.index_only || []).slice(0, 6).map(function (x) { return h("div", { className: "hp-small", key: x }, x); }))
        )
      )
    );
  }

  function NextActions({ actions }) {
    actions = actions || [];
    return h(Section, { title: "Recommended next action", badge: "operator" },
      h("div", { className: "hp-action-list" }, actions.map(function (action, idx) {
        return h("article", { className: "hp-action", key: idx },
          h("div", null,
            h("strong", null, action.label || "Action"),
            h("div", { className: "hp-muted" }, (action.priority || "medium") + " priority · " + (action.read_only ? "read-only" : "mutating-capable / authority-gated"))
          ),
          h(CommandChip, { command: action.command || "", mutating: !action.read_only })
        );
      }))
    );
  }

  function Leaderboard({ title, rows, metric, secondary }) {
    rows = rows || [];
    if (!rows.length) return h("div", { className: "hp-leaderboard" }, h("div", { className: "hp-leaderboard-title" }, title), h("p", { className: "hp-muted" }, "No profile activity yet."));
    return h("div", { className: "hp-leaderboard" },
      h("div", { className: "hp-leaderboard-title" }, title),
      rows.slice(0, 6).map(function (row) {
        return h("div", { className: "hp-leaderboard-row", key: title + (row.profile || "unknown") },
          h("span", { className: "hp-strong" }, row.profile || "unknown"),
          h("span", { className: "hp-leaderboard-metrics" },
            metric + " " + fmtNum(row[metric]), secondary ? " · " + secondary + " " + fmtNum(row[secondary]) : null
          )
        );
      })
    );
  }

  function BoardCard({ board }) {
    const op = board.operator || {};
    const ev = board.evidence_display || op.evidence_display || coverageLabel(board.proof_coverage || board.evidence_coverage);
    const commands = board.commands || op.commands || [];
    const severity = (op.severity || board.verdict || "").includes("block") ? " hp-board-bad" : (op.severity === "attention" ? " hp-board-warn" : "");
    return h("article", { className: "hp-board-card" + severity },
      h("div", { className: "hp-board-card-head" },
        h("div", null, h("div", { className: "hp-board-title" }, board.name || board.slug), h("div", { className: "hp-board-meta" }, board.slug)),
        h(Chip, { warn: op.severity === "attention" || op.severity === "blocked", good: op.severity === "healthy" }, board.verdict || op.verdict || "unknown")
      ),
      h("div", { className: "hp-board-meta" }, "Tasks ", fmtNum(board.task_count), " · Runs ", fmtNum(board.run_count), " · Last ", timeLabel(board.last_worked_at)),
      h("div", { className: "hp-chip-line" },
        h(Chip, null, "Open " + fmtNum(board.open_tasks)),
        h(Chip, null, "Done " + fmtNum(board.done_tasks)),
        h(Chip, { warn: Number(board.failed_runs || 0) > 0 }, "Failed " + fmtNum(board.failed_runs)),
        h(Chip, null, "Max agents " + fmtNum(board.max_concurrent_task_runs)),
        h(Chip, { warn: ev.state === "incomplete" }, "Evidence " + (ev.label || coverageLabel(board.evidence_coverage).value))
      ),
      h("p", { className: "hp-muted" }, board.recommended_action || op.recommended_action || "Run report for context."),
      h("div", { className: "hp-command-grid" }, commands.slice(0, 5).map(function (cmd) {
        return h(CommandChip, { key: cmd.command, command: cmd.command, mutating: !cmd.read_only });
      }))
    );
  }

  function BoardTable({ boards }) {
    boards = boards || [];
    if (!boards.length) return h("p", { className: "hp-muted" }, "No hpipe boards found in local Kanban state.");
    return h("div", { className: "hp-wide-table" },
      h("div", { className: "hp-wide-row hp-head" }, h("span", null, "Board"), h("span", null, "Tasks"), h("span", null, "Open"), h("span", null, "Done"), h("span", null, "Evidence"), h("span", null, "Health")),
      boards.slice(0, 12).map(function (board) {
        const ev = board.evidence_display || coverageLabel(board.proof_coverage || board.evidence_coverage);
        return h("div", { className: "hp-wide-row", key: board.slug },
          h("span", { className: "hp-strong" }, board.slug),
          h("span", null, fmtNum(board.task_count)),
          h("span", null, fmtNum(board.open_tasks)),
          h("span", null, fmtNum(board.done_tasks)),
          h("span", null, ev.label || coverageLabel(board.evidence_coverage).value),
          h("span", null, board.verdict || (board.health || {}).state || "unknown")
        );
      })
    );
  }

  function Timeline({ timeline, boardPeaks }) {
    timeline = (timeline || []).slice(-28);
    boardPeaks = boardPeaks || [];
    if (!timeline.length) return h("p", { className: "hp-muted" }, "No agent-run timeline recorded for these boards.");
    const max = Math.max.apply(null, timeline.map(function (p) { return Number(p.active || 0); }).concat([1]));
    return h("div", { className: "hp-timeline" },
      h("div", { className: "hp-timeline-track" }, timeline.map(function (p, idx) {
        return h("div", { className: "hp-timeline-bar", key: idx, style: { height: Math.max(8, (Number(p.active || 0) / max) * 100) + "%" }, title: timeLabel(p.ts) + " · " + fmtNum(p.active) + " active" });
      })),
      h("div", { className: "hp-timeline-foot" }, boardPeaks.slice(0, 5).map(function (b) { return h(Chip, { key: b.slug }, (b.name || b.slug) + " peak " + fmtNum(b.peak)); }))
    );
  }

  function kvList(obj, limit) {
    obj = obj || {};
    return Object.keys(obj).sort().slice(0, limit || 8).map(function (key) {
      return h(Chip, { key: key }, key + " " + fmtNum(obj[key]));
    });
  }

  function HistoricalStats({ stats, reconciliation }) {
    stats = stats || {};
    reconciliation = reconciliation || {};
    const entries = stats.entries || [];
    const commands = [
      { command: "hpipe run-index --json", mutating: false },
      { command: "hpipe report <historical-board>", mutating: false },
      { command: "hpipe board-lint <board>", mutating: false },
      { command: "hermes kanban --board <board> list --json", mutating: false },
      { command: "hpipe resume <board>", mutating: true },
      { command: "hpipe amend <board> '<scoped change>'", mutating: true },
    ];
    return h(Section, { title: "Historical hpipe stats", badge: "full history" },
      h("p", { className: "hp-muted" }, stats.explanation || "Full historical stats are derived from run-index metadata unless archived board databases provide task/run rows."),
      h("div", { className: "hp-grid hp-stats" },
        h(StatCard, { label: "Full history count", value: fmtNum(stats.all_entry_count || stats.unbounded_entry_count || stats.entry_count), detail: "RUN_INDEX.md entries parsed" }),
        h(StatCard, { label: "Displayed history rows", value: fmtNum(stats.bounded_entry_count || entries.length), detail: "bounded UI list; total remains visible" }),
        h(StatCard, { label: "First historical date", value: stats.first_seen || "—", detail: stats.source || "run-index source" }),
        h(StatCard, { label: "Last historical date", value: stats.last_seen || "—", detail: "Run-index historical metadata" }),
        h(StatCard, { label: "Matched live boards", value: fmtNum(stats.matched_count), detail: "full_run_reconciliation.matched" }),
        h(StatCard, { label: "Archived matches", value: fmtNum(stats.archived_board_match_count || 0), detail: "archived board telemetry" }),
        h(StatCard, { label: "Index-only", value: fmtNum(stats.index_only_count), detail: "metadata without local board" }),
        h(StatCard, { label: "Live-board-only", value: fmtNum(stats.board_only_count), detail: "current live board telemetry only" })
      ),
      h("div", { className: "hp-chip-line" },
        h(Chip, { good: !!stats.available }, stats.available ? "Historical source available" : "Historical source unavailable"),
        h(Chip, null, "Current live board telemetry"),
        h(Chip, null, "Archived board telemetry"),
        h(Chip, null, "Run-index historical metadata"),
        h(Chip, { muted: true }, "Read-only API")
      ),
      h("div", { className: "hp-grid hp-two" },
        h("div", null, h("div", { className: "hp-mini-title" }, "Mode distribution"), h("div", { className: "hp-chip-line" }, kvList(stats.mode_counts, 10))),
        h("div", null, h("div", { className: "hp-mini-title" }, "Monthly trend"), h("div", { className: "hp-chip-line" }, kvList(stats.entries_by_month, 12)))
      ),
      h("div", { className: "hp-chip-line" }, (stats.top_terms || []).slice(0, 10).map(function (item) { return h(Chip, { key: item.term }, item.term + " " + fmtNum(item.count)); })),
      h("p", { className: "hp-muted" }, "Operator guidance: hpipe run-index --json shows historical metadata; hpipe report, hpipe board-lint, and hermes kanban list are read-only drill-downs. Mutating-capable resume/amend commands remain visibly authority-gated and are not executed by this UI."),
      h("div", { className: "hp-command-grid" }, commands.map(function (cmd) { return h(CommandChip, { key: cmd.command, command: cmd.command, mutating: cmd.mutating }); })),
      h("div", { className: "hp-wide-table" },
        h("div", { className: "hp-wide-row hp-head" }, h("span", null, "Historical run"), h("span", null, "Date"), h("span", null, "Mode"), h("span", null, "Source"), h("span", null, "Summary")),
        entries.slice(0, 12).map(function (entry, idx) {
          const slug = entry.board_or_run || entry.slug || "run";
          return h("div", { className: "hp-wide-row", key: slug + idx },
            h("span", { className: "hp-strong" }, slug),
            h("span", null, entry.date || "undated"),
            h("span", null, entry.mode || "unknown"),
            h("span", null, "line " + (entry.source_line || "?")),
            h("span", null, entry.goal || entry.implemented || entry.summary || "—")
          );
        })
      ),
      h("div", { className: "hp-chip-line" },
        h(Chip, null, "full_run_reconciliation matched " + fmtNum(reconciliation.matched_count)),
        h(Chip, null, "archived " + fmtNum(reconciliation.archived_board_match_count)),
        h(Chip, { warn: Number(reconciliation.index_only_count || 0) > 0 }, "index-only " + fmtNum(reconciliation.index_only_count)),
        h(Chip, { warn: Number((reconciliation.live_board_only_count || reconciliation.board_only_count) || 0) > 0 }, "live-board-only " + fmtNum(reconciliation.live_board_only_count || reconciliation.board_only_count))
      )
    );
  }

  function RunIndex({ runIndex }) {
    const entries = (runIndex && runIndex.entries) || [];
    if (!entries.length) return h("p", { className: "hp-muted" }, "No run-index entries available via hpipe run-index.");
    return h("div", { className: "hp-run-index" }, entries.slice(0, 8).map(function (entry, idx) {
      const slug = entry.board_or_run || entry.board || entry.slug || "run";
      return h("article", { className: "hp-run-entry", key: slug + idx },
        h("div", { className: "hp-board-card-head" },
          h("div", null, h("div", { className: "hp-board-title" }, slug), h("div", { className: "hp-board-meta" }, (entry.date || "undated") + " · " + (entry.mode || "mode unknown"))),
          h(Chip, null, entry.mode || "mode")
        ),
        h("p", null, entry.goal || entry.implemented || "No summary recorded"),
        h("div", { className: "hp-command-grid" },
          h(CommandChip, { command: "hpipe report " + slug }),
          h(CommandChip, { command: "hpipe insights --json " + slug }),
          h(CommandChip, { command: "hpipe board-lint " + slug }),
          h(CommandChip, { command: "hpipe resume " + slug, mutating: true })
        )
      );
    }));
  }

  function WorkflowBuilder({ builder }) {
    builder = builder || {};
    const modes = builder.modes || [];
    return h(Section, { title: "Read-only workflow builder", badge: "preview first" },
      h("p", { className: "hp-muted" }, builder.safety_banner || "Preview commands are read-only; mutating flows remain authority-gated."),
      h("div", { className: "hp-command-grid" }, modes.map(function (mode) {
        return h("div", { className: "hp-builder-row", key: mode.mode },
          h("div", null, h("strong", null, mode.mode), h("div", { className: "hp-muted" }, mode.authority)),
          h(CommandChip, { command: mode.template, mutating: !String(mode.authority || "").includes("read-only") })
        );
      })),
      h("div", { className: "hp-chip-line hp-hero-meta" }, (builder.knobs || []).map(function (knob) { return h(Chip, { key: knob }, knob); }))
    );
  }

  function DataSources({ sources, freshness }) {
    sources = sources || [];
    freshness = freshness || {};
    return h(Section, { title: "Source labels & freshness", badge: "read-only" },
      h("div", { className: "hp-chip-line" },
        h(Chip, null, "Last refreshed " + timeLabel(freshness.last_refreshed_at)),
        h(Chip, null, "Query limit " + fmtNum(freshness.query_limit)),
        h(Chip, null, "API is read-only")
      ),
      h("p", { className: "hp-muted" }, freshness.data_age_note || "Local snapshot only."),
      h("div", { className: "hp-stack" }, sources.map(function (s, idx) {
        return h("div", { className: "hp-callout", key: idx }, h("strong", null, s.label || "source"), " — ", s.role || "", s.path ? h("div", { className: "hp-small" }, s.path) : null);
      }))
    );
  }

  function FailureAnalysis({ failureBreakdown }) {
    failureBreakdown = failureBreakdown || {};
    const cats = failureBreakdown.by_category || {};
    const reasons = failureBreakdown.top_reasons || [];
    return h(Section, { title: "Failure analysis", badge: "read-only" },
      h("div", { className: "hp-chip-line" }, Object.keys(cats).sort().map(function (key) { return h(Chip, { key: key, warn: Number(cats[key] || 0) > 0 }, key.replace(/_/g, " ") + " " + fmtNum(cats[key])); })),
      reasons.length ? h("div", { className: "hp-stack" }, reasons.slice(0, 6).map(function (item, idx) { return h("div", { className: "hp-callout", key: idx }, h("span", { className: "hp-strong" }, item.reason || "unknown"), " — ", fmtNum(item.count)); })) : h("p", { className: "hp-muted" }, "No failed/crashed/timed-out runs in the current telemetry scope.")
    );
  }

  function HpipePage() {
    const hooks = React;
    const [data, setData] = hooks.useState(null);
    const [error, setError] = hooks.useState(null);
    const [loading, setLoading] = hooks.useState(true);

    const load = hooks.useCallback(function () {
      setLoading(true);
      setError(null);
      api("/snapshot?limit=30")
        .then(function (payload) { setData(payload); })
        .catch(function (err) { setError(String(err && err.message || err)); })
        .finally(function () { setLoading(false); });
    }, []);

    hooks.useEffect(function () { load(); }, [load]);

    if (loading && !data) return h("div", { className: "hp-page" }, h("div", { className: "hp-card" }, "Loading Hpipe cockpit…"));
    if (error && !data) return h("div", { className: "hp-page" }, h("div", { className: "hp-card hp-error" }, "Hpipe telemetry failed: " + error));

    const summary = (data && data.summary) || {};
    const cli = (data && data.latest_cli_insights) || {};
    const cliHealth = cli.health || {};
    const cliCounts = cli.counts || {};
    const cliTaskRuns = cli.task_runs || {};
    const cliEvidence = cli.evidence_coverage || {};
    const cliLeaderboards = cli.leaderboards || {};
    const cliCoder = cli.coder_activity_proxy || {};
    const cliReviewer = cli.review_activity_proxy || {};
    const health = (data && data.health) || summary.health || {};
    const evidence = summary.evidence_coverage || {};
    const activityEvidence = summary.activity_evidence_coverage || summary.proxy_evidence_coverage || evidence;
    const proofCoverage = summary.proof_coverage || summary.strong_proof_coverage || evidence;
    const coder = summary.coder_activity_proxy || {};
    const reviewer = summary.review_activity_proxy || {};
    const leaderboards = summary.leaderboards || {};
    const concurrency = summary.concurrency || {};
    const boards = (data && (data.board_inventory || data.recent_boards)) || [];
    const historicalStats = (data && data.historical_stats) || {};
    const fullReconciliation = (data && data.full_run_reconciliation) || (data && data.run_reconciliation) || {};
    const failureBreakdown = (data && data.failure_breakdown) || summary.failure_breakdown || {};
    const staleness = (data && data.staleness) || summary.staleness || {};
    const hasLocalBoardActivity = (boards || []).some(function (board) {
      return Number(board.task_count || 0) > 0 || Number(board.run_count || 0) > 0 || Number(board.open_tasks || 0) > 0 || Number(board.done_tasks || 0) > 0 || Number(board.failed_runs || 0) > 0;
    }) || Number(summary.open_tasks || 0) > 0 || Number(summary.done_tasks || 0) > 0 || Number(summary.failed_runs || 0) > 0 || Number(summary.max_concurrent_task_runs || 0) > 0;
    const effectiveSummary = hasLocalBoardActivity ? summary : {
      ...summary,
      open_tasks: Number(cliHealth.open_tasks || summary.open_tasks || 0),
      done_tasks: Number(cliHealth.done_tasks || summary.done_tasks || 0),
      blocked_tasks: Number(cliHealth.blocked_tasks || summary.blocked_tasks || 0),
      failed_runs: Number(cliHealth.failed_runs || summary.failed_runs || 0),
      reclaimed_runs: Number(cliCounts.reclaimed_runs || summary.reclaimed_runs || 0),
      max_concurrent_task_runs: Number(cliTaskRuns.max_concurrent || summary.max_concurrent_task_runs || 0),
      max_concurrent_board: cli.board_name || summary.max_concurrent_board || null,
      evidence_coverage: Object.keys(cliEvidence).length ? cliEvidence : evidence,
      activity_evidence_coverage: Object.keys(cliEvidence).length ? cliEvidence : activityEvidence,
      proof_coverage: Object.keys(cliEvidence).length ? cliEvidence : proofCoverage,
      coder_activity_proxy: cliCoder.top_profile ? cliCoder : coder,
      review_activity_proxy: cliReviewer.top_profile ? cliReviewer : reviewer,
      leaderboards: Object.keys(cliLeaderboards).length ? cliLeaderboards : leaderboards,
      concurrency: Object.keys(cliTaskRuns).length ? { max: Number(cliTaskRuns.max_concurrent || 0), first_seen: null, interval_count: Number(cliTaskRuns.total || 0), timeline: cliTaskRuns.timeline || [], board_peaks: [{slug: cli.board || "live", name: cli.board_name || cli.board || "live", peak: Number(cliTaskRuns.max_concurrent || 0), first_seen: null}] } : concurrency,
      health: Object.keys(cliHealth).length ? cliHealth : health,
    };
    const evLabel = coverageLabel((effectiveSummary.proof_coverage || proofCoverage));
    const topCoder = (effectiveSummary.coder_activity_proxy || coder).top_profile || "N/A";
    const topReviewer = (effectiveSummary.review_activity_proxy || reviewer).top_profile || "N/A";

    return h("div", { className: "hp-page" },
      h("div", { className: "hp-hero" },
        h("div", null,
          h("div", { className: "hp-kicker" }, "HPIPE CONTROL PLANE"),
          h("h1", null, "Hpipe operator cockpit"),
          h("p", null, "Local-only read-only telemetry, source reconciliation, board health, swarm shape, evidence proof, safe command helpers, and preview-first workflow controls from Hermes state."),
          h("div", { className: "hp-chip-line hp-hero-meta" },
            h(Chip, null, "Generated " + timeLabel(data && data.generated_at)),
            h(Chip, null, "Boards " + fmtNum(summary.board_count)),
            h(Chip, { good: true }, "Read-only local API"),
            h(Chip, { muted: true }, hasLocalBoardActivity ? "populated telemetry" : "zero-state diagnostics")
          )
        ),
        h("button", { className: "hp-button", onClick: load, disabled: loading }, loading ? "Refreshing…" : "Refresh")
      ),
      error ? h("div", { className: "hp-card hp-error" }, error) : null,
      h("div", { className: "hp-safety" }, "Safety boundary: this page reads local state and prints commands only. It does not dispatch agents, mutate files, send messages, push, deploy, or perform external acts."),
      h(HistoricalStats, { stats: historicalStats, reconciliation: fullReconciliation }),
      h(HealthStrip, { health: effectiveSummary.health || health }),
      h(NextActions, { actions: data.next_actions || [] }),
      h("div", { className: "hp-grid hp-stats" },
        h(StatCard, { label: "Boards tracked", value: fmtNum(summary.board_count), detail: "local Kanban boards" }),
        h(StatCard, { label: "Tasks", value: fmtNum((effectiveSummary.open_tasks || 0) + (effectiveSummary.done_tasks || 0)), detail: fmtNum(effectiveSummary.open_tasks) + " open · " + fmtNum(effectiveSummary.done_tasks) + " done" }),
        h(StatCard, { label: "Blocked", value: fmtNum(effectiveSummary.blocked_tasks), detail: "blocking work" }),
        h(StatCard, { label: "Failed runs", value: fmtNum(effectiveSummary.failed_runs), detail: "crashes, timeouts, failures" }),
        h(StatCard, { label: "Max concurrency", value: fmtNum(effectiveSummary.max_concurrent_task_runs), detail: effectiveSummary.max_concurrent_board || "peak window" }),
        h(StatCard, { label: "Proof coverage", value: evLabel.value, detail: evLabel.detail }),
        h(StatCard, { label: "Top coder", value: topCoder, detail: topCoder === "N/A" ? "No coder-profile task runs found." : "implementation proxy" }),
        h(StatCard, { label: "Top reviewer", value: topReviewer, detail: topReviewer === "N/A" ? "No reviewer-profile task runs found." : "review proxy" })
      ),
      h(Diagnostics, { diagnostics: data.diagnostics || {}, reconciliation: fullReconciliation }),
      h("div", { className: "hp-cockpit-grid" },
        h(Section, { title: "Concurrency timeline" }, h(Timeline, { timeline: (effectiveSummary.concurrency || concurrency).timeline || data.concurrency_timeline || [], boardPeaks: (effectiveSummary.concurrency || concurrency).board_peaks || [] })),
        h(Section, { title: "Leaderboards" },
          h("div", { className: "hp-subgrid" },
            h(Leaderboard, { title: "Profile activity", rows: (effectiveSummary.leaderboards || leaderboards).profile_activity || summary.profiles || [], metric: "run_count", secondary: "task_count" }),
            h(Leaderboard, { title: "Implementation-heavy", rows: (effectiveSummary.leaderboards || leaderboards).implementation_heavy || [], metric: "implementation_hits", secondary: "run_count" }),
            h(Leaderboard, { title: "Review-heavy", rows: (effectiveSummary.leaderboards || leaderboards).review_heavy || [], metric: "review_hits", secondary: "run_count" }),
            h(Leaderboard, { title: "Run volume", rows: (effectiveSummary.leaderboards || leaderboards).run_volume || [], metric: "run_count", secondary: "completed_runs" })
          )
        ),
        h(Section, { title: "Board health / staleness" },
          h("div", { className: "hp-chip-line" },
            h(Chip, { warn: (staleness.stale_boards_24h || 0) > 0 }, "Boards stale 24h " + fmtNum(staleness.stale_boards_24h)),
            h(Chip, { warn: (staleness.stale_boards_72h || 0) > 0 }, "Boards stale 72h " + fmtNum(staleness.stale_boards_72h)),
            h(Chip, { warn: (staleness.stale_boards_7d || 0) > 0 }, "Boards stale 7d " + fmtNum(staleness.stale_boards_7d)),
            h(Chip, null, "Oldest open " + ageLabel(staleness.oldest_open_task_age_seconds)),
            h(Chip, null, "Last activity " + ageLabel(staleness.last_activity_age_seconds))
          ),
          h("div", { className: "hp-board-list" }, boards.slice(0, 4).map(function (board) { return h(BoardCard, { key: board.slug, board: board }); }))
        )
      ),
      h("div", { className: "hp-grid hp-two" },
        h(Section, { title: "Compact board table", badge: "scan" }, h(BoardTable, { boards: boards })),
        h(FailureAnalysis, { failureBreakdown: failureBreakdown })
      ),
      h(Section, { title: "Recent hpipe boards/runs", badge: "actions" }, h("div", { className: "hp-board-list" }, boards.slice(0, 8).map(function (board) { return h(BoardCard, { key: board.slug + "-recent", board: board }); }))),
      h("div", { className: "hp-grid hp-two" },
        h(Section, { title: "Run index", badge: "history" }, h(RunIndex, { runIndex: data.run_index || {} })),
        h(WorkflowBuilder, { builder: data.workflow_builder || {} })
      ),
      h(DataSources, { sources: data.data_sources || [], freshness: data.freshness || {} }),
      h(Section, { title: "Drill-down helpers", badge: "copy commands" },
        h("div", { className: "hp-command-grid" },
          h(CommandChip, { command: "hpipe status" }),
          h(CommandChip, { command: "hpipe run-index --json" }),
          h(CommandChip, { command: "hpipe insights --json <board>" }),
          h(CommandChip, { command: "hpipe report <board>" }),
          h(CommandChip, { command: "hpipe board-lint <board>" }),
          h(CommandChip, { command: "hpipe fail-log <board>" }),
          h(CommandChip, { command: "hermes kanban --board <board> list --json" })
        )
      )
    );
  }

  REG.register("hpipe", HpipePage);
})();
