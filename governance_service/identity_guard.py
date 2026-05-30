"""Identity, Access & Authorization checks — Phase 4.

Covers:
  Category 2 — credential exposure in outputs, least-privilege assessment,
                supply chain integrity (model registry), session token TTL.

Key public API:
  run_identity_checks(db, trace_id, run_id, agent_role, spans) → IdentityResult
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# LiteLLM prefixes the model string with a provider namespace (e.g. "openai/gpt-4o-mini").
# OTel GenAI instrumentation strips that prefix before writing gen_ai.request.model,
# so span attributes always contain just the model name. Normalize registry lookups
# the same way so both "openai/gpt-4o-mini" and "gpt-4o-mini" resolve correctly.
_LITELLM_PROVIDERS = {
    "openai", "anthropic", "azure", "cohere", "google", "mistral",
    "groq", "replicate", "together_ai", "huggingface", "bedrock",
    "sagemaker", "vertex_ai", "ollama", "deepseek", "perplexity",
    "ai21", "nlp_cloud", "aleph_alpha", "petals",
}


def _normalize_model_name(name: str) -> str:
    """Strip LiteLLM provider prefix if present (e.g. 'openai/gpt-4o-mini' → 'gpt-4o-mini')."""
    if "/" in name:
        provider, _, model = name.partition("/")
        if provider.lower() in _LITELLM_PROVIDERS:
            return model
    return name

# ──────────────────────────────────────────────────────────────────────────────
# Extended credential patterns
# (complements pii_scanner.py which covers email, SSN, credit card, phone,
#  IP address, AWS key, and generic API key)
# ──────────────────────────────────────────────────────────────────────────────

_CREDENTIAL_PATTERNS: dict[str, re.Pattern] = {
    "github_token":        re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    "slack_token":         re.compile(r"\b(xoxb|xoxp)-[A-Za-z0-9\-]{20,}\b"),
    "jwt_token":           re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
    ),
    "private_key":         re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),
    "azure_conn":          re.compile(
        r"DefaultEndpointsProtocol=https;AccountName="
    ),
    "gcp_service_account": re.compile(r'"type":\s*"service_account"'),
}

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

_SESSION_TTL_THRESHOLD_SECONDS = 3600  # flag tokens with TTL > 1 hour


# ──────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class IdentityResult:
    trace_id:   str
    agent_role: str
    run_id:     str

    # Credential exposure
    cred_exposure_count: int             = 0
    cred_types_found:    list[str]       = field(default_factory=list)

    # Least-privilege
    tools_called:       list[str]        = field(default_factory=list)
    tools_authorized:   list[str]        = field(default_factory=list)
    least_privilege_ratio:    float      = 0.0
    privilege_over_provisioned: bool     = False

    # Supply chain
    supply_chain_violations: int         = 0
    unregistered_artifacts:  list[str]   = field(default_factory=list)

    # Session tokens
    session_token_violations: int        = 0


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _collect_output_texts(span: dict) -> list[str]:
    """Return all output / result text blobs from a span."""
    attrs = span.get("attributes") or {}
    texts: list[str] = []
    for key in (
        "task.output", "gen_ai.completion", "llm.output", "agent.output",
        "tool.result", "tool_result",
    ):
        val = attrs.get(key)
        if val:
            texts.append(str(val))
    # Tool results encoded as JSON array
    raw_tr = attrs.get("tool_results") or attrs.get("tool.results")
    if raw_tr:
        try:
            parsed = json.loads(raw_tr) if isinstance(raw_tr, str) else raw_tr
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        texts.append(str(item.get("content", "") or ""))
                    else:
                        texts.append(str(item))
            else:
                texts.append(str(parsed))
        except (json.JSONDecodeError, TypeError):
            texts.append(str(raw_tr))
    return [t for t in texts if t.strip()]


def _scan_credentials(text: str) -> list[str]:
    """Return list of credential type names found in text."""
    found: list[str] = []
    for cred_type, pattern in _CREDENTIAL_PATTERNS.items():
        if pattern.search(text):
            found.append(cred_type)
    return found


def _extract_tool_name(span: dict) -> Optional[str]:
    """Extract the tool name called in a tool_call span."""
    attrs = span.get("attributes") or {}
    for key in ("tool.name", "tool_name", "gen_ai.tool.name", "agent.tool"):
        val = attrs.get(key)
        if val:
            return str(val).strip()
    # Fall back to parsing span_name like "agent.tool_call:tool_name"
    span_name = str(span.get("span_name", "") or "")
    if ":" in span_name:
        return span_name.split(":", 1)[1].strip()
    return None


def _write_identity_event(
    db,
    trace_id:   str,
    span_id:    str,
    run_id:     str,
    agent_role: str,
    event_type: str,
    severity:   str,
    detail:     str,
) -> None:
    """Insert a row into otel.gov_identity_events."""
    try:
        db.execute(
            "INSERT INTO otel.gov_identity_events "
            "(event_id, trace_id, span_id, run_id, agent_role, "
            "event_type, severity, detail) VALUES",
            [(
                str(uuid.uuid4()),
                trace_id, span_id, run_id, agent_role,
                event_type, severity, detail,
            )],
        )
    except Exception as exc:
        log.warning("identity_event_write_failed", event_type=event_type,
                    span_id=span_id, error=str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# Sub-checks
# ──────────────────────────────────────────────────────────────────────────────

def _check_credential_exposure(
    db,
    trace_id:   str,
    run_id:     str,
    agent_role: str,
    spans:      list[dict],
) -> tuple[int, list[str]]:
    """Scan all span outputs for extended credential patterns.

    Returns (total_exposure_count, unique_cred_types_found).
    """
    total_count     = 0
    all_cred_types: list[str] = []

    for span in spans:
        span_id    = span.get("span_id", "")
        out_texts  = _collect_output_texts(span)
        combined   = "\n".join(out_texts)
        if not combined:
            continue

        found_types = _scan_credentials(combined)
        if not found_types:
            continue

        total_count += 1
        for cred_type in found_types:
            if cred_type not in all_cred_types:
                all_cred_types.append(cred_type)

        _write_identity_event(
            db, trace_id, span_id, run_id, agent_role,
            event_type="credential_exposure",
            severity="critical",
            detail=json.dumps({"cred_types": found_types,
                               "span_name": span.get("span_name", "")}),
        )
        log.warning("credential_exposure_detected",
                    trace_id=trace_id, span_id=span_id,
                    cred_types=found_types)

    return total_count, all_cred_types


def _check_least_privilege(
    db,
    trace_id:   str,
    run_id:     str,
    agent_role: str,
    spans:      list[dict],
    lp_ratio:   float = 0.5,
) -> tuple[list[str], list[str], float, bool]:
    """Compare tools actually called against the agent's tool whitelist.

    Returns (tools_called, tools_authorized, least_privilege_ratio,
             privilege_over_provisioned).
    """
    # 1. Collect tools actually called in this trace
    tools_called: list[str] = []
    for span in spans:
        span_name = str(span.get("span_name", "") or "")
        if "tool" in span_name.lower():
            tool_name = _extract_tool_name(span)
            if tool_name and tool_name not in tools_called:
                tools_called.append(tool_name)

    # 2. Query whitelist for this agent role
    tools_authorized: list[str] = []
    try:
        rows = db.fetch_all(
            "SELECT tool_name FROM otel.gov_agent_tool_whitelist FINAL "
            "WHERE agent_role = %(role)s AND allowed = 1",
            {"role": agent_role},
        )
        tools_authorized = [r["tool_name"] for r in rows if r.get("tool_name")]
    except Exception as exc:
        log.warning("tool_whitelist_query_failed",
                    agent_role=agent_role, error=str(exc))

    if not tools_authorized:
        # No whitelist configured — cannot assess; ratio = 0.0, not over-provisioned
        return tools_called, tools_authorized, 0.0, False

    # 3. Flag any called tool that is NOT on the whitelist
    for tool in tools_called:
        if tool not in tools_authorized:
            _write_identity_event(
                db, trace_id, "", run_id, agent_role,
                event_type="unauthorized_tool_call",
                severity="high",
                detail=json.dumps({"tool": tool,
                                   "authorized_tools": tools_authorized}),
            )
            log.warning("unauthorized_tool_call",
                        trace_id=trace_id, agent_role=agent_role, tool=tool)

    # 4. Compute least-privilege ratio
    #    (authorized - used) / authorized  → 0 = perfectly provisioned,
    #    1 = agent was given all tools but used none
    used_count       = len([t for t in tools_called if t in tools_authorized])
    authorized_count = len(tools_authorized)
    ratio            = (authorized_count - used_count) / authorized_count
    over_provisioned = ratio > lp_ratio

    if over_provisioned:
        _write_identity_event(
            db, trace_id, "", run_id, agent_role,
            event_type="over_provisioned_permissions",
            severity="medium",
            detail=json.dumps({
                "least_privilege_ratio": round(ratio, 4),
                "tools_authorized":      tools_authorized,
                "tools_used":            [t for t in tools_called
                                          if t in tools_authorized],
                "tools_unused":          [t for t in tools_authorized
                                          if t not in tools_called],
            }),
        )

    return tools_called, tools_authorized, round(ratio, 4), over_provisioned


def _check_supply_chain(
    db,
    trace_id:   str,
    run_id:     str,
    agent_role: str,
    spans:      list[dict],
) -> tuple[int, list[str]]:
    """Check model artifacts used in spans against gov_supply_chain_registry.

    Returns (violation_count, unregistered_artifact_names).
    """
    violation_count:      int        = 0
    unregistered_names:   list[str]  = []
    checked_models:       set[str]   = set()

    for span in spans:
        attrs = span.get("attributes") or {}
        span_id = span.get("span_id", "")

        model_name = _normalize_model_name(
            str(attrs.get("gen_ai.request.model", "") or "").strip()
            or str(attrs.get("model_name", "") or "").strip()
            or str(attrs.get("llm.model", "") or "").strip()
        )
        if not model_name or model_name in checked_models:
            continue
        checked_models.add(model_name)

        try:
            row = db.fetch_one(
                "SELECT artifact_name, expected_hash, verified "
                "FROM otel.gov_supply_chain_registry FINAL "
                "WHERE artifact_type = 'model' AND artifact_name = %(name)s",
                {"name": model_name},
            )
        except Exception as exc:
            log.warning("supply_chain_query_failed",
                        model=model_name, error=str(exc))
            continue

        if not row:
            # Model not in registry at all
            violation_count += 1
            unregistered_names.append(model_name)
            _write_identity_event(
                db, trace_id, span_id, run_id, agent_role,
                event_type="supply_chain_violation",
                severity="high",
                detail=json.dumps({"model": model_name,
                                   "reason": "not_in_registry"}),
            )
            log.warning("supply_chain_unregistered_model",
                        trace_id=trace_id, model=model_name)
        elif not int(row.get("verified", 0) or 0):
            # Model registered but hash not verified
            violation_count += 1
            if model_name not in unregistered_names:
                unregistered_names.append(model_name)
            _write_identity_event(
                db, trace_id, span_id, run_id, agent_role,
                event_type="supply_chain_violation",
                severity="high",
                detail=json.dumps({"model": model_name,
                                   "reason": "hash_not_verified",
                                   "expected_hash": row.get("expected_hash", "")}),
            )
            log.warning("supply_chain_unverified_model",
                        trace_id=trace_id, model=model_name)

    return violation_count, unregistered_names


def _check_session_tokens(
    db,
    trace_id:      str,
    run_id:        str,
    agent_role:    str,
    spans:         list[dict],
    ttl_threshold: int = _SESSION_TTL_THRESHOLD_SECONDS,
) -> int:
    """Scan spans for session token TTL attributes that exceed the threshold.

    Returns the count of violating spans.
    """
    violations = 0
    for span in spans:
        attrs   = span.get("attributes") or {}
        span_id = span.get("span_id", "")

        for ttl_key in ("session.token_ttl", "auth.token_expiry", "auth.token_ttl"):
            ttl_val = attrs.get(ttl_key)
            if ttl_val is None:
                continue
            try:
                ttl_seconds = float(ttl_val)
            except (ValueError, TypeError):
                continue

            if ttl_seconds > ttl_threshold:
                violations += 1
                _write_identity_event(
                    db, trace_id, span_id, run_id, agent_role,
                    event_type="session_token_ttl_violation",
                    severity="medium",
                    detail=json.dumps({
                        "ttl_key":     ttl_key,
                        "ttl_seconds": ttl_seconds,
                        "threshold":   ttl_threshold,
                    }),
                )
                log.warning("session_token_ttl_violation",
                            trace_id=trace_id, span_id=span_id,
                            ttl_seconds=ttl_seconds)
                break  # one event per span is enough

    return violations


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────

def run_identity_checks(
    db,
    trace_id:   str,
    run_id:     str,
    agent_role: str,
    spans:      list[dict],
    thresholds: dict = None,
) -> IdentityResult:
    """Run all identity & access checks for the supplied spans.

    Args:
        db:         GovernanceDB instance.
        trace_id:   OTel trace ID.
        run_id:     Governance run ID.
        agent_role: Role of the agent being evaluated.
        spans:      List of span dicts as returned by db.get_spans_for_trace().

    Returns:
        IdentityResult dataclass with all check results.
    """
    t            = thresholds or {}
    _session_ttl = int(t.get("identity.session_ttl_seconds",    3600))
    _lp_ratio    = float(t.get("identity.least_privilege_ratio", 0.5))

    # ── 1. Credential exposure ────────────────────────────────────────────
    cred_count, cred_types = _check_credential_exposure(
        db, trace_id, run_id, agent_role, spans
    )

    # ── 2. Least-privilege assessment ────────────────────────────────────
    tools_called, tools_authorized, lp_ratio, over_provisioned = (
        _check_least_privilege(db, trace_id, run_id, agent_role, spans,
                               lp_ratio=_lp_ratio)
    )

    # ── 3. Supply chain check ─────────────────────────────────────────────
    sc_violations, unregistered = _check_supply_chain(
        db, trace_id, run_id, agent_role, spans
    )

    # ── 4. Session token TTL check ────────────────────────────────────────
    session_violations = _check_session_tokens(
        db, trace_id, run_id, agent_role, spans,
        ttl_threshold=_session_ttl,
    )

    # ── Persist metric snapshots ──────────────────────────────────────────
    _save_metric = lambda metric, value, detail="": db.save_gov_metric(
        trace_id, "", run_id, metric, value, detail
    )

    try:
        _save_metric(
            "cred_exposure_count", float(cred_count),
            json.dumps({"cred_types": cred_types}),
        )
        _save_metric(
            "least_privilege_ratio", lp_ratio,
            json.dumps({
                "tools_called":     tools_called,
                "tools_authorized": tools_authorized,
                "over_provisioned": over_provisioned,
            }),
        )
        supply_compliance = 0.0 if sc_violations > 0 else 1.0
        _save_metric(
            "supply_chain_compliance", supply_compliance,
            json.dumps({
                "violations":             sc_violations,
                "unregistered_artifacts": unregistered,
            }),
        )
    except Exception as exc:
        log.warning("identity_metrics_save_failed",
                    trace_id=trace_id, error=str(exc))

    result = IdentityResult(
        trace_id=trace_id,
        agent_role=agent_role,
        run_id=run_id,
        cred_exposure_count=cred_count,
        cred_types_found=cred_types,
        tools_called=tools_called,
        tools_authorized=tools_authorized,
        least_privilege_ratio=lp_ratio,
        privilege_over_provisioned=over_provisioned,
        supply_chain_violations=sc_violations,
        unregistered_artifacts=unregistered,
        session_token_violations=session_violations,
    )

    log.info(
        "identity_checks_complete",
        trace_id=trace_id,
        agent_role=agent_role,
        cred_exposure_count=cred_count,
        cred_types_found=cred_types,
        least_privilege_ratio=lp_ratio,
        privilege_over_provisioned=over_provisioned,
        supply_chain_violations=sc_violations,
        unregistered_artifacts=unregistered,
        session_token_violations=session_violations,
    )

    return result
