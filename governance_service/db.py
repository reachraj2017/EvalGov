"""GovernanceDB — ClickHouse client for the governance service.

Wraps clickhouse-driver with the same retry pattern used by eval_runner,
plus all domain-specific read/write helpers needed by gate.py,
budget_tracker.py, drift_detector.py, runner.py, and watcher.py.
"""

import os
import uuid
from datetime import datetime
from typing import Any, Optional

import structlog
from clickhouse_driver import Client

log = structlog.get_logger(__name__)


class GovernanceDB:
    """ClickHouse client for the governance service."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        database: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        self.host     = host     or os.getenv("CLICKHOUSE_HOST",     "localhost")
        self.port     = port     or int(os.getenv("CLICKHOUSE_PORT", "9000"))
        self.database = database or os.getenv("CLICKHOUSE_DB",       "otel")
        self.user     = user     or os.getenv("CLICKHOUSE_USER",     "default")
        self.password = password or os.getenv("CLICKHOUSE_PASSWORD", "")
        self._client: Optional[Client] = None

    # ──────────────────────────────────────────────────────────────────────
    # Connection helpers
    # ──────────────────────────────────────────────────────────────────────

    def _get_client(self) -> Client:
        if self._client is None:
            self._client = Client(
                host=self.host, port=self.port,
                database=self.database, user=self.user, password=self.password,
                settings={"use_numpy": False, "connect_timeout": 10,
                          "send_receive_timeout": 30},
            )
        return self._client

    def _reconnect(self) -> Client:
        self._client = None
        return self._get_client()

    # ──────────────────────────────────────────────────────────────────────
    # Schema bootstrap
    # ──────────────────────────────────────────────────────────────────────

    def _migrate_hitl_table(self) -> None:
        """Drop gov_hitl_queue if it's still the old MergeTree schema so it gets recreated as ReplacingMergeTree."""
        try:
            row = self.fetch_one(
                "SELECT engine FROM system.tables WHERE database='otel' AND name='gov_hitl_queue'",
                {},
            )
            if row and row.get("engine") == "MergeTree":
                log.info("hitl_table_migration", action="drop_old_mergetree")
                self.execute("DROP TABLE IF EXISTS otel.gov_hitl_queue")
        except Exception as exc:
            log.warning("hitl_table_migration_failed", error=str(exc))

    def ensure_tables(self) -> None:
        """Create governance tables if they don't exist (idempotent)."""
        ddl_statements = [
            """CREATE TABLE IF NOT EXISTS otel.gov_metric_snapshots (
                trace_id    String, span_id String DEFAULT '', run_id String DEFAULT '',
                metric String, value Float32, detail String DEFAULT '',
                ts DateTime DEFAULT now()
            ) ENGINE = MergeTree() ORDER BY (trace_id, metric, ts)
            SETTINGS index_granularity = 8192""",

            """CREATE TABLE IF NOT EXISTS otel.gov_policy_decisions (
                decision_id String DEFAULT generateUUIDv4(),
                trace_id String DEFAULT '', run_id String DEFAULT '',
                metric LowCardinality(String), decision LowCardinality(String),
                value Float32, threshold Float32, message String DEFAULT '',
                ts DateTime DEFAULT now()
            ) ENGINE = MergeTree() ORDER BY (run_id, trace_id, metric, ts)
            SETTINGS index_granularity = 8192""",

            """CREATE TABLE IF NOT EXISTS otel.gov_audit_log (
                event_id String DEFAULT generateUUIDv4(),
                trace_id String DEFAULT '', span_id String DEFAULT '',
                run_id String DEFAULT '', event_type String,
                detail String DEFAULT '', ts DateTime DEFAULT now()
            ) ENGINE = MergeTree() ORDER BY (trace_id, event_type, ts)
            SETTINGS index_granularity = 8192""",

            """CREATE TABLE IF NOT EXISTS otel.gov_hitl_queue (
                request_id String,
                trace_id String DEFAULT '', span_id String DEFAULT '',
                run_id String DEFAULT '',
                risk_tier LowCardinality(String) DEFAULT 'low',
                action_type String DEFAULT '', payload String DEFAULT '',
                status LowCardinality(String) DEFAULT 'pending',
                reviewer String DEFAULT '', notes String DEFAULT '',
                created_at DateTime DEFAULT now(),
                decided_at DateTime DEFAULT '1970-01-01 00:00:00'
            ) ENGINE = ReplacingMergeTree(decided_at)
            ORDER BY request_id""",

            """CREATE TABLE IF NOT EXISTS otel.gov_token_budgets (
                agent_role String, daily_token_limit UInt64 DEFAULT 0,
                enabled UInt8 DEFAULT 1, cost_usd_limit Float32 DEFAULT 0.0,
                updated_at DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY agent_role""",

            """CREATE TABLE IF NOT EXISTS otel.gov_prompt_drift (
                template_id String, snapshot_hash String,
                is_baseline UInt8 DEFAULT 0, drift_detected UInt8 DEFAULT 0,
                first_seen DateTime DEFAULT now(), last_seen DateTime DEFAULT now(),
                seen_count UInt64 DEFAULT 1
            ) ENGINE = ReplacingMergeTree(last_seen)
            ORDER BY (template_id, snapshot_hash)""",

            """CREATE TABLE IF NOT EXISTS otel.gov_routing_decisions (
                decision_id String DEFAULT generateUUIDv4(),
                trace_id String DEFAULT '', run_id String DEFAULT '',
                agent_role String DEFAULT '',
                complexity_tier LowCardinality(String), model String,
                input_chars UInt32 DEFAULT 0,
                estimated_savings_pct Float32 DEFAULT 0.0,
                ts DateTime DEFAULT now()
            ) ENGINE = MergeTree() ORDER BY (trace_id, ts)
            SETTINGS index_granularity = 8192""",

            """CREATE TABLE IF NOT EXISTS otel.gov_model_routing_config (
                complexity_tier LowCardinality(String), model String,
                cost_per_1m_input Float32 DEFAULT 3.0,
                max_input_tokens UInt32 DEFAULT 16000,
                enabled UInt8 DEFAULT 1, updated_at DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY complexity_tier""",

            # ── Phase 4: Safety ──────────────────────────────────────────────
            """CREATE TABLE IF NOT EXISTS otel.gov_safety_events (
                event_id    String DEFAULT generateUUIDv4(),
                trace_id    String DEFAULT '', run_id String DEFAULT '',
                agent_role  String DEFAULT '', rule_type String DEFAULT '',
                pattern     String DEFAULT '', severity LowCardinality(String) DEFAULT 'low',
                matched_text String DEFAULT '', ts DateTime DEFAULT now()
            ) ENGINE = MergeTree() ORDER BY (trace_id, severity, ts)
            SETTINGS index_granularity = 8192""",

            """CREATE TABLE IF NOT EXISTS otel.gov_safety_rules (
                rule_id     String DEFAULT generateUUIDv4(),
                rule_type   String, pattern String,
                severity    LowCardinality(String) DEFAULT 'medium',
                description String DEFAULT '',
                enabled     UInt8 DEFAULT 1,
                created_at  DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(created_at) ORDER BY rule_id""",

            # ── Phase 4: Identity ─────────────────────────────────────────────
            """CREATE TABLE IF NOT EXISTS otel.gov_identity_events (
                event_id    String DEFAULT generateUUIDv4(),
                trace_id    String DEFAULT '', run_id String DEFAULT '',
                agent_role  String DEFAULT '',
                check_type  String DEFAULT '', passed UInt8 DEFAULT 1,
                detail      String DEFAULT '', ts DateTime DEFAULT now()
            ) ENGINE = MergeTree() ORDER BY (trace_id, check_type, ts)
            SETTINGS index_granularity = 8192""",

            """CREATE TABLE IF NOT EXISTS otel.gov_supply_chain_registry (
                artifact_id   String DEFAULT generateUUIDv4(),
                artifact_type LowCardinality(String),
                artifact_name String,
                expected_hash String DEFAULT '',
                hash_algo     LowCardinality(String) DEFAULT 'sha256',
                verified      UInt8 DEFAULT 1,
                updated_at    DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY (artifact_type, artifact_name)""",

            """CREATE TABLE IF NOT EXISTS otel.gov_tool_whitelist (
                entry_id    String DEFAULT generateUUIDv4(),
                agent_role  String, tool_name String,
                allowed     UInt8 DEFAULT 1,
                updated_at  DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY (agent_role, tool_name)""",

            # ── Phase 4: Reliability ──────────────────────────────────────────
            """CREATE TABLE IF NOT EXISTS otel.gov_reliability_metrics (
                metric_id   String DEFAULT generateUUIDv4(),
                trace_id    String DEFAULT '', run_id String DEFAULT '',
                agent_role  String DEFAULT '',
                success     UInt8 DEFAULT 1, latency_ms Float32 DEFAULT 0.0,
                error_type  String DEFAULT '', ts DateTime DEFAULT now()
            ) ENGINE = MergeTree() ORDER BY (agent_role, ts)
            SETTINGS index_granularity = 8192""",

            """CREATE TABLE IF NOT EXISTS otel.gov_slo_config (
                agent_role           String,
                target_availability  Float32 DEFAULT 0.999,
                error_budget_pct     Float32 DEFAULT 0.1,
                slo_window_days      UInt16 DEFAULT 30,
                p95_target_ms        Float32 DEFAULT 2000.0,
                p99_target_ms        Float32 DEFAULT 5000.0,
                updated_at           DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY agent_role""",

            """CREATE TABLE IF NOT EXISTS otel.gov_incidents (
                incident_id  String DEFAULT generateUUIDv4(),
                trace_id     String DEFAULT '', run_id String DEFAULT '',
                agent_role   String DEFAULT '', title String DEFAULT '',
                severity     LowCardinality(String) DEFAULT 'low',
                status       LowCardinality(String) DEFAULT 'open',
                root_cause   String DEFAULT '',
                detected_at  DateTime DEFAULT now(),
                contained_at DateTime DEFAULT '1970-01-01 00:00:00',
                resolved_at  DateTime DEFAULT '1970-01-01 00:00:00'
            ) ENGINE = MergeTree() ORDER BY (status, severity, detected_at)
            SETTINGS index_granularity = 8192""",

            # ── Phase 4: Behavior ─────────────────────────────────────────────
            """CREATE TABLE IF NOT EXISTS otel.gov_behavior_events (
                event_id    String DEFAULT generateUUIDv4(),
                trace_id    String DEFAULT '', run_id String DEFAULT '',
                agent_role  String DEFAULT '',
                check_type  String DEFAULT '', passed UInt8 DEFAULT 1,
                detail      String DEFAULT '', ts DateTime DEFAULT now()
            ) ENGINE = MergeTree() ORDER BY (trace_id, check_type, ts)
            SETTINGS index_granularity = 8192""",

            """CREATE TABLE IF NOT EXISTS otel.gov_persona_config (
                config_id           String DEFAULT generateUUIDv4(),
                agent_role          String,
                authorized_topics   String DEFAULT '[]',
                persona_description String DEFAULT '',
                updated_at          DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY agent_role""",

            # ── Phase 4: Lifecycle ────────────────────────────────────────────
            """CREATE TABLE IF NOT EXISTS otel.gov_change_log (
                change_id   String DEFAULT generateUUIDv4(),
                agent_role  String DEFAULT '', change_type String DEFAULT '',
                old_value   String DEFAULT '', new_value String DEFAULT '',
                changed_by  String DEFAULT '', ts DateTime DEFAULT now()
            ) ENGINE = MergeTree() ORDER BY (agent_role, change_type, ts)
            SETTINGS index_granularity = 8192""",

            """CREATE TABLE IF NOT EXISTS otel.gov_version_pins (
                pin_id          String DEFAULT generateUUIDv4(),
                agent_role      String, model_name String,
                pinned_version  String DEFAULT '',
                is_pinned       UInt8 DEFAULT 1,
                deployment_mode LowCardinality(String) DEFAULT 'production',
                updated_at      DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY (agent_role, model_name)""",

            # ── Phase 5: Threshold Configuration ─────────────────────────────
            """CREATE TABLE IF NOT EXISTS otel.gov_threshold_config (
                config_key   String,
                display_name String DEFAULT '',
                category     LowCardinality(String) DEFAULT '',
                value        Float64,
                unit         String DEFAULT '',
                description  String DEFAULT '',
                updated_at   DateTime DEFAULT now(),
                updated_by   String DEFAULT 'system'
            ) ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY config_key
            SETTINGS index_granularity = 8192""",

            # ── Phase 4: Regulatory ───────────────────────────────────────────
            """CREATE TABLE IF NOT EXISTS otel.gov_compliance_scorecards (
                scorecard_id     String DEFAULT generateUUIDv4(),
                framework        String DEFAULT '', agent_role String DEFAULT '',
                score            Float32 DEFAULT 0.0,
                controls_passing UInt32 DEFAULT 0,
                controls_failing UInt32 DEFAULT 0,
                generated_at     DateTime DEFAULT now()
            ) ENGINE = MergeTree() ORDER BY (framework, agent_role, generated_at)
            SETTINGS index_granularity = 8192""",

            """CREATE TABLE IF NOT EXISTS otel.gov_model_registry (
                entry_id        String DEFAULT generateUUIDv4(),
                model_name      String, model_version String,
                provider        String DEFAULT '',
                license_type    String DEFAULT '',
                commercial_ok   UInt8 DEFAULT 0, dpa_signed UInt8 DEFAULT 0,
                baa_signed      UInt8 DEFAULT 0,
                sectors_allowed String DEFAULT '[]',
                notes           String DEFAULT '',
                created_at      DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(created_at)
            ORDER BY (model_name, model_version)""",

            """CREATE TABLE IF NOT EXISTS otel.gov_regulatory_scope (
                scope_id       String DEFAULT generateUUIDv4(),
                agent_role     String, framework String,
                sector         String DEFAULT '',
                classification String DEFAULT '',
                notes          String DEFAULT '',
                created_at     DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(created_at)
            ORDER BY (agent_role, framework)""",

            """CREATE TABLE IF NOT EXISTS otel.gov_risk_register (
                risk_id    String DEFAULT generateUUIDv4(),
                title      String, category String DEFAULT '',
                likelihood UInt8 DEFAULT 1, impact UInt8 DEFAULT 1,
                owner      String DEFAULT '', mitigation String DEFAULT '',
                status     LowCardinality(String) DEFAULT 'open',
                created_at DateTime DEFAULT now(),
                updated_at DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY risk_id""",

            # ── Phase 5: Enforcement ──────────────────────────────────────────
            """CREATE TABLE IF NOT EXISTS otel.gov_trust_scores (
                score_id     String DEFAULT generateUUIDv4(),
                agent_role   String,
                trust_score  Float32 DEFAULT 500.0,
                identity_score    Float32 DEFAULT 500.0,
                behavior_score    Float32 DEFAULT 500.0,
                compliance_score  Float32 DEFAULT 500.0,
                network_score     Float32 DEFAULT 500.0,
                trust_tier   LowCardinality(String) DEFAULT 'medium',
                computed_at  DateTime DEFAULT now()
            ) ENGINE = MergeTree() ORDER BY (agent_role, computed_at)
            SETTINGS index_granularity = 8192""",

            """CREATE TABLE IF NOT EXISTS otel.gov_circuit_breakers (
                agent_role      String,
                state           LowCardinality(String) DEFAULT 'closed',
                failure_count   UInt32 DEFAULT 0,
                failure_threshold UInt32 DEFAULT 5,
                last_failure_at DateTime DEFAULT '1970-01-01 00:00:00',
                opened_at       DateTime DEFAULT '1970-01-01 00:00:00',
                reset_at        DateTime DEFAULT '1970-01-01 00:00:00',
                quarantine_reason String DEFAULT '',
                updated_at      DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY agent_role""",

            # ── Quality Gates ─────────────────────────────────────────────────
            """CREATE TABLE IF NOT EXISTS otel.gov_quality_gate_config (
                gate_id     String DEFAULT generateUUIDv4(),
                agent_role  String DEFAULT '*',
                metric      String,
                threshold   Float32,
                action      LowCardinality(String) DEFAULT 'flag',
                enabled     UInt8 DEFAULT 1,
                description String DEFAULT '',
                updated_at  DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY (agent_role, metric)""",

            """CREATE TABLE IF NOT EXISTS otel.gov_quality_gate_decisions (
                decision_id String DEFAULT generateUUIDv4(),
                trace_id    String DEFAULT '',
                run_id      String DEFAULT '',
                agent_role  String DEFAULT '',
                metric      String DEFAULT '',
                score       Float32 DEFAULT 0.0,
                threshold   Float32 DEFAULT 0.0,
                action      LowCardinality(String) DEFAULT 'flag',
                status      LowCardinality(String) DEFAULT 'pending',
                reviewer    String DEFAULT '',
                notes       String DEFAULT '',
                created_at  DateTime DEFAULT now()
            ) ENGINE = MergeTree()
            ORDER BY (agent_role, metric, created_at)
            SETTINGS index_granularity = 8192""",

            """CREATE TABLE IF NOT EXISTS otel.gov_rogue_assessments (
                assessment_id   String DEFAULT generateUUIDv4(),
                agent_role      String,
                composite_score Float32 DEFAULT 0.0,
                frequency_score Float32 DEFAULT 0.0,
                entropy_score   Float32 DEFAULT 0.0,
                capability_score Float32 DEFAULT 0.0,
                risk_level      LowCardinality(String) DEFAULT 'low',
                quarantine_recommended UInt8 DEFAULT 0,
                detail          String DEFAULT '',
                assessed_at     DateTime DEFAULT now()
            ) ENGINE = MergeTree() ORDER BY (agent_role, assessed_at)
            SETTINGS index_granularity = 8192""",
        ]
        self._migrate_hitl_table()
        for ddl in ddl_statements:
            try:
                self.execute(ddl)
            except Exception as exc:
                log.warning("ensure_tables_ddl_failed", error=str(exc)[:120])

    # ──────────────────────────────────────────────────────────────────────
    # Generic helpers (also called directly in gate.py)
    # ──────────────────────────────────────────────────────────────────────

    def execute(self, query: str, params: Any = None) -> Any:
        try:
            return self._get_client().execute(query, params or [])
        except Exception as exc:
            log.warning("gov_db_execute_error", error=str(exc), query=query[:100])
            try:
                return self._reconnect().execute(query, params or [])
            except Exception as retry_exc:
                log.error("gov_db_execute_retry_failed", error=str(retry_exc))
                raise

    def fetch_all(self, query: str, params: Any = None) -> list[dict]:
        try:
            client = self._get_client()
            rows, cols = client.execute(query, params or {}, with_column_types=True)
            col_names = [c[0] for c in cols]
            return [dict(zip(col_names, r)) for r in rows]
        except Exception as exc:
            log.error("gov_db_fetch_all_error", error=str(exc), query=query[:100])
            raise

    def fetch_one(self, query: str, params: Any = None) -> Optional[dict]:
        rows = self.fetch_all(query, params)
        return rows[0] if rows else None

    # ──────────────────────────────────────────────────────────────────────
    # Governance metric snapshots
    # ──────────────────────────────────────────────────────────────────────

    def save_gov_metric(
        self,
        trace_id: str,
        span_id: str,
        run_id: str,
        metric: str,
        value: float,
        detail: str = "",
    ) -> None:
        self.execute(
            "INSERT INTO otel.gov_metric_snapshots "
            "(trace_id, span_id, run_id, metric, value, detail) VALUES",
            [(trace_id, span_id, run_id, metric, float(value), detail)],
        )

    def get_gov_metrics(self, trace_id: str) -> list[dict]:
        return self.fetch_all(
            "SELECT trace_id, span_id, run_id, metric, value, detail, ts "
            "FROM otel.gov_metric_snapshots "
            "WHERE trace_id = %(tid)s ORDER BY ts DESC",
            {"tid": trace_id},
        )

    # ──────────────────────────────────────────────────────────────────────
    # Policy decisions
    # ──────────────────────────────────────────────────────────────────────

    def save_gov_policy_decision(
        self,
        trace_id: str,
        run_id: str,
        metric: str,
        decision: str,
        value: float,
        threshold: float,
        message: str = "",
    ) -> None:
        self.execute(
            "INSERT INTO otel.gov_policy_decisions "
            "(trace_id, run_id, metric, decision, value, threshold, message) VALUES",
            [(trace_id, run_id, metric, decision, float(value),
              float(threshold), message)],
        )

    def get_gov_policy_decisions(
        self,
        run_id: str = "",
        trace_id: str = "",
        limit: int = 200,
    ) -> list[dict]:
        conditions = []
        params: dict = {"limit": limit}
        if run_id:
            conditions.append("run_id = %(run_id)s")
            params["run_id"] = run_id
        if trace_id:
            conditions.append("trace_id = %(trace_id)s")
            params["trace_id"] = trace_id
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return self.fetch_all(
            f"SELECT decision_id, trace_id, run_id, metric, decision, "
            f"value, threshold, message, ts "
            f"FROM otel.gov_policy_decisions {where} "
            f"ORDER BY ts DESC LIMIT %(limit)s",
            params,
        )

    # ──────────────────────────────────────────────────────────────────────
    # Audit log
    # ──────────────────────────────────────────────────────────────────────

    def save_gov_audit_log(
        self,
        trace_id: str,
        span_id: str,
        run_id: str,
        event_type: str,
        detail: str = "",
    ) -> None:
        self.execute(
            "INSERT INTO otel.gov_audit_log "
            "(trace_id, span_id, run_id, event_type, detail) VALUES",
            [(trace_id, span_id, run_id, event_type, detail)],
        )

    def get_gov_audit_log(self, limit: int = 100) -> list[dict]:
        return self.fetch_all(
            "SELECT event_id, trace_id, span_id, run_id, event_type, detail, ts "
            "FROM otel.gov_audit_log ORDER BY ts DESC LIMIT %(limit)s",
            {"limit": limit},
        )

    # ──────────────────────────────────────────────────────────────────────
    # HITL queue
    # ──────────────────────────────────────────────────────────────────────

    def save_gov_hitl_request(
        self,
        trace_id: str,
        span_id: str,
        run_id: str,
        risk_tier: str,
        action_type: str,
        payload: str = "",
    ) -> str:
        request_id = str(uuid.uuid4())
        self.execute(
            "INSERT INTO otel.gov_hitl_queue "
            "(request_id, trace_id, span_id, run_id, risk_tier, action_type, payload) VALUES",
            [(request_id, trace_id, span_id, run_id, risk_tier, action_type, payload)],
        )
        return request_id

    def update_gov_hitl_decision(
        self,
        request_id: str,
        status: str,
        reviewer: str = "",
        notes: str = "",
    ) -> None:
        from datetime import datetime, timezone
        original = self.fetch_one(
            "SELECT trace_id, span_id, run_id, risk_tier, action_type, payload, created_at "
            "FROM otel.gov_hitl_queue FINAL WHERE request_id = %(rid)s",
            {"rid": request_id},
        ) or {}
        self.execute(
            "INSERT INTO otel.gov_hitl_queue "
            "(request_id, trace_id, span_id, run_id, risk_tier, action_type, "
            "payload, status, reviewer, notes, created_at, decided_at) VALUES",
            [(
                request_id,
                original.get("trace_id", ""),
                original.get("span_id", ""),
                original.get("run_id", ""),
                original.get("risk_tier", "low"),
                original.get("action_type", ""),
                original.get("payload", ""),
                status,
                reviewer,
                notes,
                original.get("created_at") or datetime.now(timezone.utc),
                datetime.now(timezone.utc),
            )],
        )

    def get_gov_hitl_queue(self, status: str = "", limit: int = 200, hours: int = 0) -> list[dict]:
        params: dict = {"limit": limit}
        conditions = []
        if status:
            conditions.append("status = %(status)s")
            params["status"] = status
        # hours filter applies only to decided rows; pending rows are always returned unfiltered
        if hours and status and status != "pending":
            conditions.append(f"created_at >= now() - INTERVAL {int(hours)} HOUR")
        elif hours and not status:
            # mixed query: return all pending + decided within window
            conditions.append(
                f"(status = 'pending' OR created_at >= now() - INTERVAL {int(hours)} HOUR)"
            )
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return self.fetch_all(
            f"SELECT request_id, trace_id, span_id, run_id, risk_tier, "
            f"action_type, payload, status, reviewer, notes, created_at, decided_at "
            f"FROM otel.gov_hitl_queue FINAL {where} "
            f"ORDER BY created_at DESC LIMIT %(limit)s",
            params,
        )

    # ──────────────────────────────────────────────────────────────────────
    # Token budgets (budget_tracker.py)
    # ──────────────────────────────────────────────────────────────────────

    def get_token_budget(self, agent_role: str) -> Optional[dict]:
        """Return the active budget config row for an agent (FINAL dedup)."""
        return self.fetch_one(
            "SELECT agent_role, daily_token_limit, enabled, cost_usd_limit "
            "FROM otel.gov_token_budgets FINAL "
            "WHERE agent_role = %(role)s",
            {"role": agent_role},
        )

    def get_token_usage_today(self, agent_role: str) -> dict:
        """Return today's aggregate token usage and cost from prompt_evals.
        cost_usd is computed from token counts × governance threshold rates (no stored column).
        """
        input_rate  = self.get_threshold("budget.input_token_cost_per_1m",  0.15) / 1_000_000
        output_rate = self.get_threshold("budget.output_token_cost_per_1m", 0.60) / 1_000_000
        row = self.fetch_one(
            "SELECT "
            "  sum(prompt_tokens + completion_tokens) AS total_tokens, "
            f" round(sum(prompt_tokens) * {input_rate} + sum(completion_tokens) * {output_rate}, 6) AS total_cost_usd "
            "FROM otel.prompt_evals "
            "WHERE agent_name = %(role)s "
            "  AND toDate(created_at) = today()",
            {"role": agent_role},
        )
        total = int(row.get("total_tokens", 0) or 0) if row else 0
        cost  = float(row.get("total_cost_usd", 0.0) or 0.0) if row else 0.0
        return {"total_tokens": total, "total_cost_usd": cost}

    def get_agents_with_usage_today(self) -> list[str]:
        """Return distinct agent names that have prompt_evals rows today."""
        rows = self.fetch_all(
            "SELECT DISTINCT agent_name FROM otel.prompt_evals "
            "WHERE toDate(created_at) = today() AND agent_name != ''"
        )
        return [r["agent_name"] for r in rows]

    def upsert_token_budget(
        self,
        agent_role: str,
        daily_token_limit: int,
        enabled: int = 1,
        cost_usd_limit: float = 0.0,
    ) -> None:
        self.execute(
            "INSERT INTO otel.gov_token_budgets "
            "(agent_role, daily_token_limit, enabled, cost_usd_limit) VALUES",
            [(agent_role, daily_token_limit, enabled, cost_usd_limit)],
        )

    # ──────────────────────────────────────────────────────────────────────
    # Prompt drift (drift_detector.py)
    # ──────────────────────────────────────────────────────────────────────

    def get_prompt_drift_history(self, template_id: str) -> list[dict]:
        return self.fetch_all(
            "SELECT template_id, snapshot_hash, is_baseline, drift_detected, "
            "first_seen, last_seen, seen_count "
            "FROM otel.gov_prompt_drift FINAL "
            "WHERE template_id = %(tid)s ORDER BY first_seen ASC",
            {"tid": template_id},
        )

    def save_prompt_drift(
        self,
        template_id: str,
        snapshot_hash: str,
        is_baseline: int,
        drift_detected: int,
    ) -> None:
        self.execute(
            "INSERT INTO otel.gov_prompt_drift "
            "(template_id, snapshot_hash, is_baseline, drift_detected) VALUES",
            [(template_id, snapshot_hash, is_baseline, drift_detected)],
        )

    def update_prompt_drift_seen(self, template_id: str, snapshot_hash: str) -> None:
        """Bump last_seen and seen_count for an existing (template, hash) row."""
        self.execute(
            "ALTER TABLE otel.gov_prompt_drift UPDATE "
            "last_seen = now(), seen_count = seen_count + 1 "
            "WHERE template_id = %(tid)s AND snapshot_hash = %(hash)s",
            {"tid": template_id, "hash": snapshot_hash},
        )

    def set_prompt_drift_baseline(self, template_id: str, snapshot_hash: str) -> None:
        """Promote snapshot_hash to baseline; demote all others for this template."""
        # Demote all
        self.execute(
            "ALTER TABLE otel.gov_prompt_drift UPDATE is_baseline = 0 "
            "WHERE template_id = %(tid)s",
            {"tid": template_id},
        )
        # Promote new baseline
        self.execute(
            "ALTER TABLE otel.gov_prompt_drift UPDATE is_baseline = 1 "
            "WHERE template_id = %(tid)s AND snapshot_hash = %(hash)s",
            {"tid": template_id, "hash": snapshot_hash},
        )

    def get_all_drift_events(self, hours: int = 48) -> list[dict]:
        return self.fetch_all(
            f"SELECT template_id, snapshot_hash, is_baseline, drift_detected, "
            f"first_seen, last_seen, seen_count "
            f"FROM otel.gov_prompt_drift FINAL "
            f"WHERE last_seen >= now() - INTERVAL {int(hours)} HOUR "
            f"ORDER BY last_seen DESC"
        )

    # ──────────────────────────────────────────────────────────────────────
    # Model routing (model_router.py / runner.py)
    # ──────────────────────────────────────────────────────────────────────

    def get_routing_config(self) -> dict[str, dict]:
        """Return routing config from ClickHouse; falls back to empty dict."""
        try:
            rows = self.fetch_all(
                "SELECT complexity_tier, model, cost_per_1m_input, max_input_tokens "
                "FROM otel.gov_model_routing_config FINAL "
                "WHERE enabled = 1 ORDER BY complexity_tier"
            )
            return {
                r["complexity_tier"]: {
                    "model":              r["model"],
                    "cost_per_1m_input":  float(r["cost_per_1m_input"]),
                    "max_input_tokens":   int(r["max_input_tokens"]),
                }
                for r in rows
            } if rows else {}
        except Exception:
            return {}

    def save_routing_decision(
        self,
        trace_id: str,
        run_id: str,
        agent_role: str,
        complexity_tier: str,
        model: str,
        input_chars: int,
        estimated_savings_pct: float,
    ) -> None:
        self.execute(
            "INSERT INTO otel.gov_routing_decisions "
            "(trace_id, run_id, agent_role, complexity_tier, model, "
            "input_chars, estimated_savings_pct) VALUES",
            [(trace_id, run_id, agent_role, complexity_tier, model,
              int(input_chars), float(estimated_savings_pct))],
        )

    def get_routing_decisions(self, hours: int = 24, limit: int = 500) -> list[dict]:
        return self.fetch_all(
            f"SELECT decision_id, trace_id, run_id, agent_role, complexity_tier, "
            f"model, input_chars, estimated_savings_pct, ts "
            f"FROM otel.gov_routing_decisions "
            f"WHERE ts >= now() - INTERVAL {int(hours)} HOUR "
            f"ORDER BY ts DESC LIMIT %(limit)s",
            {"limit": limit},
        )

    def seed_routing_config(self, config: dict[str, dict]) -> None:
        """Insert default routing rows if the config table is empty."""
        existing = self.fetch_one(
            "SELECT count() AS cnt FROM otel.gov_model_routing_config FINAL"
        )
        if existing and int(existing.get("cnt", 0) or 0) > 0:
            return
        rows = [
            (tier, cfg["model"], float(cfg["cost_per_1m_input"]),
             int(cfg["max_input_tokens"]), 1)
            for tier, cfg in config.items()
        ]
        if rows:
            self.execute(
                "INSERT INTO otel.gov_model_routing_config "
                "(complexity_tier, model, cost_per_1m_input, max_input_tokens, enabled) VALUES",
                rows,
            )

    # ──────────────────────────────────────────────────────────────────────
    # Span / trace helpers (runner.py, watcher.py)
    # ──────────────────────────────────────────────────────────────────────

    def get_spans_for_trace(self, trace_id: str) -> list[dict]:
        """Return all spans for a trace from otel_traces."""
        return self.fetch_all(
            "SELECT TraceId AS trace_id, SpanId AS span_id, "
            "ParentSpanId AS parent_span_id, "
            "SpanName AS span_name, SpanAttributes AS attributes, "
            "StatusCode AS status_code, Duration AS duration "
            "FROM otel.otel_traces "
            "WHERE TraceId = %(tid)s ORDER BY Timestamp ASC",
            {"tid": trace_id},
        )

    def get_unchecked_traces(
        self,
        since_ts: datetime,
        limit: int = 100,
    ) -> list[dict]:
        """
        Return trace_ids that have agent.task spans but have NOT yet been
        governance-evaluated (no governance_eval row in gov_audit_log).
        """
        return self.fetch_all(
            "SELECT DISTINCT TraceId AS trace_id, min(Timestamp) AS first_seen "
            "FROM otel.otel_traces "
            "WHERE SpanName = 'agent.task' "
            "  AND Timestamp >= %(since)s "
            "  AND TraceId NOT IN ("
            "    SELECT DISTINCT trace_id FROM otel.gov_audit_log "
            "    WHERE event_type = 'governance_eval'"
            "  ) "
            "GROUP BY TraceId "
            "ORDER BY first_seen ASC "
            "LIMIT %(limit)s",
            {"since": since_ts, "limit": limit},
        )

    def get_hitl_request_status(self, request_id: str) -> Optional[dict]:
        """Return current status of a HITL queue entry (for agent polling)."""
        return self.fetch_one(
            "SELECT request_id, status, reviewer, notes, decided_at "
            "FROM otel.gov_hitl_queue FINAL "
            "WHERE request_id = %(rid)s",
            {"rid": request_id},
        )

    # ──────────────────────────────────────────────────────────────────────
    # Phase 3: policies, agent keys, webhooks, anomalies, compliance
    # ──────────────────────────────────────────────────────────────────────

    def get_anomaly_events(self, hours: int = 24, limit: int = 200) -> list[dict]:
        return self.fetch_all(
            f"SELECT anomaly_id, trace_id, run_id, agent_role, metric, "
            f"observed_value, baseline_mean, baseline_stddev, z_score, severity, ts "
            f"FROM otel.gov_anomaly_events "
            f"WHERE ts >= now() - INTERVAL {int(hours)} HOUR "
            f"ORDER BY ts DESC LIMIT %(limit)s",
            {"limit": limit},
        )

    def get_remediation_log(self, hours: int = 24, limit: int = 200) -> list[dict]:
        return self.fetch_all(
            f"SELECT action_id, trace_id, run_id, trigger, action_type, detail, ts "
            f"FROM otel.gov_remediation_log "
            f"WHERE ts >= now() - INTERVAL {int(hours)} HOUR "
            f"ORDER BY ts DESC LIMIT %(limit)s",
            {"limit": limit},
        )

    def get_compliance_reports(self, limit: int = 20) -> list[dict]:
        return self.fetch_all(
            "SELECT report_id, report_type, from_ts, to_ts, "
            "generated_by, created_at "
            "FROM otel.gov_compliance_reports "
            "ORDER BY created_at DESC LIMIT %(limit)s",
            {"limit": limit},
        )

    def get_compliance_report(self, report_id: str) -> Optional[dict]:
        return self.fetch_one(
            "SELECT report_id, report_type, from_ts, to_ts, "
            "generated_by, payload, created_at "
            "FROM otel.gov_compliance_reports "
            "WHERE report_id = %(rid)s LIMIT 1",
            {"rid": report_id},
        )

    # Agent keys
    def list_agent_keys(self) -> list[dict]:
        return self.fetch_all(
            "SELECT key_id, agent_role, key_prefix, enabled, created_at, last_used_at "
            "FROM otel.gov_agent_keys FINAL "
            "WHERE revoked_at = '1970-01-01 00:00:00' "
            "ORDER BY created_at DESC"
        )

    def save_agent_key(
        self,
        key_id: str,
        agent_role: str,
        key_hash: str,
        key_prefix: str,
    ) -> None:
        self.execute(
            "INSERT INTO otel.gov_agent_keys "
            "(key_id, agent_role, key_hash, key_prefix) VALUES",
            [(key_id, agent_role, key_hash, key_prefix)],
        )

    def revoke_agent_key(self, key_id: str) -> None:
        self.execute(
            "ALTER TABLE otel.gov_agent_keys UPDATE "
            "enabled = 0, revoked_at = now() "
            "WHERE key_id = %(kid)s",
            {"kid": key_id},
        )

    def toggle_agent_key(self, key_id: str, enabled: int) -> None:
        safe_id = key_id.replace("'", "")[:64]
        v = 1 if int(enabled) else 0
        try:
            self.execute(
                f"ALTER TABLE otel.gov_agent_keys UPDATE enabled = {v} "
                f"WHERE key_id = '{safe_id}'"
            )
        except Exception as exc:
            log.warning("toggle_agent_key_failed", key_id=key_id, error=str(exc))

    # Webhooks
    def list_webhooks(self) -> list[dict]:
        return self.fetch_all(
            "SELECT webhook_id, name, url, events, enabled, updated_at "
            "FROM otel.gov_webhook_configs FINAL ORDER BY name ASC"
        )

    def save_webhook(
        self,
        webhook_id: str,
        name: str,
        url: str,
        events: list[str],
        enabled: int = 1,
        secret: str = "",
    ) -> None:
        self.execute(
            "INSERT INTO otel.gov_webhook_configs "
            "(webhook_id, name, url, events, enabled, secret) VALUES",
            [(webhook_id, name, url, events, enabled, secret)],
        )

    def delete_webhook(self, webhook_id: str) -> None:
        self.execute(
            "ALTER TABLE otel.gov_webhook_configs UPDATE enabled = 0 "
            "WHERE webhook_id = %(wid)s",
            {"wid": webhook_id},
        )

    # ── Phase 4: Safety ──────────────────────────────────────────────────────

    def get_safety_events(self, trace_id: str = "", limit: int = 100) -> list[dict]:
        """Return safety events, optionally filtered by trace_id."""
        try:
            params: dict = {"limit": limit}
            where = ""
            if trace_id:
                where = "WHERE trace_id = %(trace_id)s"
                params["trace_id"] = trace_id
            return self.fetch_all(
                f"SELECT event_id, trace_id, span_id, run_id, agent_role, "
                f"event_type, detected, pattern_name, confidence, detail, ts "
                f"FROM otel.gov_safety_events {where} "
                f"ORDER BY ts DESC LIMIT %(limit)s",
                params,
            )
        except Exception as exc:
            log.warning("get_safety_events_failed", error=str(exc))
            return []

    def get_safety_summary(self, hours: int = 24) -> dict:
        """Return aggregate safety event counts over the past N hours."""
        try:
            row = self.fetch_one(
                f"SELECT "
                f"  countIf(detected = 1)                        AS detected_count, "
                f"  countIf(event_type = 'prompt_injection')     AS injection_count, "
                f"  countIf(event_type = 'jailbreak_attempt')    AS jailbreak_count, "
                f"  countIf(event_type IN ('toxic_output', 'bias_detected')) AS toxic_bias_count, "
                f"  count()                                      AS total_count "
                f"FROM otel.gov_safety_events "
                f"WHERE ts >= now() - INTERVAL {int(hours)} HOUR"
            )
            return row or {}
        except Exception as exc:
            log.warning("get_safety_summary_failed", error=str(exc))
            return {}

    def get_safety_rules(self) -> list[dict]:
        """Return all active safety rules."""
        try:
            return self.fetch_all(
                "SELECT rule_id, rule_type, pattern, severity, description, enabled, updated_at "
                "FROM otel.gov_safety_rules FINAL "
                "WHERE enabled = 1 ORDER BY rule_type ASC, severity ASC"
            )
        except Exception as exc:
            log.warning("get_safety_rules_failed", error=str(exc))
            return []

    def save_safety_rule(
        self,
        rule_type: str,
        pattern: str,
        severity: str,
        description: str,
        enabled: int = 1,
    ) -> str:
        """Insert a new safety rule; return its generated rule_id."""
        rule_id = str(uuid.uuid4())
        try:
            self.execute(
                "INSERT INTO otel.gov_safety_rules "
                "(rule_id, rule_type, pattern, severity, description, enabled) VALUES",
                [(rule_id, rule_type, pattern, severity, description, enabled)],
            )
        except Exception as exc:
            log.warning("save_safety_rule_failed", error=str(exc))
        return rule_id

    # ── Phase 4: Identity ─────────────────────────────────────────────────────

    def get_identity_events(self, trace_id: str = "", limit: int = 100) -> list[dict]:
        """Return identity/provenance events, optionally filtered by trace_id."""
        try:
            params: dict = {"limit": limit}
            where = ""
            if trace_id:
                where = "WHERE trace_id = %(trace_id)s"
                params["trace_id"] = trace_id
            return self.fetch_all(
                f"SELECT event_id, trace_id, run_id, agent_role, check_type, "
                f"passed, detail, ts "
                f"FROM otel.gov_identity_events {where} "
                f"ORDER BY ts DESC LIMIT %(limit)s",
                params,
            )
        except Exception as exc:
            log.warning("get_identity_events_failed", error=str(exc))
            return []

    def get_supply_chain_registry(self) -> list[dict]:
        """Return all supply-chain artifact registry entries."""
        try:
            return self.fetch_all(
                "SELECT artifact_id, artifact_type, artifact_name, expected_hash, "
                "verified, updated_at "
                "FROM otel.gov_supply_chain_registry FINAL "
                "ORDER BY artifact_type ASC, artifact_name ASC"
            )
        except Exception as exc:
            log.warning("get_supply_chain_registry_failed", error=str(exc))
            return []

    def save_supply_chain_entry(
        self,
        artifact_type: str,
        artifact_name: str,
        expected_hash: str,
        verified: int = 1,
    ) -> str:
        """Upsert a supply-chain registry entry; return its artifact_id."""
        artifact_id = str(uuid.uuid4())
        try:
            self.execute(
                "INSERT INTO otel.gov_supply_chain_registry "
                "(artifact_id, artifact_type, artifact_name, expected_hash, verified) VALUES",
                [(artifact_id, artifact_type, artifact_name, expected_hash, verified)],
            )
        except Exception as exc:
            log.warning("save_supply_chain_entry_failed", error=str(exc))
        return artifact_id

    def get_tool_whitelist(self, agent_role: str = "") -> list[dict]:
        """Return tool whitelist entries, optionally filtered by agent_role."""
        try:
            params: dict = {}
            where = ""
            if agent_role:
                where = "WHERE agent_role = %(agent_role)s"
                params["agent_role"] = agent_role
            return self.fetch_all(
                f"SELECT entry_id, agent_role, tool_name, allowed, updated_at "
                f"FROM otel.gov_tool_whitelist FINAL {where} "
                f"ORDER BY agent_role ASC, tool_name ASC",
                params,
            )
        except Exception as exc:
            log.warning("get_tool_whitelist_failed", error=str(exc))
            return []

    def save_tool_whitelist_entry(
        self,
        agent_role: str,
        tool_name: str,
        allowed: int = 1,
    ) -> None:
        """Upsert a tool whitelist entry for an agent role."""
        try:
            entry_id = str(uuid.uuid4())
            self.execute(
                "INSERT INTO otel.gov_tool_whitelist "
                "(entry_id, agent_role, tool_name, allowed) VALUES",
                [(entry_id, agent_role, tool_name, allowed)],
            )
        except Exception as exc:
            log.warning("save_tool_whitelist_entry_failed", error=str(exc))

    def update_supply_chain_entry(
        self,
        artifact_id:   str,
        artifact_type: str,
        artifact_name: str,
        expected_hash: str,
        verified:      int,
    ) -> None:
        safe_id   = artifact_id.replace("'",   "")[:64]
        safe_type = artifact_type.replace("'", "")[:64]
        safe_name = artifact_name.replace("'", "\\'")[:256]
        safe_hash = expected_hash.replace("'", "\\'")[:256]
        v = 1 if int(verified) else 0
        try:
            self.execute(
                f"ALTER TABLE otel.gov_supply_chain_registry UPDATE "
                f"artifact_type = '{safe_type}', artifact_name = '{safe_name}', "
                f"expected_hash = '{safe_hash}', verified = {v} "
                f"WHERE artifact_id = '{safe_id}'"
            )
        except Exception as exc:
            log.warning("update_supply_chain_entry_failed",
                        artifact_id=artifact_id, error=str(exc))

    def delete_supply_chain_entry(self, artifact_id: str) -> None:
        safe_id = artifact_id.replace("'", "")[:64]
        try:
            self.execute(
                f"ALTER TABLE otel.gov_supply_chain_registry DELETE "
                f"WHERE artifact_id = '{safe_id}'"
            )
        except Exception as exc:
            log.warning("delete_supply_chain_entry_failed",
                        artifact_id=artifact_id, error=str(exc))

    def update_tool_whitelist_entry(
        self,
        entry_id:   str,
        agent_role: str,
        tool_name:  str,
        allowed:    int,
    ) -> None:
        safe_id   = entry_id.replace("'",   "")[:64]
        safe_role = agent_role.replace("'", "\\'")[:128]
        safe_tool = tool_name.replace("'", "\\'")[:128]
        v = 1 if int(allowed) else 0
        try:
            self.execute(
                f"ALTER TABLE otel.gov_tool_whitelist UPDATE "
                f"agent_role = '{safe_role}', tool_name = '{safe_tool}', allowed = {v} "
                f"WHERE entry_id = '{safe_id}'"
            )
        except Exception as exc:
            log.warning("update_tool_whitelist_entry_failed",
                        entry_id=entry_id, error=str(exc))

    def delete_tool_whitelist_entry(self, entry_id: str) -> None:
        safe_id = entry_id.replace("'", "")[:64]
        try:
            self.execute(
                f"ALTER TABLE otel.gov_tool_whitelist DELETE "
                f"WHERE entry_id = '{safe_id}'"
            )
        except Exception as exc:
            log.warning("delete_tool_whitelist_entry_failed",
                        entry_id=entry_id, error=str(exc))

    # ── Phase 4: Reliability ──────────────────────────────────────────────────

    def get_reliability_metrics(
        self, agent_role: str = "", limit: int = 50
    ) -> list[dict]:
        """Return reliability metric rows, optionally filtered by agent_role."""
        try:
            params: dict = {"limit": limit}
            where = ""
            if agent_role:
                where = "WHERE agent_role = %(agent_role)s"
                params["agent_role"] = agent_role
            return self.fetch_all(
                f"SELECT metric_id, trace_id, run_id, agent_role, "
                f"success, latency_ms, error_type, ts "
                f"FROM otel.gov_reliability_metrics {where} "
                f"ORDER BY ts DESC LIMIT %(limit)s",
                params,
            )
        except Exception as exc:
            log.warning("get_reliability_metrics_failed", error=str(exc))
            return []

    def get_slo_config(self) -> list[dict]:
        """Return SLO configuration for all agent roles."""
        try:
            return self.fetch_all(
                "SELECT agent_role, target_availability, error_budget_pct, "
                "slo_window_days, p95_target_ms, p99_target_ms, updated_at "
                "FROM otel.gov_slo_config FINAL ORDER BY agent_role ASC"
            )
        except Exception as exc:
            log.warning("get_slo_config_failed", error=str(exc))
            return []

    def save_slo_config(
        self,
        agent_role: str,
        target_availability: float,
        error_budget_pct: float,
        slo_window_days: int,
        p95_target_ms: float,
        p99_target_ms: float,
    ) -> None:
        """Upsert SLO configuration for an agent role."""
        try:
            self.execute(
                "INSERT INTO otel.gov_slo_config "
                "(agent_role, target_availability, error_budget_pct, "
                "slo_window_days, p95_target_ms, p99_target_ms) VALUES",
                [(agent_role, float(target_availability), float(error_budget_pct),
                  int(slo_window_days), float(p95_target_ms), float(p99_target_ms))],
            )
        except Exception as exc:
            log.warning("save_slo_config_failed", error=str(exc))

    def get_incidents(
        self, agent_role: str = "", status: str = "", limit: int = 50
    ) -> list[dict]:
        """Return incidents, optionally filtered by agent_role and/or status."""
        try:
            conditions = []
            params: dict = {"limit": limit}
            if agent_role:
                conditions.append("agent_role = %(agent_role)s")
                params["agent_role"] = agent_role
            if status:
                conditions.append("status = %(status)s")
                params["status"] = status
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            return self.fetch_all(
                f"SELECT incident_id, agent_role, incident_type, severity, "
                f"trigger_event, status, root_cause, recurrence_of, "
                f"opened_at, contained_at, resolved_at, detail "
                f"FROM otel.gov_incidents {where} "
                f"ORDER BY opened_at DESC LIMIT %(limit)s",
                params,
            )
        except Exception as exc:
            log.warning("get_incidents_failed", error=str(exc))
            return []

    def update_incident(
        self,
        incident_id: str,
        status: str,
        root_cause: str = "",
        contained_at: str = "",
        resolved_at: str = "",
    ) -> None:
        """Update status and resolution fields of an existing incident.

        Builds the SET clause dynamically to avoid passing empty strings
        to toDateTime() — ClickHouse evaluates both if() branches in mutations.
        """
        # Sanitise inputs — only allow safe values to prevent injection
        safe_status    = status.replace("'", "")[:32]
        safe_cause     = root_cause.replace("'", "\\'"  )[:1000]
        safe_id        = incident_id.replace("'", "")[:64]

        set_parts = [
            f"status = '{safe_status}'",
            f"root_cause = '{safe_cause}'",
        ]
        if contained_at:
            set_parts.append(f"contained_at = toDateTime('{contained_at}')")
        if resolved_at:
            set_parts.append(f"resolved_at = toDateTime('{resolved_at}')")

        sql = (
            f"ALTER TABLE otel.gov_incidents UPDATE "
            f"{', '.join(set_parts)} "
            f"WHERE incident_id = '{safe_id}'"
        )
        try:
            self.execute(sql)
        except Exception as exc:
            log.warning("update_incident_failed", error=str(exc))

    # ── Phase 4: Behavior ─────────────────────────────────────────────────────

    def get_behavior_events(self, trace_id: str = "", limit: int = 100) -> list[dict]:
        """Return behavior analysis events, optionally filtered by trace_id."""
        try:
            params: dict = {"limit": limit}
            where = ""
            if trace_id:
                where = "WHERE trace_id = %(trace_id)s"
                params["trace_id"] = trace_id
            return self.fetch_all(
                f"SELECT event_id, trace_id, run_id, agent_role, check_type, "
                f"passed, detail, ts "
                f"FROM otel.gov_behavior_events {where} "
                f"ORDER BY ts DESC LIMIT %(limit)s",
                params,
            )
        except Exception as exc:
            log.warning("get_behavior_events_failed", error=str(exc))
            return []

    def get_persona_config(self, agent_role: str = "") -> list[dict]:
        """Return persona/topic-scope config, optionally filtered by agent_role."""
        try:
            params: dict = {}
            where = ""
            if agent_role:
                where = "WHERE agent_role = %(agent_role)s"
                params["agent_role"] = agent_role
            return self.fetch_all(
                f"SELECT config_id, agent_role, authorized_topics, "
                f"persona_description, updated_at "
                f"FROM otel.gov_persona_config FINAL {where} "
                f"ORDER BY agent_role ASC",
                params,
            )
        except Exception as exc:
            log.warning("get_persona_config_failed", error=str(exc))
            return []

    def save_persona_config(
        self,
        agent_role: str,
        authorized_topics: list,
        persona_description: str,
    ) -> None:
        """Upsert persona configuration for an agent role."""
        try:
            import json as _json
            config_id = str(uuid.uuid4())
            self.execute(
                "INSERT INTO otel.gov_persona_config "
                "(config_id, agent_role, authorized_topics, persona_description) VALUES",
                [(config_id, agent_role,
                  _json.dumps(authorized_topics), persona_description)],
            )
        except Exception as exc:
            log.warning("save_persona_config_failed", error=str(exc))

    # ── Phase 4: Lifecycle ────────────────────────────────────────────────────

    def get_change_log(self, agent_role: str = "", limit: int = 50) -> list[dict]:
        """Return lifecycle change log entries, optionally filtered by agent_role."""
        try:
            params: dict = {"limit": limit}
            where = ""
            if agent_role:
                where = "WHERE agent_role = %(agent_role)s"
                params["agent_role"] = agent_role
            return self.fetch_all(
                f"SELECT change_id, agent_role, change_type, old_value, "
                f"new_value, changed_by, ts "
                f"FROM otel.gov_change_log {where} "
                f"ORDER BY ts DESC LIMIT %(limit)s",
                params,
            )
        except Exception as exc:
            log.warning("get_change_log_failed", error=str(exc))
            return []

    def get_version_pins(self) -> list[dict]:
        """Return all model version pin entries."""
        try:
            return self.fetch_all(
                "SELECT pin_id, agent_role, model_name, pinned_version, "
                "is_pinned, deployment_mode, updated_at "
                "FROM otel.gov_version_pins FINAL "
                "WHERE is_pinned = 1 ORDER BY agent_role ASC, model_name ASC"
            )
        except Exception as exc:
            log.warning("get_version_pins_failed", error=str(exc))
            return []

    def save_version_pin(
        self,
        agent_role: str,
        model_name: str,
        pinned_version: str,
        is_pinned: int = 1,
        deployment_mode: str = "production",
    ) -> None:
        """Upsert a model version pin for an agent role."""
        try:
            pin_id = str(uuid.uuid4())
            self.execute(
                "INSERT INTO otel.gov_version_pins "
                "(pin_id, agent_role, model_name, pinned_version, "
                "is_pinned, deployment_mode) VALUES",
                [(pin_id, agent_role, model_name, pinned_version,
                  is_pinned, deployment_mode)],
            )
        except Exception as exc:
            log.warning("save_version_pin_failed", error=str(exc))

    # ── Phase 4: Regulatory ───────────────────────────────────────────────────

    def get_compliance_scorecards(self, framework: str = "") -> list[dict]:
        """Return compliance scorecards, optionally filtered by framework."""
        try:
            params: dict = {}
            where = ""
            if framework:
                where = "WHERE framework = %(framework)s"
                params["framework"] = framework
            return self.fetch_all(
                f"SELECT scorecard_id, framework, agent_role, score, "
                f"controls_passing, controls_failing, generated_at "
                f"FROM otel.gov_compliance_scorecards {where} "
                f"ORDER BY generated_at DESC",
                params,
            )
        except Exception as exc:
            log.warning("get_compliance_scorecards_failed", error=str(exc))
            return []

    def get_model_registry(self) -> list[dict]:
        """Return all model registry entries."""
        try:
            return self.fetch_all(
                "SELECT entry_id, model_name, model_version, provider, "
                "license_type, commercial_ok, dpa_signed, baa_signed, "
                "sectors_allowed, notes, created_at "
                "FROM otel.gov_model_registry FINAL "
                "ORDER BY model_name ASC, model_version ASC"
            )
        except Exception as exc:
            log.warning("get_model_registry_failed", error=str(exc))
            return []

    def save_model_registry_entry(
        self,
        model_name: str,
        model_version: str,
        provider: str,
        license_type: str,
        commercial_ok: int,
        dpa_signed: int,
        baa_signed: int,
        sectors_allowed: list,
        notes: str,
    ) -> str:
        """Insert a model registry entry; return its entry_id."""
        entry_id = str(uuid.uuid4())
        try:
            import json as _json
            self.execute(
                "INSERT INTO otel.gov_model_registry "
                "(entry_id, model_name, model_version, provider, license_type, "
                "commercial_ok, dpa_signed, baa_signed, sectors_allowed, notes) VALUES",
                [(entry_id, model_name, model_version, provider, license_type,
                  commercial_ok, dpa_signed, baa_signed,
                  _json.dumps(sectors_allowed), notes)],
            )
        except Exception as exc:
            log.warning("save_model_registry_entry_failed", error=str(exc))
        return entry_id

    def get_regulatory_scope(self, agent_role: str = "") -> list[dict]:
        """Return regulatory scope entries, optionally filtered by agent_role."""
        try:
            params: dict = {}
            where = ""
            if agent_role:
                where = "WHERE agent_role = %(agent_role)s"
                params["agent_role"] = agent_role
            return self.fetch_all(
                f"SELECT scope_id, agent_role, framework, sector, "
                f"classification, notes, created_at "
                f"FROM otel.gov_regulatory_scope FINAL {where} "
                f"ORDER BY agent_role ASC, framework ASC",
                params,
            )
        except Exception as exc:
            log.warning("get_regulatory_scope_failed", error=str(exc))
            return []

    def save_regulatory_scope(
        self,
        agent_role: str,
        framework: str,
        sector: str,
        classification: str,
        notes: str,
    ) -> str:
        """Insert a regulatory scope entry; return its scope_id."""
        scope_id = str(uuid.uuid4())
        try:
            self.execute(
                "INSERT INTO otel.gov_regulatory_scope "
                "(scope_id, agent_role, framework, sector, classification, notes) VALUES",
                [(scope_id, agent_role, framework, sector, classification, notes)],
            )
        except Exception as exc:
            log.warning("save_regulatory_scope_failed", error=str(exc))
        return scope_id

    def get_risk_register(self) -> list[dict]:
        """Return all risk register entries."""
        try:
            return self.fetch_all(
                "SELECT risk_id, title, category, likelihood, impact, "
                "owner, mitigation, status, created_at, updated_at "
                "FROM otel.gov_risk_register FINAL "
                "ORDER BY (likelihood * impact) DESC, created_at DESC"
            )
        except Exception as exc:
            log.warning("get_risk_register_failed", error=str(exc))
            return []

    def save_risk_item(
        self,
        title: str,
        category: str,
        likelihood: int,
        impact: int,
        owner: str,
        mitigation: str,
        status: str,
        risk_id: str = "",
    ) -> str:
        """Upsert a risk register item; return its risk_id."""
        if not risk_id:
            risk_id = str(uuid.uuid4())
        try:
            self.execute(
                "INSERT INTO otel.gov_risk_register "
                "(risk_id, title, category, likelihood, impact, "
                "owner, mitigation, status) VALUES",
                [(risk_id, title, category, int(likelihood), int(impact),
                  owner, mitigation, status)],
            )
        except Exception as exc:
            log.warning("save_risk_item_failed", error=str(exc))
        return risk_id

    # ── Phase 5: Threshold Configuration ─────────────────────────────────────

    def get_all_thresholds(self) -> dict[str, float]:
        """Return all threshold config entries as {config_key: value}."""
        try:
            rows = self.fetch_all(
                "SELECT config_key, value FROM otel.gov_threshold_config FINAL"
            )
            return {r["config_key"]: float(r["value"]) for r in (rows or [])}
        except Exception as exc:
            log.warning("get_all_thresholds_failed", error=str(exc))
            return {}

    def get_threshold(self, key: str, default: float) -> float:
        """Return a single threshold value by key, or default if not found."""
        try:
            row = self.fetch_one(
                "SELECT value FROM otel.gov_threshold_config FINAL "
                "WHERE config_key = %(k)s",
                {"k": key},
            )
            if row and row.get("value") is not None:
                return float(row["value"])
        except Exception as exc:
            log.warning("get_threshold_failed", key=key, error=str(exc))
        return default

    def save_threshold(
        self,
        config_key: str,
        value: float,
        display_name: str = "",
        category: str = "",
        unit: str = "",
        description: str = "",
        updated_by: str = "user",
    ) -> None:
        """Insert or replace a threshold config entry."""
        try:
            self.execute(
                "INSERT INTO otel.gov_threshold_config "
                "(config_key, display_name, category, value, unit, description, updated_by) "
                "VALUES",
                [(config_key, display_name, category, float(value),
                  unit, description, updated_by)],
            )
        except Exception as exc:
            log.warning("save_threshold_failed", key=config_key, error=str(exc))

    def get_threshold_config_all(self) -> list[dict]:
        """Return all threshold config entries with metadata, ordered by category."""
        try:
            return self.fetch_all(
                "SELECT config_key, display_name, category, value, unit, "
                "description, updated_at, updated_by "
                "FROM otel.gov_threshold_config FINAL "
                "ORDER BY category, config_key"
            )
        except Exception as exc:
            log.warning("get_threshold_config_all_failed", error=str(exc))
            return []

    def delete_threshold(self, config_key: str) -> None:
        """Hard-delete a threshold config entry."""
        try:
            self.execute(
                "ALTER TABLE otel.gov_threshold_config DELETE "
                "WHERE config_key = %(k)s",
                {"k": config_key},
            )
        except Exception as exc:
            log.warning("delete_threshold_failed", key=config_key, error=str(exc))

    # ── Phase 5: Enforcement — Trust Score ────────────────────────────────────

    def save_trust_score(
        self,
        agent_role: str,
        trust_score: float,
        identity_score: float,
        behavior_score: float,
        compliance_score: float,
        network_score: float,
        trust_tier: str,
    ) -> None:
        try:
            self.execute(
                "INSERT INTO otel.gov_trust_scores "
                "(agent_role, trust_score, identity_score, behavior_score, "
                "compliance_score, network_score, trust_tier) VALUES",
                [(agent_role, float(trust_score), float(identity_score),
                  float(behavior_score), float(compliance_score),
                  float(network_score), trust_tier)],
            )
        except Exception as exc:
            log.warning("save_trust_score_failed", agent_role=agent_role, error=str(exc))

    def get_latest_trust_scores(self) -> list[dict]:
        """Return most recent trust score per agent_role."""
        try:
            return self.fetch_all(
                "SELECT agent_role, trust_score, identity_score, behavior_score, "
                "compliance_score, network_score, trust_tier, max(computed_at) AS computed_at "
                "FROM otel.gov_trust_scores "
                "GROUP BY agent_role, trust_score, identity_score, behavior_score, "
                "compliance_score, network_score, trust_tier "
                "ORDER BY computed_at DESC "
                "LIMIT 1 BY agent_role"
            )
        except Exception as exc:
            log.warning("get_latest_trust_scores_failed", error=str(exc))
            return []

    def get_trust_score_history(self, agent_role: str, hours: int = 24) -> list[dict]:
        try:
            return self.fetch_all(
                "SELECT agent_role, trust_score, trust_tier, computed_at "
                "FROM otel.gov_trust_scores "
                "WHERE agent_role = %(role)s "
                "  AND computed_at >= now() - INTERVAL %(h)s HOUR "
                "ORDER BY computed_at ASC",
                {"role": agent_role, "h": hours},
            )
        except Exception as exc:
            log.warning("get_trust_score_history_failed", error=str(exc))
            return []

    # ── Phase 5: Enforcement — Circuit Breaker ────────────────────────────────

    def get_circuit_breaker(self, agent_role: str) -> dict:
        try:
            rows = self.fetch_all(
                "SELECT agent_role, state, failure_count, failure_threshold, "
                "last_failure_at, opened_at, reset_at, quarantine_reason, updated_at "
                "FROM otel.gov_circuit_breakers FINAL "
                "WHERE agent_role IN (%(role)s, '*')",
                {"role": agent_role},
            )
            if not rows:
                return {}
            # If the agent's own CB is open/half_open, return it.
            # If the wildcard CB (*) is open, it acts as a global kill switch.
            for row in rows:
                if str(row.get("agent_role", "")) == agent_role and str(row.get("state", "")).lower() in ("open", "half_open"):
                    return row
            for row in rows:
                if str(row.get("agent_role", "")) == "*" and str(row.get("state", "")).lower() in ("open", "half_open"):
                    return row
            # Neither is tripped — return the agent's own row for failure_count tracking
            for row in rows:
                if str(row.get("agent_role", "")) == agent_role:
                    return row
            return rows[0]
        except Exception as exc:
            log.warning("get_circuit_breaker_failed", agent_role=agent_role, error=str(exc))
            return {}

    def get_all_circuit_breakers(self) -> list[dict]:
        try:
            return self.fetch_all(
                "SELECT agent_role, state, failure_count, failure_threshold, "
                "last_failure_at, opened_at, reset_at, quarantine_reason, updated_at "
                "FROM otel.gov_circuit_breakers FINAL "
                "ORDER BY agent_role ASC"
            )
        except Exception as exc:
            log.warning("get_all_circuit_breakers_failed", error=str(exc))
            return []

    def upsert_circuit_breaker(
        self,
        agent_role: str,
        state: str,
        failure_count: int,
        failure_threshold: int = 5,
        quarantine_reason: str = "",
    ) -> None:
        safe_role   = agent_role.replace("'", "")[:128]
        safe_state  = state.replace("'", "")[:32]
        safe_reason = quarantine_reason.replace("'", "\\'")[:512]
        fc = int(failure_count)
        ft = int(failure_threshold)
        try:
            self.execute(
                "INSERT INTO otel.gov_circuit_breakers "
                "(agent_role, state, failure_count, failure_threshold, quarantine_reason) VALUES",
                [(safe_role, safe_state, fc, ft, safe_reason)],
            )
        except Exception as exc:
            log.warning("upsert_circuit_breaker_failed", agent_role=agent_role, error=str(exc))

    def reset_circuit_breaker(self, agent_role: str) -> None:
        safe_role = agent_role.replace("'", "")[:128]
        try:
            self.execute(
                f"ALTER TABLE otel.gov_circuit_breakers UPDATE "
                f"state = 'closed', failure_count = 0, quarantine_reason = '' "
                f"WHERE agent_role = '{safe_role}'"
            )
        except Exception as exc:
            log.warning("reset_circuit_breaker_failed", agent_role=agent_role, error=str(exc))

    # ── Phase 5: Enforcement — Rogue Assessment ───────────────────────────────

    def save_rogue_assessment(
        self,
        agent_role: str,
        composite_score: float,
        frequency_score: float,
        entropy_score: float,
        capability_score: float,
        risk_level: str,
        quarantine_recommended: bool,
        detail: str = "",
    ) -> None:
        try:
            self.execute(
                "INSERT INTO otel.gov_rogue_assessments "
                "(agent_role, composite_score, frequency_score, entropy_score, "
                "capability_score, risk_level, quarantine_recommended, detail) VALUES",
                [(agent_role, float(composite_score), float(frequency_score),
                  float(entropy_score), float(capability_score),
                  risk_level, 1 if quarantine_recommended else 0, detail)],
            )
        except Exception as exc:
            log.warning("save_rogue_assessment_failed", agent_role=agent_role, error=str(exc))

    def get_latest_rogue_assessments(self) -> list[dict]:
        try:
            return self.fetch_all(
                "SELECT agent_role, composite_score, frequency_score, entropy_score, "
                "capability_score, risk_level, quarantine_recommended, detail, "
                "max(assessed_at) AS assessed_at "
                "FROM otel.gov_rogue_assessments "
                "GROUP BY agent_role, composite_score, frequency_score, entropy_score, "
                "capability_score, risk_level, quarantine_recommended, detail "
                "ORDER BY assessed_at DESC "
                "LIMIT 1 BY agent_role"
            )
        except Exception as exc:
            log.warning("get_latest_rogue_assessments_failed", error=str(exc))
            return []

    def get_rogue_assessment_history(self, agent_role: str, hours: int = 24) -> list[dict]:
        try:
            return self.fetch_all(
                "SELECT composite_score, risk_level, quarantine_recommended, assessed_at "
                "FROM otel.gov_rogue_assessments "
                "WHERE agent_role = %(role)s "
                "  AND assessed_at >= now() - INTERVAL %(h)s HOUR "
                "ORDER BY assessed_at ASC",
                {"role": agent_role, "h": hours},
            )
        except Exception as exc:
            log.warning("get_rogue_assessment_history_failed", error=str(exc))
            return []

    # ── Quality Gates ─────────────────────────────────────────────────────────

    def get_quality_gate_configs(self, agent_role: str = "") -> list[dict]:
        try:
            params: dict = {}
            where = ""
            if agent_role:
                where = "WHERE agent_role = %(role)s"
                params["role"] = agent_role
            return self.fetch_all(
                f"SELECT gate_id, agent_role, metric, threshold, action, "
                f"enabled, description, updated_at "
                f"FROM otel.gov_quality_gate_config FINAL {where} "
                f"ORDER BY agent_role, metric",
                params,
            )
        except Exception as exc:
            log.warning("get_quality_gate_configs_failed", error=str(exc))
            return []

    def save_quality_gate_config(
        self,
        agent_role: str,
        metric: str,
        threshold: float,
        action: str = "flag",
        enabled: int = 1,
        description: str = "",
        gate_id: str = "",
    ) -> str:
        if not gate_id:
            gate_id = str(uuid.uuid4())
        try:
            self.execute(
                "INSERT INTO otel.gov_quality_gate_config "
                "(gate_id, agent_role, metric, threshold, action, enabled, description) VALUES",
                [(gate_id, agent_role, metric, float(threshold), action, enabled, description)],
            )
        except Exception as exc:
            log.warning("save_quality_gate_config_failed", error=str(exc))
        return gate_id

    def delete_quality_gate_config(self, gate_id: str) -> None:
        try:
            self.execute(
                "ALTER TABLE otel.gov_quality_gate_config DELETE "
                "WHERE gate_id = %(gid)s",
                {"gid": gate_id},
            )
        except Exception as exc:
            log.warning("delete_quality_gate_config_failed", gate_id=gate_id, error=str(exc))

    def save_quality_gate_decision(
        self,
        decision_id: str,
        trace_id: str,
        run_id: str,
        agent_role: str,
        metric: str,
        score: float,
        threshold: float,
        action: str,
        status: str = "pending",
    ) -> None:
        try:
            self.execute(
                "INSERT INTO otel.gov_quality_gate_decisions "
                "(decision_id, trace_id, run_id, agent_role, metric, "
                "score, threshold, action, status) VALUES",
                [(decision_id, trace_id, run_id, agent_role, metric,
                  float(score), float(threshold), action, status)],
            )
        except Exception as exc:
            log.warning("save_quality_gate_decision_failed", error=str(exc))

    def get_quality_gate_decisions(
        self,
        agent_role: str = "",
        status: str = "",
        hours: int = 24,
        limit: int = 200,
    ) -> list[dict]:
        try:
            conditions = [f"created_at >= now() - INTERVAL {int(hours)} HOUR"]
            params: dict = {"limit": limit}
            if agent_role:
                conditions.append("agent_role = %(role)s")
                params["role"] = agent_role
            if status:
                conditions.append("status = %(status)s")
                params["status"] = status
            where = "WHERE " + " AND ".join(conditions)
            return self.fetch_all(
                f"SELECT decision_id, trace_id, run_id, agent_role, metric, "
                f"score, threshold, action, status, reviewer, notes, created_at "
                f"FROM otel.gov_quality_gate_decisions {where} "
                f"ORDER BY created_at DESC LIMIT %(limit)s",
                params,
            )
        except Exception as exc:
            log.warning("get_quality_gate_decisions_failed", error=str(exc))
            return []

    def get_hold_count(self, agent_role: str, hours: int = 24) -> int:
        """Count confirmed (rejected or expired) hold decisions for an agent within the window."""
        try:
            row = self.fetch_one(
                "SELECT count() AS cnt FROM otel.gov_quality_gate_decisions "
                "WHERE agent_role = %(role)s "
                "  AND action = 'hold' "
                "  AND status IN ('rejected', 'expired') "
                "  AND created_at >= now() - INTERVAL %(h)s HOUR",
                {"role": agent_role, "h": hours},
            )
            return int(row.get("cnt", 0) if row else 0)
        except Exception as exc:
            log.warning("get_hold_count_failed", agent_role=agent_role, error=str(exc))
            return 0

    def expire_timed_out_holds(self, timeout_seconds: int) -> list[dict]:
        """Find pending quality_gate_hold HITL entries past timeout, mark them expired.

        Returns list of {agent_role, decision_id} for threshold checking by caller.
        Uses INSERT pattern (RMT) for gov_hitl_queue; ALTER TABLE UPDATE for gov_quality_gate_decisions.
        """
        import json as _json
        try:
            rows = self.fetch_all(
                "SELECT request_id, payload, created_at "
                "FROM otel.gov_hitl_queue FINAL "
                "WHERE action_type = 'quality_gate_hold' "
                "  AND status = 'pending' "
                "  AND created_at < now() - INTERVAL %(t)s SECOND",
                {"t": int(timeout_seconds)},
            )
        except Exception as exc:
            log.warning("expire_timed_out_holds_query_failed", error=str(exc))
            return []

        expired = []
        from datetime import datetime, timezone
        for row in rows:
            request_id = str(row.get("request_id", ""))
            if not request_id:
                continue
            try:
                payload = _json.loads(row.get("payload", "{}") or "{}")
            except Exception:
                payload = {}
            agent_role  = payload.get("agent_role", "")
            decision_id = payload.get("quality_gate_decision_id", "")

            # Mark HITL entry expired via INSERT (ReplacingMergeTree picks latest decided_at)
            try:
                original = self.fetch_one(
                    "SELECT trace_id, span_id, run_id, risk_tier, action_type, payload, created_at "
                    "FROM otel.gov_hitl_queue FINAL WHERE request_id = %(rid)s",
                    {"rid": request_id},
                ) or {}
                self.execute(
                    "INSERT INTO otel.gov_hitl_queue "
                    "(request_id, trace_id, span_id, run_id, risk_tier, action_type, "
                    "payload, status, reviewer, notes, created_at, decided_at) VALUES",
                    [(
                        request_id,
                        original.get("trace_id", ""),
                        original.get("span_id", ""),
                        original.get("run_id", ""),
                        original.get("risk_tier", "high"),
                        original.get("action_type", "quality_gate_hold"),
                        original.get("payload", ""),
                        "expired",
                        "system",
                        "auto-expired: review timeout",
                        original.get("created_at") or datetime.now(timezone.utc),
                        datetime.now(timezone.utc),
                    )],
                )
            except Exception as exc:
                log.warning("expire_hitl_insert_failed", request_id=request_id, error=str(exc))
                continue

            # Mark quality gate decision expired (plain MergeTree → ALTER TABLE UPDATE)
            if decision_id:
                try:
                    self.execute(
                        "ALTER TABLE otel.gov_quality_gate_decisions "
                        "UPDATE status = 'expired' WHERE decision_id = %(did)s",
                        {"did": decision_id},
                    )
                except Exception as exc:
                    log.warning("expire_qg_decision_failed", decision_id=decision_id, error=str(exc))

            if agent_role:
                expired.append({"agent_role": agent_role, "decision_id": decision_id})
                log.info("hold_hitl_expired", agent_role=agent_role, request_id=request_id)

        return expired

    def bulk_expire_quality_gate_hitl(self, reviewer: str = "operator") -> int:
        """Expire ALL pending quality_gate_hold and quality_gate_block HITL entries.

        Cleanup operation — does NOT trigger CB threshold checks.
        Returns count of entries expired.
        """
        import json as _json
        from datetime import datetime, timezone
        try:
            rows = self.fetch_all(
                "SELECT request_id, trace_id, span_id, run_id, risk_tier, action_type, payload, created_at "
                "FROM otel.gov_hitl_queue FINAL "
                "WHERE action_type IN ('quality_gate_hold', 'quality_gate_block') "
                "  AND status = 'pending'",
                {},
            )
        except Exception as exc:
            log.warning("bulk_expire_qg_hitl_query_failed", error=str(exc))
            return 0

        count = 0
        now = datetime.now(timezone.utc)
        for row in rows:
            request_id = str(row.get("request_id", ""))
            if not request_id:
                continue
            try:
                self.execute(
                    "INSERT INTO otel.gov_hitl_queue "
                    "(request_id, trace_id, span_id, run_id, risk_tier, action_type, "
                    "payload, status, reviewer, notes, created_at, decided_at) VALUES",
                    [(
                        request_id,
                        row.get("trace_id", ""),
                        row.get("span_id", ""),
                        row.get("run_id", ""),
                        row.get("risk_tier", "high"),
                        row.get("action_type", "quality_gate_hold"),
                        row.get("payload", ""),
                        "expired",
                        reviewer,
                        "bulk-dismissed: operator cleared history",
                        row.get("created_at") or now,
                        now,
                    )],
                )
            except Exception as exc:
                log.warning("bulk_expire_qg_hitl_insert_failed", request_id=request_id, error=str(exc))
                continue

            try:
                payload = _json.loads(row.get("payload", "{}") or "{}")
                decision_id = payload.get("quality_gate_decision_id", "")
                if decision_id:
                    self.execute(
                        "ALTER TABLE otel.gov_quality_gate_decisions "
                        "UPDATE status = 'expired' WHERE decision_id = %(did)s",
                        {"did": decision_id},
                    )
            except Exception as exc:
                log.warning("bulk_expire_qg_decision_failed", request_id=request_id, error=str(exc))

            count += 1

        if count:
            log.info("bulk_expire_quality_gate_hitl_complete", count=count)
        return count

    def mark_quality_gate_decision(self, decision_id: str, status: str) -> None:
        """Update status of a specific quality gate decision (rejected / reviewed / expired)."""
        try:
            self.execute(
                "ALTER TABLE otel.gov_quality_gate_decisions "
                "UPDATE status = %(status)s WHERE decision_id = %(did)s",
                {"status": status, "did": decision_id},
            )
        except Exception as exc:
            log.warning("mark_qg_decision_failed", decision_id=decision_id, status=status, error=str(exc))

    def record_quality_gate_cb_failure(self, agent_role: str, reason: str) -> None:
        """Increment CB failure count for quality gate violation. Opens CB if threshold crossed."""
        cb = self.get_circuit_breaker(agent_role)
        if not cb:
            self.upsert_circuit_breaker(
                agent_role=agent_role, state="closed",
                failure_count=1, failure_threshold=5,
                quarantine_reason="",
            )
            return
        failure_count     = int(cb.get("failure_count", 0)) + 1
        failure_threshold = int(cb.get("failure_threshold", 5))
        current_state     = str(cb.get("state", "closed"))
        new_state         = current_state
        new_reason        = str(cb.get("quarantine_reason", "") or "")
        if failure_count >= failure_threshold and current_state == "closed":
            new_state  = "open"
            new_reason = reason
        self.upsert_circuit_breaker(
            agent_role=agent_role, state=new_state,
            failure_count=failure_count, failure_threshold=failure_threshold,
            quarantine_reason=new_reason,
        )
        log.info("quality_gate_cb_failure_recorded",
                 agent_role=agent_role, failure_count=failure_count,
                 threshold=failure_threshold, new_state=new_state)

    def decrement_quality_gate_cb_failure(self, agent_role: str) -> None:
        """Decrement CB failure count when operator approves/overrides a quality gate block."""
        cb = self.get_circuit_breaker(agent_role)
        if not cb:
            return
        failure_count = max(0, int(cb.get("failure_count", 0)) - 1)
        self.upsert_circuit_breaker(
            agent_role=agent_role,
            state=str(cb.get("state", "closed")),
            failure_count=failure_count,
            failure_threshold=int(cb.get("failure_threshold", 5)),
            quarantine_reason=str(cb.get("quarantine_reason", "") or ""),
        )
        log.info("quality_gate_cb_failure_decremented",
                 agent_role=agent_role, new_failure_count=failure_count)

    def clear_quality_gate_blocks(self, agent_role: str) -> None:
        """Mark pending block/hold quality gate decisions as reviewed for this agent.

        Called when an operator approves a HITL entry. Clears the pre-execution gate
        escalation signal so the agent is not re-blocked on every subsequent invocation.
        """
        try:
            self.execute(
                "ALTER TABLE otel.gov_quality_gate_decisions "
                "UPDATE status = 'reviewed' "
                "WHERE agent_role = %(role)s "
                "  AND status = 'pending' "
                "  AND action IN ('block', 'hold')",
                {"role": agent_role},
            )
            log.info("quality_gate_blocks_cleared", agent_role=agent_role)
        except Exception as exc:
            log.warning("clear_quality_gate_blocks_failed", agent_role=agent_role, error=str(exc))

    def get_recent_quality_gate_blocks(self, agent_role: str, hours: int = 1) -> list[dict]:
        """Return recent block/hold decisions for gate check escalation."""
        try:
            return self.fetch_all(
                "SELECT metric, action, score, threshold, created_at "
                "FROM otel.gov_quality_gate_decisions "
                "WHERE agent_role = %(role)s "
                "  AND action IN ('block', 'hold') "
                "  AND status = 'pending' "
                "  AND created_at >= now() - INTERVAL %(h)s HOUR "
                "ORDER BY created_at DESC LIMIT 10",
                {"role": agent_role, "h": hours},
            )
        except Exception as exc:
            log.warning("get_recent_quality_gate_blocks_failed", error=str(exc))
            return []
