"""ClickHouse client for the EvalGov Agent service — findings CRUD + analytics queries."""

import os
import uuid

import structlog
from clickhouse_driver import Client

log = structlog.get_logger()

_CREATE_FINDINGS = """
CREATE TABLE IF NOT EXISTS otel.gov_agent_findings (
    finding_id      String,
    finding_type    String,
    severity        String,
    title           String,
    summary         String,
    rca             String  DEFAULT '',
    recommendation  String  DEFAULT '',
    signal_data     String  DEFAULT '{}',
    affected_agent  String  DEFAULT '',
    status          String  DEFAULT 'active',
    acknowledged_by String  DEFAULT '',
    created_at      DateTime DEFAULT now(),
    updated_at      DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (finding_id)
SETTINGS index_granularity = 8192
"""


class AgentDB:
    def __init__(self):
        self.client = Client(
            host=os.getenv("CLICKHOUSE_HOST", "localhost"),
            port=int(os.getenv("CLICKHOUSE_PORT", "9000")),
            database=os.getenv("CLICKHOUSE_DB", "otel"),
            user=os.getenv("CLICKHOUSE_USER", "default"),
            password=os.getenv("CLICKHOUSE_PASSWORD", ""),
            settings={"use_numpy": False},
        )

    def ensure_tables(self):
        self.client.execute(_CREATE_FINDINGS)
        log.info("findings_table_ready")

    def _run(self, sql: str, params: dict | None = None) -> list[dict]:
        rows, cols = self.client.execute(sql, params or {}, with_column_types=True)
        names = [c[0] for c in cols]
        return [dict(zip(names, r)) for r in rows]

    def _exec(self, sql: str, params: dict | None = None):
        self.client.execute(sql, params or {})

    # ── Findings CRUD ─────────────────────────────────────────────────────────

    def insert_finding(
        self,
        finding_type: str,
        severity: str,
        title: str,
        summary: str,
        rca: str = "",
        recommendation: str = "",
        signal_data: str = "{}",
        affected_agent: str = "",
    ) -> str:
        fid = str(uuid.uuid4())
        self._exec(
            "INSERT INTO otel.gov_agent_findings "
            "(finding_id, finding_type, severity, title, summary, rca, recommendation, "
            " signal_data, affected_agent, status, acknowledged_by, created_at, updated_at) "
            "VALUES (%(fid)s, %(ft)s, %(sev)s, %(title)s, %(summary)s, %(rca)s, %(rec)s, "
            "        %(sd)s, %(aa)s, 'active', '', now(), now())",
            {
                "fid": fid, "ft": finding_type, "sev": severity, "title": title,
                "summary": summary, "rca": rca, "rec": recommendation,
                "sd": signal_data, "aa": affected_agent,
            },
        )
        return fid

    def get_findings(
        self, hours: int = 24, severity: str = "", status: str = "active", limit: int = 50
    ) -> list[dict]:
        conds = [f"created_at >= now() - INTERVAL {int(hours)} HOUR"]
        params: dict = {"limit": limit}
        if severity:
            conds.append("severity = %(sev)s")
            params["sev"] = severity
        if status:
            conds.append("status = %(status)s")
            params["status"] = status
        where = " AND ".join(conds)
        return self._run(
            f"SELECT finding_id, finding_type, severity, title, summary, rca, "
            f"recommendation, affected_agent, status, acknowledged_by, created_at "
            f"FROM otel.gov_agent_findings FINAL WHERE {where} "
            f"ORDER BY created_at DESC LIMIT %(limit)s",
            params,
        )

    def get_all_active_findings(self, hours: int = 24) -> list[dict]:
        return self._run(
            f"SELECT finding_id, finding_type, severity, title, summary, rca, "
            f"recommendation, affected_agent, status, created_at "
            f"FROM otel.gov_agent_findings FINAL "
            f"WHERE status IN ('active') "
            f"AND created_at >= now() - INTERVAL {int(hours)} HOUR "
            f"ORDER BY created_at DESC LIMIT 100"
        )

    def acknowledge_finding(self, finding_id: str, acknowledged_by: str):
        rows = self._run(
            "SELECT * FROM otel.gov_agent_findings FINAL "
            "WHERE finding_id = %(fid)s LIMIT 1",
            {"fid": finding_id},
        )
        if not rows:
            return
        r = rows[0]
        s = lambda v: str(v or "").replace("'", "\\'")
        self._exec(
            f"INSERT INTO otel.gov_agent_findings "
            f"(finding_id, finding_type, severity, title, summary, rca, recommendation, "
            f" signal_data, affected_agent, status, acknowledged_by, created_at, updated_at) VALUES "
            f"('{s(r['finding_id'])}', '{s(r['finding_type'])}', '{s(r['severity'])}', "
            f"'{s(r['title'])}', '{s(r['summary'])}', '{s(r['rca'])}', '{s(r['recommendation'])}', "
            f"'{{}}', '{s(r['affected_agent'])}', 'acknowledged', "
            f"'{s(acknowledged_by)}', '{str(r['created_at'])[:19]}', now())"
        )

    def resolve_finding(self, finding_id: str):
        rows = self._run(
            "SELECT * FROM otel.gov_agent_findings FINAL WHERE finding_id = %(fid)s LIMIT 1",
            {"fid": finding_id},
        )
        if not rows:
            return
        r = rows[0]
        s = lambda v: str(v or "").replace("'", "\\'")
        self._exec(
            f"INSERT INTO otel.gov_agent_findings "
            f"(finding_id, finding_type, severity, title, summary, rca, recommendation, "
            f" signal_data, affected_agent, status, acknowledged_by, created_at, updated_at) VALUES "
            f"('{s(r['finding_id'])}', '{s(r['finding_type'])}', '{s(r['severity'])}', "
            f"'{s(r['title'])}', '{s(r['summary'])}', '{s(r['rca'])}', '{s(r['recommendation'])}', "
            f"'{{}}', '{s(r['affected_agent'])}', 'resolved', "
            f"'{s(r['acknowledged_by'])}', '{str(r['created_at'])[:19]}', now())"
        )

    def bulk_acknowledge_quality_gate_findings(self) -> int:
        """Acknowledge all active quality gate findings.

        Uses acknowledge (not resolve) so finding_exists_recently continues to
        suppress re-creation on the next monitor cycle.
        Returns count acknowledged.
        """
        rows = self._run(
            "SELECT finding_id, finding_type, severity, title, summary, rca, recommendation, "
            "affected_agent, acknowledged_by, created_at "
            "FROM otel.gov_agent_findings FINAL "
            "WHERE finding_type IN ('quality_gate_hold_pending', 'quality_gate_block_pending') "
            "  AND status = 'active'",
        )
        s = lambda v: str(v or "").replace("'", "\\'").replace("%", "%%")
        for r in rows:
            self._exec(
                f"INSERT INTO otel.gov_agent_findings "
                f"(finding_id, finding_type, severity, title, summary, rca, recommendation, "
                f" signal_data, affected_agent, status, acknowledged_by, created_at, updated_at) VALUES "
                f"('{s(r['finding_id'])}', '{s(r['finding_type'])}', '{s(r['severity'])}', "
                f"'{s(r['title'])}', '{s(r['summary'])}', '{s(r['rca'])}', '{s(r['recommendation'])}', "
                f"'{{}}', '{s(r['affected_agent'])}', 'acknowledged', "
                f"'operator', '{str(r['created_at'])[:19]}', now())"
            )
        return len(rows)

    def finding_exists_recently(
        self, finding_type: str, affected_agent: str, minutes: int = 30
    ) -> bool:
        # State-based dedup: suppress re-raise if ANY active/acknowledged finding
        # for this condition already exists, regardless of age.  The time-based
        # check (minutes) caused the monitor to re-create the same finding every
        # 30 min for persistent conditions (open circuit breaker, low trust score,
        # open incident), flooding the Live Findings panel with duplicate rows.
        # A new finding is only created when the previous one is resolved.
        rows = self._run(
            "SELECT count() AS cnt FROM otel.gov_agent_findings FINAL "
            "WHERE finding_type = %(ft)s AND affected_agent = %(aa)s "
            "AND status IN ('active', 'acknowledged')",
            {"ft": finding_type, "aa": affected_agent},
        )
        return (rows[0]["cnt"] if rows else 0) > 0

    # ── Analytics queries (not exposed via governance_service API) ─────────────

    def get_recent_traces(
        self, agent_role: str = "", hours: int = 720, limit: int = 20
    ) -> list[dict]:
        """Return recent agent.task spans with agent attribution. Defaults to 30 days."""
        h = int(hours)
        params: dict = {"limit": limit}
        role_filter = ""
        if agent_role:
            role_filter = "AND SpanAttributes['agent.role'] = %(role)s"
            params["role"] = agent_role
        rows = self._run(
            f"SELECT TraceId, SpanId, SpanName, ServiceName, "
            f"round(Duration / 1e9, 3) AS duration_s, StatusCode, "
            f"SpanAttributes['agent.role'] AS agent_role, "
            f"SpanAttributes['task.status'] AS task_status, "
            f"Timestamp "
            f"FROM otel.otel_traces "
            f"WHERE SpanName = 'agent.task' "
            f"AND SpanAttributes['agent.role'] != '' "
            f"AND Timestamp >= now() - INTERVAL {h} HOUR "
            f"{role_filter} "
            f"ORDER BY Timestamp DESC LIMIT %(limit)s",
            params,
        )
        # Auto-widen: if no results in given window, try 30 days
        if not rows and hours < 720:
            return self.get_recent_traces(agent_role=agent_role, hours=720, limit=limit)
        return rows

    def get_eval_scores(self, hours: int = 720) -> list[dict]:
        return self._run(
            f"SELECT metric, round(avg(score), 3) AS avg_score, "
            f"round(min(score), 3) AS min_score, round(max(score), 3) AS max_score, "
            f"count() AS sample_count "
            f"FROM otel.eval_scores "
            f"WHERE evaluated_at >= now() - INTERVAL {int(hours)} HOUR "
            f"GROUP BY metric ORDER BY metric"
        )

    def _get_token_rates(self) -> tuple[float, float]:
        """Return (input_rate_per_token, output_rate_per_token) from gov_threshold_config."""
        try:
            rates = {
                r["config_key"]: float(r["value"])
                for r in self._run(
                    "SELECT config_key, value FROM otel.gov_threshold_config FINAL "
                    "WHERE config_key IN ('budget.input_token_cost_per_1m', "
                    "                     'budget.output_token_cost_per_1m')"
                )
            }
        except Exception:
            rates = {}
        return (
            rates.get("budget.input_token_cost_per_1m", 0.15) / 1_000_000,
            rates.get("budget.output_token_cost_per_1m", 0.60) / 1_000_000,
        )

    def get_cost_breakdown(self, hours: int = 720, source: str = "") -> list[dict]:
        """
        Cost per agent from otel.prompt_evals — same source as Prompt Lab.
        source: filter to 'production', 'benchmark', 'exploratory', or '' for all.
        Source requires JOIN with otel_traces (SpanAttributes['trace.source']) since
        prompt_evals has no source column.
        Uses configured token rates from gov_threshold_config (not hardcoded).
        Falls back to 30-day window if shorter window returns nothing.
        """
        h = int(hours)
        input_rate, output_rate = self._get_token_rates()

        if source:
            rows = self._run(
                f"SELECT "
                f"  pe.agent_name AS agent_role, "
                f"  count()    AS trace_count, "
                f"  sum(pe.prompt_tokens)     AS total_input_tokens, "
                f"  sum(pe.completion_tokens) AS total_output_tokens, "
                f"  round(sum(pe.prompt_tokens) * {input_rate} + sum(pe.completion_tokens) * {output_rate}, 6) AS total_cost_usd "
                f"FROM otel.prompt_evals pe "
                f"INNER JOIN ( "
                f"  SELECT DISTINCT TraceId "
                f"  FROM otel.otel_traces "
                f"  WHERE SpanAttributes['trace.source'] = %(src)s "
                f"  AND Timestamp >= now() - INTERVAL {h} HOUR "
                f") t ON pe.trace_id = t.TraceId "
                f"WHERE pe.created_at >= now() - INTERVAL {h} HOUR "
                f"  AND pe.agent_name != '' "
                f"GROUP BY pe.agent_name "
                f"ORDER BY total_cost_usd DESC",
                {"src": source},
            )
        else:
            rows = self._run(
                f"SELECT "
                f"  agent_name AS agent_role, "
                f"  count()    AS trace_count, "
                f"  sum(prompt_tokens)     AS total_input_tokens, "
                f"  sum(completion_tokens) AS total_output_tokens, "
                f"  round(sum(prompt_tokens) * {input_rate} + sum(completion_tokens) * {output_rate}, 6) AS total_cost_usd "
                f"FROM otel.prompt_evals "
                f"WHERE created_at >= now() - INTERVAL {h} HOUR "
                f"  AND agent_name != '' "
                f"GROUP BY agent_name "
                f"ORDER BY total_cost_usd DESC"
            )

        if not rows and hours < 720:
            return self.get_cost_breakdown(hours=720, source=source)
        return rows

    # ── Prompt / eval content queries ─────────────────────────────────────────

    def _enrich_conversation_ids(self, rows: list[dict]) -> None:
        """Add conversation_id to each row by joining otel_traces on TraceId (in-place)."""
        trace_ids = list({r["trace_id"] for r in rows if r.get("trace_id")})
        if not trace_ids:
            return
        id_list = ", ".join(f"'{tid}'" for tid in trace_ids)
        try:
            tr = self._run(
                f"SELECT DISTINCT TraceId, "
                f"SpanAttributes['conversation.id'] AS conversation_id "
                f"FROM otel.otel_traces "
                f"WHERE TraceId IN ({id_list}) AND SpanName = 'agent.task'"
            )
            conv_map = {r["TraceId"]: r.get("conversation_id", "") for r in tr}
        except Exception:
            conv_map = {}
        for r in rows:
            r["conversation_id"] = conv_map.get(r.get("trace_id", ""), "")

    def get_prompt_detail(self, trace_id: str) -> list[dict]:
        """Return full prompt_text + response_text + scores for a trace_id.
        cost_usd is computed from token counts × governance threshold rates (no stored column).
        """
        input_rate, output_rate = self._get_token_rates()
        rows = self._run(
            "SELECT prompt_eval_id, trace_id, run_id, agent_name, model, "
            "prompt_text, response_text, prompt_tokens, completion_tokens, "
            f"round(prompt_tokens * {input_rate} + completion_tokens * {output_rate}, 6) AS cost_usd, "
            "scores, latency_ms, created_at "
            "FROM otel.prompt_evals "
            "WHERE trace_id = %(tid)s "
            "ORDER BY created_at DESC LIMIT 10",
            {"tid": trace_id},
        )
        # Enrich with trace.source and conversation_id from otel_traces
        if rows:
            trace_rows = self._run(
                "SELECT TraceId, "
                "SpanAttributes['trace.source'] AS trace_source, "
                "SpanAttributes['conversation.id'] AS conversation_id "
                "FROM otel.otel_traces "
                "WHERE TraceId = %(tid)s AND SpanName = 'agent.task' LIMIT 1",
                {"tid": trace_id},
            )
            src     = trace_rows[0]["trace_source"]     if trace_rows else ""
            conv_id = trace_rows[0]["conversation_id"]  if trace_rows else ""
            for r in rows:
                r["source"]          = src
                r["conversation_id"] = conv_id
        return rows

    def search_prompts(
        self,
        agent_name: str = "",
        source: str = "",
        conversation_id: str = "",
        hours: int = 720,
        limit: int = 20,
    ) -> list[dict]:
        """Return recent prompt/response pairs from otel.prompt_evals.
        Filters: agent_name, source (joins otel_traces), conversation_id (joins otel_traces).
        Results are always enriched with conversation_id.
        """
        h = int(hours)
        params: dict = {"limit": min(int(limit), 50)}

        # Build otel_traces subquery conditions when we need a join
        needs_join = bool(source or conversation_id)
        join_conds = []
        if source:
            join_conds.append(f"SpanAttributes['trace.source'] = %(src)s")
            params["src"] = source
        if conversation_id:
            join_conds.append(f"SpanAttributes['conversation.id'] = %(conv)s")
            params["conv"] = conversation_id

        agent_filter = "AND pe.agent_name = %(agent)s" if agent_name else ""
        if agent_name:
            params["agent"] = agent_name

        if needs_join:
            join_where = " AND ".join(join_conds)
            rows = self._run(
                f"SELECT pe.prompt_eval_id, pe.trace_id, pe.run_id, pe.agent_name, pe.model, "
                f"pe.prompt_text, pe.response_text, pe.prompt_tokens, pe.completion_tokens, "
                f"pe.scores, pe.latency_ms, pe.created_at "
                f"FROM otel.prompt_evals pe "
                f"INNER JOIN ( "
                f"  SELECT DISTINCT TraceId "
                f"  FROM otel.otel_traces "
                f"  WHERE {join_where} "
                f"  AND Timestamp >= now() - INTERVAL {h} HOUR "
                f") t ON pe.trace_id = t.TraceId "
                f"WHERE pe.created_at >= now() - INTERVAL {h} HOUR "
                f"  AND pe.agent_name != '' "
                f"  {agent_filter} "
                f"ORDER BY pe.created_at DESC LIMIT %(limit)s",
                params,
            )
        else:
            conds = [f"created_at >= now() - INTERVAL {h} HOUR", "agent_name != ''"]
            if agent_name:
                conds.append("agent_name = %(agent)s")
            where = " AND ".join(conds)
            rows = self._run(
                f"SELECT prompt_eval_id, trace_id, run_id, agent_name, model, "
                f"prompt_text, response_text, prompt_tokens, completion_tokens, "
                f"scores, latency_ms, created_at "
                f"FROM otel.prompt_evals "
                f"WHERE {where} "
                f"ORDER BY created_at DESC LIMIT %(limit)s",
                params,
            )

        if not rows and hours < 720:
            return self.search_prompts(
                agent_name=agent_name, source=source,
                conversation_id=conversation_id, hours=720, limit=limit,
            )

        # Enrich all rows with conversation_id and source from otel_traces
        self._enrich_conversation_ids(rows)
        return rows

    def get_eval_runs(self, suite: str = "", hours: int = 720, limit: int = 20) -> list[dict]:
        """Return eval run metadata from otel.eval_runs."""
        h = int(hours)
        conds = [f"created_at >= now() - INTERVAL {h} HOUR"]
        params: dict = {"limit": min(int(limit), 100)}
        if suite:
            conds.append("suite = %(suite)s")
            params["suite"] = suite
        where = " AND ".join(conds)
        rows = self._run(
            f"SELECT toString(run_id) AS run_id, name, suite, agent_version, is_baseline, created_at "
            f"FROM otel.eval_runs "
            f"WHERE {where} "
            f"ORDER BY created_at DESC LIMIT %(limit)s",
            params,
        )
        if not rows and hours < 720:
            return self.get_eval_runs(suite=suite, hours=720, limit=limit)
        return rows

    def get_benchmarks(self, suite: str = "", difficulty: str = "", limit: int = 50) -> list[dict]:
        """Return benchmark test case definitions."""
        conds = ["active = 1"]
        params: dict = {"limit": min(int(limit), 200)}
        if suite:
            conds.append("suite = %(suite)s")
            params["suite"] = suite
        if difficulty:
            conds.append("difficulty = %(diff)s")
            params["diff"] = difficulty
        where = " AND ".join(conds)
        return self._run(
            f"SELECT toString(benchmark_id) AS benchmark_id, suite, name, task_input, expected_output, "
            f"rubric, difficulty, dataset_version, created_at "
            f"FROM otel.benchmarks "
            f"WHERE {where} "
            f"ORDER BY created_at DESC LIMIT %(limit)s",
            params,
        )

    def get_eval_scores_detail(
        self, trace_id: str = "", run_id: str = "", metric: str = "", limit: int = 20
    ) -> list[dict]:
        """Return eval scores with reasoning text for a specific trace or run.
        run_id in eval_scores is UUID type — cast string param with toUUID().
        """
        conds = []
        params: dict = {"limit": min(int(limit), 100)}
        if trace_id:
            conds.append("trace_id = %(tid)s")
            params["tid"] = trace_id
        if run_id:
            conds.append("run_id = toUUID(%(rid)s)")
            params["rid"] = run_id
        if metric:
            conds.append("metric = %(metric)s")
            params["metric"] = metric
        where = ("WHERE " + " AND ".join(conds)) if conds else "WHERE evaluated_at >= now() - INTERVAL 720 HOUR"
        return self._run(
            f"SELECT toString(id) AS id, trace_id, toString(run_id) AS run_id, "
            f"metric, score, reasoning, evaluator, eval_type, evaluated_at "
            f"FROM otel.eval_scores "
            f"{where} "
            f"ORDER BY evaluated_at DESC LIMIT %(limit)s",
            params,
        )

    def get_policy_decisions(
        self, hours: int = 24, agent_role: str = "", decision: str = "", limit: int = 50
    ) -> list[dict]:
        """Return policy engine verdicts from gov_policy_decisions."""
        h = int(hours)
        conds = [f"ts >= now() - INTERVAL {h} HOUR"]
        params: dict = {"limit": min(int(limit), 200)}
        if decision:
            conds.append("decision = %(dec)s")
            params["dec"] = decision
        where = " AND ".join(conds)
        return self._run(
            f"SELECT decision_id, trace_id, run_id, metric, decision, "
            f"value, threshold, message, ts "
            f"FROM otel.gov_policy_decisions "
            f"WHERE {where} "
            f"ORDER BY ts DESC LIMIT %(limit)s",
            params,
        )

    def get_routing_decisions(
        self, hours: int = 168, agent_role: str = "", limit: int = 50
    ) -> list[dict]:
        """Return model routing decisions from gov_routing_decisions."""
        h = int(hours)
        conds = [f"ts >= now() - INTERVAL {h} HOUR"]
        params: dict = {"limit": min(int(limit), 200)}
        if agent_role:
            conds.append("agent_role = %(role)s")
            params["role"] = agent_role
        where = " AND ".join(conds)
        return self._run(
            f"SELECT decision_id, trace_id, agent_role, complexity_tier, model, "
            f"input_chars, estimated_savings_pct, ts "
            f"FROM otel.gov_routing_decisions "
            f"WHERE {where} "
            f"ORDER BY ts DESC LIMIT %(limit)s",
            params,
        )

    # ── Live system-state queries (for redesigned findings panel) ─────────────

    def get_hitl_queue_live(self, hours: int = 24, limit: int = 25) -> list[dict]:
        """Return pending HITL requests from the last N hours."""
        h = int(hours)
        return self._run(
            f"SELECT request_id, trace_id, run_id, risk_tier, action_type, "
            f"payload, created_at "
            f"FROM otel.gov_hitl_queue FINAL "
            f"WHERE status = 'pending' "
            f"AND created_at >= now() - INTERVAL {h} HOUR "
            f"ORDER BY created_at DESC LIMIT {int(limit)}"
        )

    def get_incidents_live(self, hours: int = 24, limit: int = 25) -> list[dict]:
        """Return open incidents from the last N hours."""
        h = int(hours)
        return self._run(
            f"SELECT incident_id, agent_role, incident_type, severity, status, "
            f"root_cause, detail, opened_at "
            f"FROM otel.gov_incidents "
            f"WHERE status = 'open' "
            f"AND opened_at >= now() - INTERVAL {h} HOUR "
            f"ORDER BY opened_at DESC LIMIT {int(limit)}"
        )

    def get_circuit_breakers_live(self) -> list[dict]:
        """Return open/half-open circuit breakers (current state, no time filter)."""
        return self._run(
            "SELECT agent_role, state, failure_count, failure_threshold, "
            "opened_at, quarantine_reason, updated_at "
            "FROM otel.gov_circuit_breakers FINAL "
            "WHERE lower(state) IN ('open', 'half_open') "
            "ORDER BY updated_at DESC"
        )

    def get_error_rate(self, hours: int = 168) -> list[dict]:
        """Error rate per agent on agent.task spans. Defaults to 7 days."""
        h = int(hours)
        rows = self._run(
            f"SELECT SpanAttributes['agent.role'] AS agent_role, "
            f"countIf(StatusCode = 'STATUS_CODE_ERROR') AS errors, "
            f"count() AS total, "
            f"round(countIf(StatusCode = 'STATUS_CODE_ERROR') / count(), 3) AS error_rate "
            f"FROM otel.otel_traces "
            f"WHERE SpanName = 'agent.task' "
            f"AND SpanAttributes['agent.role'] != '' "
            f"AND Timestamp >= now() - INTERVAL {h} HOUR "
            f"GROUP BY agent_role ORDER BY error_rate DESC"
        )
        if not rows and hours < 720:
            return self.get_error_rate(hours=720)
        return rows
