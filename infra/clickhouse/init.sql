-- ============================================================
-- OTel standard tables (same schema as otelcol-contrib creates).
-- Pre-creating them so the UI works before any trace data arrives.
-- The collector's create_schema: true is idempotent (IF NOT EXISTS).
-- ============================================================

CREATE DATABASE IF NOT EXISTS otel;

-- ── OTel Traces ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS otel.otel_traces (
    Timestamp          DateTime64(9)                         CODEC(Delta, ZSTD(1)),
    TraceId            String                                CODEC(ZSTD(1)),
    SpanId             String                                CODEC(ZSTD(1)),
    ParentSpanId       String                                CODEC(ZSTD(1)),
    TraceState         String                                CODEC(ZSTD(1)),
    SpanName           LowCardinality(String)                CODEC(ZSTD(1)),
    SpanKind           LowCardinality(String)                CODEC(ZSTD(1)),
    ServiceName        LowCardinality(String)                CODEC(ZSTD(1)),
    ResourceAttributes Map(LowCardinality(String), String)   CODEC(ZSTD(1)),
    ScopeName          String                                CODEC(ZSTD(1)),
    ScopeVersion       String                                CODEC(ZSTD(1)),
    SpanAttributes     Map(LowCardinality(String), String)   CODEC(ZSTD(1)),
    Duration           Int64                                 CODEC(ZSTD(1)),
    StatusCode         LowCardinality(String)                CODEC(ZSTD(1)),
    StatusMessage      String                                CODEC(ZSTD(1)),
    Events Nested (
        Timestamp  DateTime64(9),
        Name       LowCardinality(String),
        Attributes Map(LowCardinality(String), String)
    ) CODEC(ZSTD(1)),
    Links Nested (
        TraceId    String,
        SpanId     String,
        TraceState String,
        Attributes Map(LowCardinality(String), String)
    ) CODEC(ZSTD(1))
) ENGINE = ReplacingMergeTree(Timestamp)
PARTITION BY toDate(Timestamp)
ORDER BY (ServiceName, SpanName, toUnixTimestamp(Timestamp), TraceId, SpanId)
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

-- ── OTel Logs ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS otel.otel_logs (
    Timestamp          DateTime64(9)                         CODEC(Delta, ZSTD(1)),
    TraceId            String                                CODEC(ZSTD(1)),
    SpanId             String                                CODEC(ZSTD(1)),
    TraceFlags         UInt8,
    SeverityText       LowCardinality(String)                CODEC(ZSTD(1)),
    SeverityNumber     UInt8,
    ServiceName        LowCardinality(String)                CODEC(ZSTD(1)),
    Body               String                                CODEC(ZSTD(1)),
    ResourceAttributes Map(LowCardinality(String), String)   CODEC(ZSTD(1)),
    LogAttributes      Map(LowCardinality(String), String)   CODEC(ZSTD(1)),
    ScopeName          String                                CODEC(ZSTD(1)),
    ScopeVersion       String                                CODEC(ZSTD(1))
) ENGINE = MergeTree()
PARTITION BY toDate(Timestamp)
ORDER BY (ServiceName, Timestamp)
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

-- ── OTel Metrics ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS otel.otel_metrics_gauge (
    ResourceAttributes Map(LowCardinality(String), String)   CODEC(ZSTD(1)),
    ResourceSchemaUrl  String                                CODEC(ZSTD(1)),
    ScopeName          String                                CODEC(ZSTD(1)),
    ScopeVersion       String                                CODEC(ZSTD(1)),
    ScopeAttributes    Map(LowCardinality(String), String)   CODEC(ZSTD(1)),
    ScopeDroppedAttrCount UInt32                             DEFAULT 0,
    ScopeSchemaUrl     String                                CODEC(ZSTD(1)),
    ServiceName        LowCardinality(String)                CODEC(ZSTD(1)),
    MetricName         String                                CODEC(ZSTD(1)),
    MetricDescription  String                                CODEC(ZSTD(1)),
    MetricUnit         String                                CODEC(ZSTD(1)),
    Attributes         Map(LowCardinality(String), String)   CODEC(ZSTD(1)),
    StartTimeUnix      DateTime64(9)                         CODEC(Delta, ZSTD(1)),
    TimeUnix           DateTime64(9)                         CODEC(Delta, ZSTD(1)),
    Value              Float64                               CODEC(ZSTD(1)),
    Flags              UInt32                                DEFAULT 0,
    Exemplars Nested (
        FilteredAttributes Map(LowCardinality(String), String),
        TimeUnix           DateTime64(9),
        Value              Float64,
        SpanId             String,
        TraceId            String
    ) CODEC(ZSTD(1))
) ENGINE = MergeTree()
PARTITION BY toDate(TimeUnix)
ORDER BY (ServiceName, MetricName, Attributes, toUnixTimestamp(TimeUnix))
SETTINGS index_granularity = 8192;

