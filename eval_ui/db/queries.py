"""
ClickHouse query module for the AI Eval Platform.
All database interactions go through the ClickHouseQueries class.
"""

import os
import uuid
from datetime import datetime, timedelta
from typing import Any

from clickhouse_driver import Client


class ClickHouseQueries:
    """
    Provides read and write access to the ClickHouse eval database.
    Uses the native protocol (port 9000) via clickhouse-driver.
    """

    def __init__(self) -> None:
        host = os.environ.get("CLICKHOUSE_HOST", "localhost")
        # clickhouse-driver uses the native TCP port (9000), not the HTTP port (8123)
        port = int(os.environ.get("CLICKHOUSE_NATIVE_PORT", "9000"))
        database = os.environ.get("CLICKHOUSE_DB", "otel")
        user = os.environ.get("CLICKHOUSE_USER", "default")
        password = os.environ.get("CLICKHOUSE_PASSWORD", "")

        self.database = database
        self.client = Client(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            settings={"use_numpy": False},
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _execute(self, query: str, params: dict | None = None) -> list[dict]:
        """Execute a query and return a list of dicts."""
        result = self.client.execute(query, params or {}, with_column_types=True)
        rows, columns = result
        col_names = [col[0] for col in columns]
        return [dict(zip(col_names, row)) for row in rows]

    def _execute_no_result(self, query: str, params: dict | None = None) -> None:
        """Execute a query that returns no rows (INSERT / ALTER)."""
        self.client.execute(query, params or {})

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

    def get_runs(self, limit: int = 50) -> list[dict]:
        """Return the most recent eval runs."""
        query = """
            SELECT
                run_id,
                name,
                suite,
                agent_version,
                dataset_version,
                created_at,
                is_baseline,
                metadata
            FROM otel.eval_runs
            ORDER BY created_at DESC
            LIMIT %(limit)s
        """
        return self._execute(query, {"limit": limit})

    def get_run_by_id(self, run_id: str) -> dict:
        """Return a single run by its UUID."""
        query = """
            SELECT
                run_id,
                name,
                suite,
                agent_version,
                created_at,
                is_baseline,
                metadata
            FROM otel.eval_runs
            WHERE run_id = %(run_id)s
            LIMIT 1
        """
        rows = self._execute(query, {"run_id": run_id})
        return rows[0] if rows else {}

    def get_baseline_run(self) -> dict | None:
        """Return the current baseline run, or None if none is set."""
        query = """
            SELECT
                run_id,
                name,
                suite,
                agent_version,
                created_at,
                is_baseline,
                metadata
            FROM otel.eval_runs
            WHERE is_baseline = 1
            ORDER BY created_at DESC
            LIMIT 1
        """
        rows = self._execute(query)
        return rows[0] if rows else None

    def get_scores_for_run(self, run_id: str) -> list[dict]:
        """Return average score per metric for a run."""
        query = """
            SELECT
                metric,
                avg(score)      AS avg_score,
                count()         AS num_scores,
                min(score)      AS min_score,
                max(score)      AS max_score
            FROM otel.eval_scores
            WHERE run_id = %(run_id)s
            GROUP BY metric
            ORDER BY metric
        """
        return self._execute(query, {"run_id": run_id})

    def get_scores_for_trace(self, trace_id: str) -> list[dict]:
        """Return all eval scores for a specific trace."""
        query = """
            SELECT
                id,
                trace_id,
                run_id,
                span_id,
                evaluator,
                metric,
                score,
                reasoning,
                eval_type,
                evaluated_at
            FROM otel.eval_scores
            WHERE trace_id = %(trace_id)s
            ORDER BY metric
        """
        return self._execute(query, {"trace_id": trace_id})

    def get_all_scores_for_run(self, run_id: str) -> list[dict]:
        """Return all individual scores (not aggregated) for a run."""
        query = """
            SELECT
                id,
                trace_id,
                run_id,
                span_id,
                evaluator,
                metric,
                score,
                reasoning,
                eval_type,
                evaluated_at
            FROM otel.eval_scores
            WHERE run_id = %(run_id)s
            ORDER BY evaluated_at DESC
        """
        return self._execute(query, {"run_id": run_id})

    def get_regression(self, run_id: str, baseline_run_id: str) -> list[dict]:
        """
        Return per-metric deltas between a run and a baseline run.
        Positive delta = improvement, negative = regression.
        """
        query = """
            SELECT
                current.metric                      AS metric,
                baseline.avg_score                  AS baseline_score,
                current.avg_score                   AS current_score,
                current.avg_score - baseline.avg_score AS delta
            FROM (
                SELECT metric, avg(score) AS avg_score
                FROM otel.eval_scores
                WHERE run_id = %(run_id)s
                GROUP BY metric
            ) AS current
            LEFT JOIN (
                SELECT metric, avg(score) AS avg_score
                FROM otel.eval_scores
                WHERE run_id = %(baseline_run_id)s
                GROUP BY metric
            ) AS baseline USING (metric)
            ORDER BY metric
        """
        return self._execute(query, {"run_id": run_id, "baseline_run_id": baseline_run_id})

    def get_benchmarks(self, suite: str | None = None, dataset_version: str | None = None) -> list[dict]:
        """Return benchmarks, optionally filtered by suite and/or dataset_version."""
        conditions = []
        params: dict = {}
        if suite:
            conditions.append("suite = %(suite)s")
            params["suite"] = suite
        if dataset_version:
            conditions.append("dataset_version = %(dataset_version)s")
            params["dataset_version"] = dataset_version
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"""
            SELECT
                benchmark_id,
                suite,
                name,
                task_input,
                expected_output,
                rubric,
                difficulty,
                tags,
                dataset_version,
                active,
                created_at
            FROM otel.benchmarks
            {where}
            ORDER BY created_at DESC
        """
        return self._execute(query, params)

    def get_traces_for_run(self, run_id: str, limit: int = 200) -> list[dict]:
        """Return agent.task spans tagged with a specific run_id."""
        return self._execute(
            "SELECT TraceId, SpanId, ParentSpanId, SpanName, ServiceName, "
            "Duration, StatusCode, SpanAttributes, Timestamp "
            "FROM otel.otel_traces "
            "WHERE SpanName = 'agent.task' AND SpanAttributes['run.id'] = %(run_id)s "
            "ORDER BY Timestamp DESC LIMIT %(limit)s",
            {"run_id": run_id, "limit": limit},
        )

    def get_prompts_for_run(self, run_id: str, limit: int = 200) -> list[dict]:
        """Return prompt_evals rows for a specific run_id."""
        return self._execute(
            "SELECT prompt_eval_id, prompt_hash, trace_id, run_id, agent_name, model, "
            "prompt_text, response_text, latency_ms, prompt_tokens, completion_tokens, "
            "scores, created_at FROM otel.prompt_evals "
            "WHERE run_id = %(run_id)s ORDER BY created_at DESC LIMIT %(limit)s",
            {"run_id": run_id, "limit": limit},
        )

    def get_recent_traces(self, limit: int = 50, start=None, end=None) -> list[dict]:
        """Return recent top-level agent task spans from otel_traces."""
        time_filter = ""
        params: dict = {"limit": limit}
        if start is not None:
            time_filter += " AND Timestamp >= %(start)s"
            params["start"] = start
        if end is not None:
            time_filter += " AND Timestamp <= %(end)s"
            params["end"] = end
        query = f"""
            SELECT
                TraceId,
                SpanId,
                ParentSpanId,
                SpanName,
                ServiceName,
                Duration,
                StatusCode,
                SpanAttributes,
                Timestamp
            FROM otel.otel_traces
            WHERE SpanName = 'agent.task'
            {time_filter}
            ORDER BY Timestamp DESC
            LIMIT %(limit)s
        """
        return self._execute(query, params)

    def get_trace_spans(self, trace_id: str) -> list[dict]:
        """Return all spans belonging to a trace, sorted by timestamp."""
        query = """
            SELECT
                TraceId,
                SpanId,
                ParentSpanId,
                SpanName,
                ServiceName,
                Duration,
                StatusCode,
                SpanAttributes,
                Timestamp
            FROM otel.otel_traces
            WHERE TraceId = %(trace_id)s
            ORDER BY Timestamp ASC
        """
        return self._execute(query, {"trace_id": trace_id})

    def get_pending_reviews(self) -> list[dict]:
        """
        Return eval scores that have reasoning but have not yet been
        human-reviewed (i.e. no matching row in human_reviews).
        """
        query = """
            SELECT
                es.id          AS score_id,
                es.trace_id,
                es.run_id,
                es.metric,
                es.score        AS auto_score,
                es.reasoning,
                es.evaluator,
                es.eval_type,
                es.evaluated_at
            FROM otel.eval_scores AS es
            LEFT JOIN otel.human_reviews AS hr ON hr.score_id = es.id
            WHERE es.reasoning != ''
              AND hr.id = '00000000-0000-0000-0000-000000000000'
            ORDER BY es.evaluated_at DESC
        """
        # ClickHouse LEFT JOIN: unmatched rows get default values (UUID zero)
        return self._execute(query)

    def get_score_trends(self, metric: str, days: int = 7) -> list[dict]:
        """Return daily average score for a specific metric over the last N days."""
        query = """
            SELECT
                toDate(evaluated_at)   AS day,
                avg(score)             AS avg_score,
                count()                AS num_scores
            FROM otel.eval_scores
            WHERE metric   = %(metric)s
              AND evaluated_at >= now() - INTERVAL %(days)s DAY
            GROUP BY day
            ORDER BY day ASC
        """
        return self._execute(query, {"metric": metric, "days": days})

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    def create_run(self, name: str, suite: str, agent_version: str,
                   agent_endpoint: str = "", benchmark_ids: list[str] | None = None,
                   run_type: str = "", run_group: str = "") -> str:
        """Insert a new eval run and return its run_id."""
        run_id = str(uuid.uuid4())
        metadata = {}
        if agent_endpoint:
            metadata["agent_endpoint"] = agent_endpoint
        if benchmark_ids:
            metadata["benchmark_ids"] = ",".join(benchmark_ids)
        if run_type:
            metadata["run_type"] = run_type
        if run_group:
            metadata["run_group"] = run_group
        query = """
            INSERT INTO otel.eval_runs
                (run_id, name, suite, agent_version, created_at, is_baseline, metadata)
            VALUES
        """
        self.client.execute(
            query,
            [
                {
                    "run_id": run_id,
                    "name": name,
                    "suite": suite,
                    "agent_version": agent_version,
                    "created_at": datetime.utcnow(),
                    "is_baseline": 0,
                    "metadata": metadata,
                }
            ],
        )
        return run_id

    def update_run(self, run_id: str, name: str, suite: str, agent_version: str,
                   agent_endpoint: str = "", benchmark_ids: list[str] | None = None,
                   run_type: str = "", run_group: str = "") -> None:
        """Update an existing run's fields via ALTER TABLE UPDATE (mutation)."""
        metadata = {}
        if agent_endpoint:
            metadata["agent_endpoint"] = agent_endpoint
        if benchmark_ids:
            metadata["benchmark_ids"] = ",".join(benchmark_ids)
        if run_type:
            metadata["run_type"] = run_type
        if run_group:
            metadata["run_group"] = run_group
        # Escape single quotes in strings
        safe_name    = name.replace("'", "\\'")
        safe_suite   = suite.replace("'", "\\'")
        safe_version = agent_version.replace("'", "\\'")
        meta_parts   = ", ".join(f"'{k}', '{v.replace(chr(39), chr(92)+chr(39))}'" for k, v in metadata.items())
        meta_expr    = f"map({meta_parts})" if meta_parts else "map()"
        self._execute_no_result(
            f"ALTER TABLE otel.eval_runs UPDATE "
            f"name='{safe_name}', suite='{safe_suite}', agent_version='{safe_version}', "
            f"metadata={meta_expr} "
            f"WHERE run_id='{run_id}'"
        )

    def delete_run(self, run_id: str) -> None:
        """Hard-delete a run row."""
        self._execute_no_result(
            f"ALTER TABLE otel.eval_runs DELETE WHERE run_id='{run_id}'"
        )

    def update_benchmark(self, benchmark_id: str, suite: str, name: str, task_input: str,
                          expected_output: str, rubric: str, difficulty: str, tags: list[str],
                          dataset_version: str = "v1") -> None:
        """Update an existing benchmark via ALTER TABLE UPDATE."""
        safe = lambda s: s.replace("'", "\\'")
        tags_expr = "[" + ", ".join(f"'{safe(t)}'" for t in tags) + "]"
        self._execute_no_result(
            f"ALTER TABLE otel.benchmarks UPDATE "
            f"suite='{safe(suite)}', name='{safe(name)}', task_input='{safe(task_input)}', "
            f"expected_output='{safe(expected_output)}', rubric='{safe(rubric)}', "
            f"difficulty='{safe(difficulty)}', tags={tags_expr}, "
            f"dataset_version='{safe(dataset_version)}' "
            f"WHERE benchmark_id='{benchmark_id}'"
        )

    def set_baseline(self, run_id: str) -> None:
        """
        Mark the given run as baseline and clear all other baselines.
        ClickHouse is immutable so we use ALTER TABLE UPDATE (MutatingMergeTree).
        """
        # Clear existing baselines
        self._execute_no_result(
            "ALTER TABLE otel.eval_runs UPDATE is_baseline = 0 WHERE is_baseline = 1"
        )
        # Set the new baseline
        self._execute_no_result(
            "ALTER TABLE otel.eval_runs UPDATE is_baseline = 1 WHERE run_id = %(run_id)s",
            {"run_id": run_id},
        )

    def save_human_review(
        self,
        score_id: str,
        trace_id: str,
        metric: str,
        human_score: float,
        auto_score: float,
        notes: str,
        reviewer: str,
    ) -> None:
        """Insert a human review record."""
        review_id = str(uuid.uuid4())
        self.client.execute(
            """
            INSERT INTO otel.human_reviews
                (id, score_id, trace_id, metric, human_score, auto_score, notes, reviewer, reviewed_at)
            VALUES
            """,
            [
                {
                    "id": review_id,
                    "score_id": score_id,
                    "trace_id": trace_id,
                    "metric": metric,
                    "human_score": float(human_score),
                    "auto_score": float(auto_score),
                    "notes": notes,
                    "reviewer": reviewer,
                    "reviewed_at": datetime.utcnow(),
                }
            ],
        )

    def save_benchmark(
        self,
        suite: str,
        name: str,
        task_input: str,
        expected_output: str,
        rubric: str,
        difficulty: str,
        tags: list,
        dataset_version: str = "v1",
    ) -> str:
        """Insert a new benchmark and return its benchmark_id."""
        benchmark_id = str(uuid.uuid4())
        self.client.execute(
            """
            INSERT INTO otel.benchmarks
                (benchmark_id, suite, name, task_input, expected_output, rubric,
                 difficulty, tags, dataset_version, active, created_at)
            VALUES
            """,
            [
                {
                    "benchmark_id":   benchmark_id,
                    "suite":          suite,
                    "name":           name,
                    "task_input":     task_input,
                    "expected_output": expected_output,
                    "rubric":         rubric,
                    "difficulty":     difficulty,
                    "tags":           tags,
                    "dataset_version": dataset_version,
                    "active":         1,
                    "created_at":     datetime.utcnow(),
                }
            ],
        )
        return benchmark_id

    def delete_benchmark(self, benchmark_id: str) -> None:
        """
        Soft-delete a benchmark by setting active = 0.
        ClickHouse does not support traditional DELETE on non-TTL tables
        without an ALTER TABLE DELETE (mutation), so we use ALTER UPDATE.
        """
        self._execute_no_result(
            "ALTER TABLE otel.benchmarks UPDATE active = 0 WHERE benchmark_id = %(benchmark_id)s",
            {"benchmark_id": benchmark_id},
        )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def save_execution(
        self,
        run_id: str,
        run_name: str,
        benchmark_id: str,
        benchmark_name: str,
        trace_id: str,
        status: str,
        error_detail: str,
        started_at: datetime,
        ended_at: datetime,
        duration_ms: int,
        source: str = "benchmark",
    ) -> str:
        """Insert one row into run_executions and return execution_id."""
        execution_id = str(uuid.uuid4())
        self.client.execute(
            """
            INSERT INTO otel.run_executions
                (execution_id, run_id, run_name, benchmark_id, benchmark_name,
                 trace_id, status, error_detail, started_at, ended_at, duration_ms, source)
            VALUES
            """,
            [{
                "execution_id":   execution_id,
                "run_id":         run_id,
                "run_name":       run_name,
                "benchmark_id":   benchmark_id,
                "benchmark_name": benchmark_name,
                "trace_id":       trace_id,
                "status":         status,
                "error_detail":   error_detail,
                "started_at":     started_at,
                "ended_at":       ended_at,
                "duration_ms":    duration_ms,
                "source":         source,
            }],
        )
        return execution_id

    def get_executions_for_run(self, run_id: str, limit: int = 500) -> list[dict]:
        """Return all execution records for a run, newest first."""
        return self._execute(
            """
            SELECT
                execution_id, run_id, run_name, benchmark_id, benchmark_name,
                trace_id, status, error_detail, started_at, ended_at, duration_ms, source
            FROM otel.run_executions
            WHERE run_id = %(run_id)s
            ORDER BY started_at DESC
            LIMIT %(limit)s
            """,
            {"run_id": run_id, "limit": limit},
        )

    def get_all_executions(self, limit: int = 500) -> list[dict]:
        """Return most recent execution records across all runs."""
        return self._execute(
            """
            SELECT
                execution_id, run_id, run_name, benchmark_id, benchmark_name,
                trace_id, status, error_detail, started_at, ended_at, duration_ms, source
            FROM otel.run_executions
            ORDER BY started_at DESC
            LIMIT %(limit)s
            """,
            {"limit": limit},
        )

    def get_conversations(self, start=None, end=None, limit: int = 50) -> list[dict]:
        """Return distinct conversation IDs with turn counts and timestamps."""
        time_filter = ""
        params: dict = {"limit": limit}
        if start is not None:
            time_filter += " AND Timestamp >= %(start)s"
            params["start"] = start
        if end is not None:
            time_filter += " AND Timestamp <= %(end)s"
            params["end"] = end
        query = f"""
            SELECT
                SpanAttributes['conversation.id'] AS conversation_id,
                count()                           AS turn_count,
                min(Timestamp)                    AS first_turn_at,
                max(Timestamp)                    AS last_turn_at,
                groupUniqArray(ServiceName)       AS services
            FROM otel.otel_traces
            WHERE SpanName = 'agent.task'
              AND SpanAttributes['conversation.id'] != ''
              {time_filter}
            GROUP BY conversation_id
            ORDER BY last_turn_at DESC
            LIMIT %(limit)s
        """
        return self._execute(query, params)

    def get_conversation_turns(self, conversation_id: str) -> list[dict]:
        """Return all agent.task spans for a conversation, ordered by time."""
        return self._execute(
            """
            SELECT TraceId, SpanId, SpanName, ServiceName, Duration,
                   StatusCode, SpanAttributes, Timestamp
            FROM otel.otel_traces
            WHERE SpanName = 'agent.task'
              AND SpanAttributes['conversation.id'] = %(conv_id)s
            ORDER BY Timestamp ASC
            """,
            {"conv_id": conversation_id},
        )

    def get_scores_for_conversation(self, conversation_id: str) -> list[dict]:
        """Return multi-turn eval scores stored with trace_id = conversation_id."""
        return self._execute(
            """
            SELECT id, trace_id, run_id, metric, score, reasoning,
                   eval_type, evaluator, evaluated_at
            FROM otel.eval_scores
            WHERE trace_id = %(conv_id)s
            ORDER BY metric
            """,
            {"conv_id": conversation_id},
        )

    # ------------------------------------------------------------------
    # Alert Thresholds
    # ------------------------------------------------------------------

    def get_thresholds(self) -> list[dict]:
        """Return all configured alert thresholds.
        FINAL forces ReplacingMergeTree to deduplicate so only the latest
        row per metric is returned, even before background merges complete.
        """
        return self._execute(
            "SELECT metric, operator, threshold, enabled "
            "FROM otel.alert_thresholds FINAL ORDER BY metric"
        )

    def upsert_threshold(self, metric: str, operator: str, threshold: float, enabled: int) -> None:
        """Insert or replace a threshold row (ReplacingMergeTree handles dedup)."""
        self._execute_no_result(
            "INSERT INTO otel.alert_thresholds (metric, operator, threshold, enabled) "
            "VALUES (%(metric)s, %(operator)s, %(threshold)s, %(enabled)s)",
            {"metric": metric, "operator": operator,
             "threshold": float(threshold), "enabled": int(enabled)},
        )

    def delete_threshold(self, metric: str) -> None:
        """Remove a threshold by metric name."""
        self._execute_no_result(
            "ALTER TABLE otel.alert_thresholds DELETE WHERE metric = %(metric)s",
            {"metric": metric},
        )

    def get_violations(self, hours: int = 24, limit: int = 200) -> list[dict]:
        """
        Return eval score rows that breach an active threshold within the
        last `hours` hours, enriched with prompt text and agent name.
        """
        return self._execute(f"""
            SELECT
                es.trace_id        AS trace_id,
                es.span_id         AS span_id,
                es.metric          AS metric,
                es.score           AS score,
                es.reasoning       AS reasoning,
                es.eval_type       AS eval_type,
                es.evaluator       AS evaluator,
                es.evaluated_at    AS evaluated_at,
                pe.agent_name      AS agent_name,
                pe.prompt_text     AS prompt_text,
                pe.response_text   AS response_text
            FROM otel.eval_scores es
            JOIN (SELECT * FROM otel.alert_thresholds FINAL) t ON es.metric = t.metric
            LEFT JOIN (
                SELECT
                    trace_id,
                    span_id,
                    argMax(agent_name,    prompt_tokens + completion_tokens) AS agent_name,
                    argMax(prompt_text,   prompt_tokens + completion_tokens) AS prompt_text,
                    argMax(response_text, prompt_tokens + completion_tokens) AS response_text
                FROM otel.prompt_evals
                GROUP BY trace_id, span_id
            ) pe ON es.trace_id = pe.trace_id AND es.span_id = pe.span_id
            WHERE t.enabled = 1
              AND es.evaluated_at >= now() - INTERVAL {int(hours)} HOUR
              AND (
                  (t.operator = 'lt'  AND es.score <  t.threshold) OR
                  (t.operator = 'gt'  AND es.score >  t.threshold) OR
                  (t.operator = 'lte' AND es.score <= t.threshold) OR
                  (t.operator = 'gte' AND es.score >= t.threshold)
              )
            ORDER BY es.evaluated_at DESC
            LIMIT {int(limit)}
        """)

    def ping(self) -> bool:
        """Return True if ClickHouse is reachable."""
        try:
            self.client.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Governance
    # ------------------------------------------------------------------

    def get_gov_summary(self, hours: int = 24) -> dict:
        """Return aggregate governance KPIs for the last N hours."""
        try:
            r1 = self._execute(f"""
                SELECT
                    count(DISTINCT trace_id)                                    AS traces_scanned,
                    countIf(metric = 'pii_in_output'  AND value > 0)           AS pii_output_hits,
                    countIf(metric = 'pii_in_input'   AND value > 0)           AS pii_input_hits,
                    avgIf(value, metric = 'prompt_snapshot_coverage')           AS snapshot_coverage,
                    avgIf(value, metric = 'pii_leak_rate')                      AS avg_pii_leak_rate
                FROM otel.gov_metric_snapshots
                WHERE ts >= now() - INTERVAL {int(hours)} HOUR
            """)
            r2 = self._execute(f"""
                SELECT
                    countIf(decision = 'block') AS total_blocks,
                    countIf(decision = 'warn')  AS total_warnings,
                    countIf(decision = 'pass')  AS total_passes
                FROM otel.gov_policy_decisions
                WHERE ts >= now() - INTERVAL {int(hours)} HOUR
            """)
            summary: dict = {}
            if r1:
                summary.update(r1[0])
            if r2:
                summary.update(r2[0])
            return summary
        except Exception:
            return {}

    def get_gov_policy_decisions(
        self,
        hours: int = 24,
        run_id: str = "",
        decision_filter: str = "",
        limit: int = 500,
    ) -> list[dict]:
        """Return policy decisions, optionally filtered by run or decision type."""
        conditions = [f"ts >= now() - INTERVAL {int(hours)} HOUR"]
        params: dict = {"limit": limit}
        if run_id:
            conditions.append("run_id = %(run_id)s")
            params["run_id"] = run_id
        if decision_filter:
            conditions.append("decision = %(decision_filter)s")
            params["decision_filter"] = decision_filter
        where = "WHERE " + " AND ".join(conditions)
        query = f"""
            SELECT decision_id, trace_id, run_id, metric,
                   decision, value, threshold, message, ts
            FROM otel.gov_policy_decisions
            {where}
            ORDER BY ts DESC
            LIMIT %(limit)s
        """
        try:
            return self._execute(query, params)
        except Exception:
            return []

    def get_gov_metric_trend(self, metric: str, hours: int = 48) -> list[dict]:
        """Return hourly average of a governance metric for trend charts."""
        query = f"""
            SELECT
                toStartOfHour(ts) AS hour,
                avg(value)        AS avg_value,
                count()           AS sample_count
            FROM otel.gov_metric_snapshots
            WHERE metric = %(metric)s
              AND span_id = ''
              AND ts >= now() - INTERVAL {int(hours)} HOUR
            GROUP BY hour
            ORDER BY hour ASC
        """
        try:
            return self._execute(query, {"metric": metric})
        except Exception:
            return []

    def get_gov_audit_log(self, hours: int = 168, limit: int = 100) -> list[dict]:
        """Return recent governance audit log events within the look-back window."""
        try:
            return self._execute(
                "SELECT event_id, trace_id, run_id, event_type, detail, ts "
                "FROM otel.gov_audit_log "
                "WHERE ts >= now() - INTERVAL %(hours)s HOUR "
                "ORDER BY ts DESC LIMIT %(limit)s",
                {"hours": hours, "limit": limit},
            )
        except Exception:
            return []

    def get_gov_hitl_queue(self, status: str = "pending") -> list[dict]:
        """Return HITL queue entries filtered by status."""
        try:
            return self._execute(
                "SELECT request_id, trace_id, span_id, run_id, risk_tier, "
                "action_type, payload, status, reviewer, notes, created_at, decided_at "
                "FROM otel.gov_hitl_queue FINAL "
                "WHERE status = %(status)s "
                "ORDER BY risk_tier DESC, created_at ASC",
                {"status": status},
            )
        except Exception:
            return []

    def update_gov_hitl_decision(
        self,
        request_id: str,
        status: str,
        reviewer: str,
        notes: str,
    ) -> None:
        """Approve or reject a HITL queue entry.

        ReplacingMergeTree deduplicates by request_id keeping the row with the
        latest decided_at, so we INSERT a new row rather than ALTER UPDATE.
        """
        rows = self._execute(
            "SELECT request_id, trace_id, span_id, run_id, risk_tier, "
            "action_type, payload, created_at "
            "FROM otel.gov_hitl_queue FINAL "
            "WHERE request_id = %(rid)s LIMIT 1",
            {"rid": request_id},
        )
        if not rows:
            raise ValueError(f"HITL request {request_id} not found")
        r = rows[0]
        safe = lambda s: str(s or "").replace("'", "\\'")
        self._execute_no_result(
            f"INSERT INTO otel.gov_hitl_queue "
            f"(request_id, trace_id, span_id, run_id, risk_tier, action_type, "
            f" payload, status, reviewer, notes, created_at, decided_at) VALUES "
            f"('{safe(r['request_id'])}', '{safe(r['trace_id'])}', "
            f"'{safe(r['span_id'])}', '{safe(r['run_id'])}', "
            f"'{safe(r['risk_tier'])}', '{safe(r['action_type'])}', "
            f"'{safe(r['payload'])}', '{safe(status)}', "
            f"'{safe(reviewer)}', '{safe(notes)}', "
            f"'{str(r['created_at'])[:19]}', now())"
        )

    # ------------------------------------------------------------------
    # Governance Phase 2 — budgets, drift, routing
    # ------------------------------------------------------------------

    def get_gov_budget_status(self, hours: int = 24) -> list[dict]:
        """Return token usage vs. budget for each agent within the look-back window.
        Token pricing rates are read from gov_threshold_config so they are
        configurable from the UI without code changes.
        """
        try:
            # Read pricing from configurable thresholds (fallback to gpt-4o-mini defaults)
            rates = {
                r["config_key"]: float(r["value"])
                for r in (self._execute(
                    "SELECT config_key, value FROM otel.gov_threshold_config FINAL "
                    "WHERE config_key IN ('budget.input_token_cost_per_1m', "
                    "                     'budget.output_token_cost_per_1m')"
                ) or [])
            }
            input_rate  = rates.get("budget.input_token_cost_per_1m",  0.15) / 1_000_000
            output_rate = rates.get("budget.output_token_cost_per_1m", 0.60) / 1_000_000

            return self._execute(f"""
                SELECT
                    pe.agent_name                                AS agent_role,
                    sum(pe.prompt_tokens + pe.completion_tokens) AS tokens_used,
                    sum(
                        pe.prompt_tokens     * {input_rate} +
                        pe.completion_tokens * {output_rate}
                    )                                            AS cost_usd_today,
                    any(b.daily_token_limit)                     AS daily_limit,
                    any(b.enabled)                               AS enabled,
                    if(
                        any(b.daily_token_limit) > 0,
                        sum(pe.prompt_tokens + pe.completion_tokens) /
                            any(b.daily_token_limit),
                        0
                    )                                            AS utilization
                FROM otel.prompt_evals pe
                LEFT JOIN (
                    SELECT agent_role, daily_token_limit, enabled
                    FROM otel.gov_token_budgets FINAL
                ) b ON pe.agent_name = b.agent_role
                WHERE pe.created_at >= now() - INTERVAL {int(hours)} HOUR
                  AND pe.agent_name != ''
                GROUP BY pe.agent_name
                ORDER BY utilization DESC
            """)
        except Exception:
            return []

    def get_gov_agent_budgets(self) -> list[dict]:
        """Return all configured agent budgets from gov_token_budgets."""
        try:
            return self._execute(
                "SELECT agent_role, daily_token_limit, cost_usd_limit, enabled, updated_at "
                "FROM otel.gov_token_budgets FINAL ORDER BY agent_role"
            ) or []
        except Exception:
            return []

    def save_gov_agent_budget(
        self,
        agent_role: str,
        daily_token_limit: int,
        cost_usd_limit: float,
        enabled: int = 1,
    ) -> None:
        """Upsert a budget row for an agent (ReplacingMergeTree deduplicates on agent_role)."""
        self.client.execute(
            "INSERT INTO otel.gov_token_budgets "
            "(agent_role, daily_token_limit, cost_usd_limit, enabled) VALUES",
            [{"agent_role": agent_role, "daily_token_limit": daily_token_limit,
              "cost_usd_limit": cost_usd_limit, "enabled": enabled}],
        )

    def delete_gov_agent_budget(self, agent_role: str) -> None:
        """Clear a budget by inserting a disabled zero-limit row (ReplacingMergeTree wins on updated_at)."""
        self.client.execute(
            "INSERT INTO otel.gov_token_budgets "
            "(agent_role, daily_token_limit, cost_usd_limit, enabled) VALUES",
            [{"agent_role": agent_role, "daily_token_limit": 0,
              "cost_usd_limit": 0.0, "enabled": 0}],
        )

    def get_gov_prompt_drift(self, hours: int = 48) -> list[dict]:
        """Return recent prompt drift events (new hashes vs. baseline)."""
        try:
            return self._execute(f"""
                SELECT
                    template_id,
                    snapshot_hash,
                    is_baseline,
                    drift_detected,
                    first_seen,
                    last_seen,
                    seen_count
                FROM otel.gov_prompt_drift FINAL
                WHERE last_seen >= now() - INTERVAL {int(hours)} HOUR
                ORDER BY last_seen DESC
            """)
        except Exception:
            return []

    def get_gov_drift_summary(self) -> dict:
        """Return aggregate drift stats: templates tracked, drift events."""
        try:
            rows = self._execute("""
                SELECT
                    count(DISTINCT template_id) AS templates_tracked,
                    countIf(drift_detected = 1)  AS drift_events,
                    countIf(is_baseline = 1)     AS baselines_set
                FROM otel.gov_prompt_drift FINAL
            """)
            return rows[0] if rows else {}
        except Exception:
            return {}

    def get_gov_routing_decisions(self, hours: int = 24, limit: int = 500) -> list[dict]:
        """Return model routing decisions for the look-back window."""
        try:
            return self._execute(
                f"SELECT decision_id, trace_id, run_id, agent_role, "
                f"complexity_tier, model, input_chars, estimated_savings_pct, ts "
                f"FROM otel.gov_routing_decisions "
                f"WHERE ts >= now() - INTERVAL {int(hours)} HOUR "
                f"ORDER BY ts DESC LIMIT %(limit)s",
                {"limit": limit},
            )
        except Exception:
            return []

    def get_gov_routing_summary(self, hours: int = 24) -> dict:
        """Aggregate routing stats: tier breakdown, avg savings."""
        try:
            rows = self._execute(f"""
                SELECT
                    count()                              AS total_decisions,
                    countIf(complexity_tier = 'simple')   AS simple_count,
                    countIf(complexity_tier = 'moderate') AS moderate_count,
                    countIf(complexity_tier = 'complex')  AS complex_count,
                    countIf(complexity_tier = 'critical') AS critical_count,
                    avg(estimated_savings_pct)            AS avg_savings_pct,
                    max(estimated_savings_pct)            AS max_savings_pct
                FROM otel.gov_routing_decisions
                WHERE ts >= now() - INTERVAL {int(hours)} HOUR
            """)
            return rows[0] if rows else {}
        except Exception:
            return {}

    def get_gov_gate_checks(self, hours: int = 24, limit: int = 200) -> list[dict]:
        """Return gate_check events from the audit log."""
        try:
            return self._execute(
                f"SELECT event_id, trace_id, run_id, detail, ts "
                f"FROM otel.gov_audit_log "
                f"WHERE event_type = 'gate_check' "
                f"  AND ts >= now() - INTERVAL {int(hours)} HOUR "
                f"ORDER BY ts DESC LIMIT %(limit)s",
                {"limit": limit},
            )
        except Exception:
            return []

    def get_gov_pii_breakdown(self, hours: int = 24) -> list[dict]:
        """Return per-span PII detection details (type counts in detail JSON)."""
        try:
            return self._execute(
                f"SELECT trace_id, span_id, detail, ts "
                f"FROM otel.gov_metric_snapshots "
                f"WHERE metric = 'pii_in_output' AND value > 0 "
                f"  AND ts >= now() - INTERVAL {int(hours)} HOUR "
                f"ORDER BY ts DESC LIMIT 200"
            )
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Governance Phase 3 — policies, anomalies, compliance, keys, webhooks
    # ------------------------------------------------------------------

    def get_gov_policies(self) -> list[dict]:
        """Return all policy rules (including disabled) for the editor."""
        try:
            return self._execute(
                "SELECT policy_id, metric, threshold, direction, decision, "
                "scope, scope_value, enabled, description, updated_at "
                "FROM otel.gov_policies FINAL ORDER BY metric ASC, decision DESC"
            )
        except Exception:
            return []

    def get_gov_anomaly_events(self, hours: int = 24, limit: int = 200) -> list[dict]:
        """Return recent anomaly detection events."""
        try:
            return self._execute(
                f"SELECT anomaly_id, trace_id, agent_role, metric, "
                f"observed_value, baseline_mean, baseline_stddev, z_score, severity, ts "
                f"FROM otel.gov_anomaly_events "
                f"WHERE ts >= now() - INTERVAL {int(hours)} HOUR "
                f"ORDER BY ts DESC LIMIT %(limit)s",
                {"limit": limit},
            )
        except Exception:
            return []

    def get_gov_anomaly_summary(self, hours: int = 24) -> dict:
        """Aggregate anomaly counts by severity."""
        try:
            rows = self._execute(
                f"SELECT "
                f"  count()                               AS total, "
                f"  countIf(severity = 'critical')        AS critical_count, "
                f"  countIf(severity = 'high')            AS high_count, "
                f"  countIf(severity = 'medium')          AS medium_count "
                f"FROM otel.gov_anomaly_events "
                f"WHERE ts >= now() - INTERVAL {int(hours)} HOUR"
            )
            return rows[0] if rows else {}
        except Exception:
            return {}

    def get_gov_remediation_log(self, hours: int = 24, limit: int = 200) -> list[dict]:
        """Return recent automated remediation actions."""
        try:
            return self._execute(
                f"SELECT action_id, trace_id, run_id, trigger, action_type, detail, ts "
                f"FROM otel.gov_remediation_log "
                f"WHERE ts >= now() - INTERVAL {int(hours)} HOUR "
                f"ORDER BY ts DESC LIMIT %(limit)s",
                {"limit": limit},
            )
        except Exception:
            return []

    def get_gov_compliance_reports(self, limit: int = 20) -> list[dict]:
        """Return list of generated compliance reports."""
        try:
            return self._execute(
                "SELECT report_id, report_type, from_ts, to_ts, "
                "generated_by, created_at "
                "FROM otel.gov_compliance_reports "
                "ORDER BY created_at DESC LIMIT %(limit)s",
                {"limit": limit},
            )
        except Exception:
            return []

    def get_gov_agent_keys(self) -> list[dict]:
        """Return active (non-revoked) agent API keys."""
        try:
            return self._execute(
                "SELECT key_id, agent_role, key_prefix, enabled, "
                "created_at, last_used_at "
                "FROM otel.gov_agent_keys FINAL "
                "WHERE revoked_at = '1970-01-01 00:00:00' "
                "ORDER BY created_at DESC"
            )
        except Exception:
            return []

    def get_gov_webhooks(self) -> list[dict]:
        """Return all webhook configurations."""
        try:
            return self._execute(
                "SELECT webhook_id, name, url, events, enabled, updated_at "
                "FROM otel.gov_webhook_configs FINAL ORDER BY name ASC"
            )
        except Exception:
            return []

    def get_gov_baselines(self) -> list[dict]:
        """Return computed behavioral baselines."""
        try:
            return self._execute(
                "SELECT agent_role, metric, mean, stddev, sample_count, "
                "window_days, computed_at "
                "FROM otel.gov_agent_baselines FINAL "
                "ORDER BY metric ASC, agent_role ASC"
            )
        except Exception:
            return []

    def get_gov_safety_summary(self, conn=None):
        """Return safety event counts by type for the last 24 hours.

        Returns a pandas DataFrame with columns: event_type, cnt, detected_count.
        The ``conn`` parameter is accepted for compatibility but unused — this
        method uses the internal ClickHouse client directly.
        """
        import pandas as pd
        try:
            rows = self._execute(
                "SELECT event_type, "
                "count() AS cnt, "
                "countIf(detected = 1) AS detected_count "
                "FROM otel.gov_safety_events "
                "WHERE ts >= now() - INTERVAL 24 HOUR "
                "GROUP BY event_type"
            )
            if rows:
                return pd.DataFrame(rows)
            return pd.DataFrame(columns=["event_type", "cnt", "detected_count"])
        except Exception:
            return pd.DataFrame(columns=["event_type", "cnt", "detected_count"])


# Module-level singleton used by all pages
db = ClickHouseQueries()
