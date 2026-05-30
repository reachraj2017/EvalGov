"""Automated remediation actions triggered by governance violations.

Actions:
  redact    — replace PII in text with [REDACTED:type] tokens
  throttle  — write a throttle record the agent can poll
  notify    — fire configured webhooks
All actions are logged to gov_remediation_log.
"""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger(__name__)


@dataclass
class RemediationAction:
    trigger:     str   # pii_detected | budget_exceeded | drift_detected | gate_block | anomaly
    action_type: str   # redact | throttle | notify
    detail:      dict


def redact_pii(text: str, patterns: dict) -> tuple[str, list[str]]:
    """Replace PII matches with [REDACTED:type] tokens.

    Returns (redacted_text, list_of_types_redacted).
    """
    redacted_types = []
    result = text
    for pii_type, pattern in patterns.items():
        new = pattern.sub(f"[REDACTED:{pii_type}]", result)
        if new != result:
            redacted_types.append(pii_type)
            result = new
    return result, redacted_types


def build_remediations(
    pii_detected: bool,
    budget_exceeded: list[str],
    drift_detected: list[str],
    anomalies: list,
) -> list[RemediationAction]:
    """Build the list of remediation actions for a governance run result."""
    actions: list[RemediationAction] = []

    if pii_detected:
        actions.append(RemediationAction(
            trigger="pii_detected",
            action_type="notify",
            detail={"message": "PII detected in agent output"},
        ))

    for role in budget_exceeded:
        actions.append(RemediationAction(
            trigger="budget_exceeded",
            action_type="notify",
            detail={"agent_role": role, "message": f"Token budget exceeded for {role}"},
        ))

    for template_id in drift_detected:
        actions.append(RemediationAction(
            trigger="drift_detected",
            action_type="notify",
            detail={"template_id": template_id,
                    "message": f"Prompt drift detected for template {template_id}"},
        ))

    for anomaly in anomalies:
        if anomaly.severity in ("high", "critical"):
            actions.append(RemediationAction(
                trigger="anomaly",
                action_type="notify",
                detail={
                    "agent_role": anomaly.agent_role,
                    "metric":     anomaly.metric,
                    "z_score":    anomaly.z_score,
                    "severity":   anomaly.severity,
                },
            ))

    return actions


def run_remediations(
    db,
    trace_id: str,
    run_id: str,
    actions: list[RemediationAction],
) -> None:
    """Execute and log all remediation actions."""
    for action in actions:
        try:
            if action.action_type == "notify":
                _fire_webhooks(db, action)
            db.execute(
                "INSERT INTO otel.gov_remediation_log "
                "(trace_id, run_id, trigger, action_type, detail) VALUES",
                [(trace_id, run_id, action.trigger, action.action_type,
                  json.dumps(action.detail))],
            )
            log.info("remediation_action_taken", trace_id=trace_id,
                     trigger=action.trigger, action_type=action.action_type)
        except Exception as exc:
            log.error("remediation_action_failed", trigger=action.trigger, error=str(exc))


def _fire_webhooks(db, action: RemediationAction) -> None:
    """Post to all enabled webhook configs that subscribe to this event type."""
    try:
        webhooks = db.fetch_all(
            "SELECT webhook_id, url, secret, events "
            "FROM otel.gov_webhook_configs FINAL WHERE enabled = 1"
        )
    except Exception:
        return

    payload_obj = {
        "event":  action.trigger,
        "detail": action.detail,
        "ts":     int(time.time()),
    }
    payload = json.dumps(payload_obj)

    for wh in webhooks:
        events = list(wh.get("events") or [])
        if action.trigger not in events and "all" not in events:
            continue
        try:
            headers = {"Content-Type": "application/json"}
            secret  = str(wh.get("secret") or "")
            if secret:
                sig = hmac.new(secret.encode(), payload.encode(),
                               hashlib.sha256).hexdigest()
                headers["X-Governance-Signature"] = f"sha256={sig}"

            with httpx.Client(timeout=5.0) as client:
                resp = client.post(str(wh["url"]), content=payload, headers=headers)
            log.info("webhook_fired", url=wh["url"], status=resp.status_code)
        except Exception as exc:
            log.warning("webhook_fire_failed", url=wh.get("url"), error=str(exc))