-- ── Eval Runs ──────────────────────────────────────────────
-- A run is a group of traces produced by one benchmark execution
-- or tagged production period.
CREATE TABLE IF NOT EXISTS otel.eval_runs (
    run_id          UUID DEFAULT generateUUIDv4(),
    name            String,
    suite           String        DEFAULT '',
    agent_version   String        DEFAULT '',
    dataset_version String        DEFAULT '',
    created_at      DateTime      DEFAULT now(),
    is_baseline     UInt8         DEFAULT 0,
    metadata        Map(String, String) DEFAULT map()
) ENGINE = MergeTree()
ORDER BY (created_at, run_id);

-- ── Eval Scores ────────────────────────────────────────────
-- Written by the eval runner after each completed trace.
CREATE TABLE IF NOT EXISTS otel.eval_scores (
    id              UUID     DEFAULT generateUUIDv4(),
    trace_id        String,
    run_id          UUID,
    span_id         String   DEFAULT '',
    evaluator       String,
    metric          String,
    score           Float32,
    reasoning       String   DEFAULT '',
    eval_type       String,   -- 'deterministic' | 'embedding' | 'llm_judge'
    evaluated_at    DateTime  DEFAULT now()
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(evaluated_at)
ORDER BY (run_id, trace_id, evaluator, evaluated_at);

-- ── Benchmarks ─────────────────────────────────────────────
-- Test cases for offline evaluation.
CREATE TABLE IF NOT EXISTS otel.benchmarks (
    benchmark_id     UUID     DEFAULT generateUUIDv4(),
    suite            String,
    name             String,
    task_input       String,
    expected_output  String   DEFAULT '',
    rubric           String   DEFAULT '',  -- JSON rubric for LLM judges
    difficulty       String   DEFAULT 'medium',
    tags             Array(String) DEFAULT [],
    dataset_version  String   DEFAULT 'v1',
    active           UInt8    DEFAULT 1,
    created_at       DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (suite, benchmark_id);

-- ── Human Reviews ──────────────────────────────────────────
-- Human feedback on eval scores (calibration + ground truth).
CREATE TABLE IF NOT EXISTS otel.human_reviews (
    id              UUID     DEFAULT generateUUIDv4(),
    score_id        UUID,
    trace_id        String,
    metric          String,
    human_score     Float32,
    auto_score      Float32,
    notes           String   DEFAULT '',
    reviewer        String   DEFAULT '',
    reviewed_at     DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (reviewed_at, trace_id);

-- ── Materialized View: Per-Run Metric Summary ──────────────
-- Pre-aggregated for fast Grafana queries.
CREATE MATERIALIZED VIEW IF NOT EXISTS otel.eval_run_summary
ENGINE = AggregatingMergeTree()
PARTITION BY tuple()
ORDER BY (run_id, metric, evaluator)
AS SELECT
    run_id,
    metric,
    evaluator,
    eval_type,
    countState()              AS sample_count,
    avgState(score)           AS avg_score,
    minState(score)           AS min_score,
    maxState(score)           AS max_score,
    quantileState(0.5)(score) AS p50_score,
    quantileState(0.95)(score) AS p95_score
FROM otel.eval_scores
GROUP BY run_id, metric, evaluator, eval_type;

-- ── Prompt Evaluations ─────────────────────────────────────
-- One row per agent invocation: captures prompt, response, model,
-- and all computed metric scores.  Primary table for prompt optimization.
CREATE TABLE IF NOT EXISTS otel.prompt_evals (
    prompt_eval_id   String   DEFAULT generateUUIDv4(),
    prompt_hash      String,                              -- SHA256 of prompt_text for dedup/grouping
    trace_id         String,
    span_id          String   DEFAULT '',
    run_id           String   DEFAULT '',
    agent_name       LowCardinality(String),
    model            LowCardinality(String) DEFAULT '',
    prompt_text      String,                              -- full user-facing input
    response_text    String,                              -- full agent response
    latency_ms       UInt32   DEFAULT 0,
    prompt_tokens    UInt32   DEFAULT 0,
    completion_tokens UInt32  DEFAULT 0,
    scores           Map(String, Float32) DEFAULT map(), -- metric_name → score
    created_at       DateTime64(3) DEFAULT now64(3)
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (agent_name, created_at, trace_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS otel.run_executions (
    execution_id     String        DEFAULT generateUUIDv4(),
    run_id           String,
    run_name         String        DEFAULT '',
    benchmark_id     String        DEFAULT '',
    benchmark_name   String        DEFAULT '',
    trace_id         String        DEFAULT '',
    status           LowCardinality(String) DEFAULT 'ok',   -- ok | error
    error_detail     String        DEFAULT '',
    started_at       DateTime64(3),
    ended_at         DateTime64(3),
    duration_ms      UInt32        DEFAULT 0,
    source           LowCardinality(String) DEFAULT 'benchmark'
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(started_at)
ORDER BY (run_id, started_at)
SETTINGS index_granularity = 8192;

-- ── Alert Thresholds ──────────────────────────────────────
-- One row per metric. ReplacingMergeTree deduplicates on (metric) so
-- an upsert is just a new INSERT — the latest row wins after merging.
CREATE TABLE IF NOT EXISTS otel.alert_thresholds (
    metric      String,
    operator    LowCardinality(String) DEFAULT 'lt',  -- lt | gt | lte | gte
    threshold   Float32,
    enabled     UInt8    DEFAULT 1,
    updated_at  DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY metric;

-- ── Governance Tables ─────────────────────────────────────────────────────────

-- Per-span and per-trace governance metric snapshots.
-- Rows are written by GovernanceRunner after every trace evaluation.
-- span_id = "" means the row is a trace-level aggregate.
CREATE TABLE IF NOT EXISTS otel.gov_metric_snapshots (
    trace_id    String,
    span_id     String        DEFAULT '',
    run_id      String        DEFAULT '',
    metric      String,
    value       Float32,
    detail      String        DEFAULT '',   -- JSON with context (PII types, missing attrs)
    ts          DateTime      DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (trace_id, metric, ts)
SETTINGS index_granularity = 8192;

-- Policy gate evaluation results.
-- One row per (trace_id, metric, policy_rule) per governance run.
CREATE TABLE IF NOT EXISTS otel.gov_policy_decisions (
    decision_id String        DEFAULT generateUUIDv4(),
    trace_id    String        DEFAULT '',
    run_id      String        DEFAULT '',
    metric      LowCardinality(String),
    decision    LowCardinality(String),   -- pass | warn | block
    value       Float32,
    threshold   Float32,
    message     String        DEFAULT '',
    ts          DateTime      DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (run_id, trace_id, metric, ts)
SETTINGS index_granularity = 8192;

-- Immutable append-only audit trail for all governance events.
CREATE TABLE IF NOT EXISTS otel.gov_audit_log (
    event_id    String        DEFAULT generateUUIDv4(),
    trace_id    String        DEFAULT '',
    span_id     String        DEFAULT '',
    run_id      String        DEFAULT '',
    event_type  String,                    -- governance_eval | hitl_request | policy_override
    detail      String        DEFAULT '',  -- JSON
    ts          DateTime      DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (trace_id, event_type, ts)
SETTINGS index_granularity = 8192;

-- Human-in-the-loop review queue.
-- Phase 1: populated post-hoc for review visibility.
-- Phase 2: agent runners check this before executing high-risk actions.
-- decided_at uses epoch sentinel ('1970-01-01') to avoid Nullable.
CREATE TABLE IF NOT EXISTS otel.gov_hitl_queue (
    request_id  String        DEFAULT generateUUIDv4(),
    trace_id    String,
    span_id     String        DEFAULT '',
    run_id      String        DEFAULT '',
    risk_tier   LowCardinality(String) DEFAULT 'low',   -- low | medium | high | critical
    action_type String        DEFAULT '',
    payload     String        DEFAULT '',               -- JSON context
    status      LowCardinality(String) DEFAULT 'pending', -- pending | approved | rejected | auto_approved
    reviewer    String        DEFAULT '',
    notes       String        DEFAULT '',
    created_at  DateTime      DEFAULT now(),
    decided_at  DateTime      DEFAULT '1970-01-01 00:00:00'
) ENGINE = MergeTree()
ORDER BY (status, risk_tier, created_at)
SETTINGS index_granularity = 8192;

-- ── Governance Phase 2 Tables ────────────────────────────────────────────────

-- Per-agent daily token budget configuration.
-- ReplacingMergeTree deduplicates on agent_role; latest row wins after merge.
CREATE TABLE IF NOT EXISTS otel.gov_token_budgets (
    agent_role        String,
    daily_token_limit UInt64   DEFAULT 0,
    enabled           UInt8    DEFAULT 1,
    cost_usd_limit    Float32  DEFAULT 0.0,
    updated_at        DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY agent_role;

-- Prompt snapshot hash history per template.
-- Tracks baseline and drift detection per (template_id, snapshot_hash).
CREATE TABLE IF NOT EXISTS otel.gov_prompt_drift (
    template_id    String,
    snapshot_hash  String,
    is_baseline    UInt8    DEFAULT 0,
    drift_detected UInt8    DEFAULT 0,
    first_seen     DateTime DEFAULT now(),
    last_seen      DateTime DEFAULT now(),
    seen_count     UInt64   DEFAULT 1
) ENGINE = ReplacingMergeTree(last_seen)
ORDER BY (template_id, snapshot_hash);

-- Model routing decision log — one row per evaluated trace.
CREATE TABLE IF NOT EXISTS otel.gov_routing_decisions (
    decision_id           String  DEFAULT generateUUIDv4(),
    trace_id              String  DEFAULT '',
    run_id                String  DEFAULT '',
    agent_role            String  DEFAULT '',
    complexity_tier       LowCardinality(String),
    model                 String,
    input_chars           UInt32  DEFAULT 0,
    estimated_savings_pct Float32 DEFAULT 0.0,
    ts                    DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (trace_id, ts)
SETTINGS index_granularity = 8192;

-- Model routing table configuration.
-- ReplacingMergeTree deduplicates on complexity_tier; latest row wins.
CREATE TABLE IF NOT EXISTS otel.gov_model_routing_config (
    complexity_tier   LowCardinality(String),
    model             String,
    cost_per_1m_input Float32  DEFAULT 3.0,
    max_input_tokens  UInt32   DEFAULT 16000,
    enabled           UInt8    DEFAULT 1,
    updated_at        DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY complexity_tier;

-- ── Governance Phase 3 Tables ────────────────────────────────────────────────

-- Configurable policy rules (editable from UI; ReplacingMergeTree on policy_id).
CREATE TABLE IF NOT EXISTS otel.gov_policies (
    policy_id    String        DEFAULT generateUUIDv4(),
    metric       String,
    threshold    Float32,
    direction    LowCardinality(String) DEFAULT 'gt',   -- gt | lt | gte | lte
    decision     LowCardinality(String),                -- warn | block
    scope        LowCardinality(String) DEFAULT 'global', -- global | agent_role | service
    scope_value  String        DEFAULT '',
    enabled      UInt8         DEFAULT 1,
    description  String        DEFAULT '',
    created_at   DateTime      DEFAULT now(),
    updated_at   DateTime      DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY policy_id;

-- Per-agent behavioral baselines for anomaly detection.
CREATE TABLE IF NOT EXISTS otel.gov_agent_baselines (
    agent_role   String,
    metric       String,
    mean         Float32  DEFAULT 0.0,
    stddev       Float32  DEFAULT 0.0,
    sample_count UInt32   DEFAULT 0,
    window_days  UInt8    DEFAULT 7,
    computed_at  DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(computed_at)
ORDER BY (agent_role, metric);

-- Anomaly detection events — statistical deviations from baseline.
CREATE TABLE IF NOT EXISTS otel.gov_anomaly_events (
    anomaly_id       String  DEFAULT generateUUIDv4(),
    trace_id         String  DEFAULT '',
    run_id           String  DEFAULT '',
    agent_role       String  DEFAULT '',
    metric           String,
    observed_value   Float32,
    baseline_mean    Float32,
    baseline_stddev  Float32,
    z_score          Float32,
    severity         LowCardinality(String) DEFAULT 'medium',
    ts               DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (ts, agent_role, metric)
SETTINGS index_granularity = 8192;

-- Automated remediation action log.
CREATE TABLE IF NOT EXISTS otel.gov_remediation_log (
    action_id   String  DEFAULT generateUUIDv4(),
    trace_id    String  DEFAULT '',
    run_id      String  DEFAULT '',
    trigger     String,
    action_type String,
    detail      String  DEFAULT '',
    ts          DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (ts, trigger)
SETTINGS index_granularity = 8192;

-- Compliance evidence reports (full JSON payload).
CREATE TABLE IF NOT EXISTS otel.gov_compliance_reports (
    report_id     String DEFAULT generateUUIDv4(),
    report_type   String DEFAULT 'summary',
    from_ts       DateTime,
    to_ts         DateTime,
    generated_by  String DEFAULT '',
    payload       String DEFAULT '',
    created_at    DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (created_at, report_type)
SETTINGS index_granularity = 8192;

-- Agent API keys (hashed; plaintext never stored).
CREATE TABLE IF NOT EXISTS otel.gov_agent_keys (
    key_id       String  DEFAULT generateUUIDv4(),
    agent_role   String,
    key_hash     String,
    key_prefix   String  DEFAULT '',
    enabled      UInt8   DEFAULT 1,
    created_at   DateTime DEFAULT now(),
    last_used_at DateTime DEFAULT '1970-01-01 00:00:00',
    revoked_at   DateTime DEFAULT '1970-01-01 00:00:00'
) ENGINE = ReplacingMergeTree(revoked_at)
ORDER BY (agent_role, key_id);

-- Webhook notification configurations.
CREATE TABLE IF NOT EXISTS otel.gov_webhook_configs (
    webhook_id String  DEFAULT generateUUIDv4(),
    name       String,
    url        String,
    events     Array(String) DEFAULT [],
    enabled    UInt8   DEFAULT 1,
    secret     String  DEFAULT '',
    updated_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY webhook_id;

-- ── Governance Phase 4 Tables ────────────────────────────────────────────────

-- Category 4: Safety events (prompt injection, jailbreak, toxic, bias, guardrail)
CREATE TABLE IF NOT EXISTS otel.gov_safety_events (
    event_id     String DEFAULT generateUUIDv4(),
    trace_id     String DEFAULT '',
    span_id      String DEFAULT '',
    run_id       String DEFAULT '',
    agent_role   String DEFAULT '',
    event_type   LowCardinality(String),
    detected     UInt8  DEFAULT 0,
    pattern_name String DEFAULT '',
    confidence   Float32 DEFAULT 0.0,
    detail       String DEFAULT '',
    ts           DateTime DEFAULT now()
) ENGINE = MergeTree() ORDER BY (trace_id, event_type, ts);

-- Category 4: Safety guardrail rules (pattern config, UI-editable)
CREATE TABLE IF NOT EXISTS otel.gov_safety_rules (
    rule_id      String DEFAULT generateUUIDv4(),
    rule_type    LowCardinality(String),
    pattern      String,
    severity     LowCardinality(String) DEFAULT 'high',
    enabled      UInt8 DEFAULT 1,
    description  String DEFAULT '',
    updated_at   DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY (rule_type, rule_id);

-- Category 2: Identity/credential/supply-chain events
CREATE TABLE IF NOT EXISTS otel.gov_identity_events (
    event_id        String DEFAULT generateUUIDv4(),
    trace_id        String DEFAULT '',
    span_id         String DEFAULT '',
    run_id          String DEFAULT '',
    agent_role      String DEFAULT '',
    event_type      LowCardinality(String),
    severity        LowCardinality(String) DEFAULT 'high',
    detail          String DEFAULT '',
    ts              DateTime DEFAULT now()
) ENGINE = MergeTree() ORDER BY (trace_id, event_type, ts);

-- Category 2: Supply chain artifact registry
CREATE TABLE IF NOT EXISTS otel.gov_supply_chain_registry (
    artifact_id   String DEFAULT generateUUIDv4(),
    artifact_type LowCardinality(String),
    artifact_name String,
    expected_hash String DEFAULT '',
    hash_algo     LowCardinality(String) DEFAULT 'sha256',
    verified      UInt8 DEFAULT 1,
    updated_at    DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY (artifact_type, artifact_name);

-- Category 2+7: Tool whitelist per agent role
CREATE TABLE IF NOT EXISTS otel.gov_agent_tool_whitelist (
    agent_role    String,
    tool_name     String,
    allowed       UInt8 DEFAULT 1,
    updated_at    DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY (agent_role, tool_name);

-- Category 6: SRE reliability metrics per agent per time window
CREATE TABLE IF NOT EXISTS otel.gov_reliability_metrics (
    metric_id             String DEFAULT generateUUIDv4(),
    agent_role            String DEFAULT '',
    window_start          DateTime DEFAULT now(),
    window_end            DateTime DEFAULT now(),
    total_spans           UInt32 DEFAULT 0,
    error_spans           UInt32 DEFAULT 0,
    error_rate            Float32 DEFAULT 0.0,
    p50_ms                Float32 DEFAULT 0.0,
    p95_ms                Float32 DEFAULT 0.0,
    p99_ms                Float32 DEFAULT 0.0,
    availability          Float32 DEFAULT 1.0,
    error_budget_consumed Float32 DEFAULT 0.0,
    graceful_count        UInt32 DEFAULT 0,
    degraded_count        UInt32 DEFAULT 0,
    ts                    DateTime DEFAULT now()
) ENGINE = MergeTree() ORDER BY (agent_role, window_start);

-- Category 6: SLO configuration per agent
CREATE TABLE IF NOT EXISTS otel.gov_slo_config (
    agent_role           String,
    target_availability  Float32 DEFAULT 0.999,
    error_budget_pct     Float32 DEFAULT 0.001,
    slo_window_days      UInt16  DEFAULT 30,
    p95_target_ms        Float32 DEFAULT 3000.0,
    p99_target_ms        Float32 DEFAULT 10000.0,
    enabled              UInt8   DEFAULT 1,
    updated_at           DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY agent_role;

-- Category 6+12: Incident lifecycle tracking
CREATE TABLE IF NOT EXISTS otel.gov_incidents (
    incident_id    String DEFAULT generateUUIDv4(),
    agent_role     String DEFAULT '',
    incident_type  LowCardinality(String),
    severity       LowCardinality(String) DEFAULT 'p2',
    trigger_event  String DEFAULT '',
    opened_at      DateTime DEFAULT now(),
    detected_at    DateTime DEFAULT now(),
    contained_at   DateTime DEFAULT '1970-01-01 00:00:00',
    resolved_at    DateTime DEFAULT '1970-01-01 00:00:00',
    status         LowCardinality(String) DEFAULT 'open',
    root_cause     String DEFAULT '',
    recurrence_of  String DEFAULT '',
    notified_at    DateTime DEFAULT '1970-01-01 00:00:00',
    detail         String DEFAULT ''
) ENGINE = MergeTree() ORDER BY (agent_role, opened_at);

-- Category 7: Behavioral events (scope violations, OOD, persona drift)
CREATE TABLE IF NOT EXISTS otel.gov_behavior_events (
    event_id   String DEFAULT generateUUIDv4(),
    trace_id   String DEFAULT '',
    span_id    String DEFAULT '',
    run_id     String DEFAULT '',
    agent_role String DEFAULT '',
    event_type LowCardinality(String),
    detail     String DEFAULT '',
    score      Float32 DEFAULT 0.0,
    ts         DateTime DEFAULT now()
) ENGINE = MergeTree() ORDER BY (trace_id, event_type, ts);

-- Category 7: Persona configuration per agent
CREATE TABLE IF NOT EXISTS otel.gov_persona_config (
    agent_role           String,
    authorized_topics    Array(String) DEFAULT [],
    persona_description  String DEFAULT '',
    enabled              UInt8 DEFAULT 1,
    updated_at           DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY agent_role;

-- Category 8: Model/provider license and compliance registry
CREATE TABLE IF NOT EXISTS otel.gov_model_registry (
    registry_id      String DEFAULT generateUUIDv4(),
    model_name       String,
    model_version    String DEFAULT '',
    provider         String DEFAULT '',
    license_type     LowCardinality(String) DEFAULT 'proprietary',
    commercial_ok    UInt8 DEFAULT 1,
    dpa_signed       UInt8 DEFAULT 0,
    baa_signed       UInt8 DEFAULT 0,
    sectors_allowed  Array(String) DEFAULT [],
    notes            String DEFAULT '',
    updated_at       DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY (model_name, model_version);

-- Category 8+13: Regulatory scope configuration per agent
CREATE TABLE IF NOT EXISTS otel.gov_regulatory_scope (
    scope_id       String DEFAULT generateUUIDv4(),
    agent_role     String DEFAULT '',
    framework      LowCardinality(String),
    enabled        UInt8 DEFAULT 1,
    sector         String DEFAULT '',
    classification String DEFAULT '',
    notes          String DEFAULT '',
    updated_at     DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY (agent_role, framework);

-- Category 11: Change management audit trail
CREATE TABLE IF NOT EXISTS otel.gov_change_log (
    change_id     String DEFAULT generateUUIDv4(),
    agent_role    String DEFAULT '',
    change_type   LowCardinality(String),
    version_from  String DEFAULT '',
    version_to    String DEFAULT '',
    changed_by    String DEFAULT '',
    rollback_plan String DEFAULT '',
    status        LowCardinality(String) DEFAULT 'applied',
    canary_pct    Float32 DEFAULT 0.0,
    notes         String DEFAULT '',
    ts            DateTime DEFAULT now()
) ENGINE = MergeTree() ORDER BY (agent_role, ts);

-- Category 11: Version pinning registry
CREATE TABLE IF NOT EXISTS otel.gov_version_pins (
    agent_role       String,
    model_name       String DEFAULT '',
    pinned_version   String DEFAULT '',
    is_pinned        UInt8 DEFAULT 0,
    deployment_mode  LowCardinality(String) DEFAULT 'production',
    updated_at       DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY (agent_role, model_name);

-- Category 13: Risk register
CREATE TABLE IF NOT EXISTS otel.gov_risk_register (
    risk_id       String DEFAULT generateUUIDv4(),
    title         String DEFAULT '',
    category      String DEFAULT '',
    likelihood    UInt8  DEFAULT 3,
    impact        UInt8  DEFAULT 3,
    risk_score    UInt8  DEFAULT 9,
    owner         String DEFAULT '',
    mitigation    String DEFAULT '',
    status        LowCardinality(String) DEFAULT 'open',
    last_reviewed DateTime DEFAULT now(),
    updated_at    DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY risk_id;

-- Category 13: Compliance scorecard per framework
CREATE TABLE IF NOT EXISTS otel.gov_compliance_scorecard (
    scorecard_id     String DEFAULT generateUUIDv4(),
    agent_role       String DEFAULT '',
    framework        LowCardinality(String),
    total_controls   UInt16 DEFAULT 0,
    passing_controls UInt16 DEFAULT 0,
    score_pct        Float32 DEFAULT 0.0,
    eu_ai_act_class  LowCardinality(String) DEFAULT '',
    detail           String DEFAULT '',
    computed_at      DateTime DEFAULT now()
) ENGINE = MergeTree() ORDER BY (framework, computed_at);

-- Category 14: Configurable threshold registry
CREATE TABLE IF NOT EXISTS otel.gov_threshold_config (
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
SETTINGS index_granularity = 8192;

-- ── Sample Benchmark Data ──────────────────────────────────
INSERT INTO otel.benchmarks (suite, name, task_input, expected_output, rubric, difficulty, tags) VALUES ('unit', 'Simple factual QA', 'What is the capital of France?', 'Paris', '{"criteria": ["correct answer", "concise"]}', 'easy', ['factual', 'geography']);
INSERT INTO otel.benchmarks (suite, name, task_input, expected_output, rubric, difficulty, tags) VALUES ('unit', 'JSON format compliance', 'Return a JSON object with fields: name (string) and age (integer) for a person named Alice who is 30.', '{"name": "Alice", "age": 30}', '{"criteria": ["valid JSON", "correct fields", "correct types"]}', 'easy', ['format', 'json']);
INSERT INTO otel.benchmarks (suite, name, task_input, expected_output, rubric, difficulty, tags) VALUES ('integration', 'Multi-step research task', 'Research the top 3 benefits of exercise and provide a structured summary with citations.', '', '{"criteria": ["covers 3+ benefits", "structured format", "mentions evidence/sources", "coherent"]}', 'medium', ['research', 'summarization']);
INSERT INTO otel.benchmarks (suite, name, task_input, expected_output, rubric, difficulty, tags) VALUES ('collaboration', 'Orchestrator delegation', 'You are an orchestrator. Break this task into subtasks and delegate: Write a blog post about climate change including research, writing, and fact-checking steps.', '', '{"criteria": ["identifies subtasks", "delegates appropriately", "final output coherent", "no redundant steps"]}', 'hard', ['multi-agent', 'orchestration']);
