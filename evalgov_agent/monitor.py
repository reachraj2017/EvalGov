"""Proactive monitor — polls governance signals every 60s, generates RCA findings via Claude."""

import asyncio
import json
import os
from datetime import datetime, timezone

import httpx
import structlog

from db import AgentDB

log = structlog.get_logger()
GOV_URL = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")
POLL_INTERVAL = int(os.getenv("MONITOR_POLL_SECONDS", "60"))
HITL_TIMEOUT_MINUTES = int(os.getenv("HITL_TIMEOUT_MINUTES", "15"))


def _gov(path: str, params: dict | None = None) -> list | dict:
    try:
        r = httpx.get(f"{GOV_URL}{path}", params=params or {}, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


class ProactiveMonitor:
    def __init__(self, db: AgentDB):
        self.db = db
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop())
        log.info("monitor_started", poll_interval_s=POLL_INTERVAL)

    def stop(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        await asyncio.sleep(10)  # warm-up delay
        while True:
            try:
                await self._run_cycle()
            except Exception as exc:
                log.error("monitor_cycle_error", error=str(exc))
            await asyncio.sleep(POLL_INTERVAL)

    async def _run_cycle(self):
        loop = asyncio.get_event_loop()
        anomalies = await loop.run_in_executor(None, self._detect_anomalies)
        for anomaly in anomalies:
            atype = anomaly["type"]
            agent = anomaly.get("agent", "system")
            if self.db.finding_exists_recently(atype, agent, minutes=30):
                continue
            rca_data = await loop.run_in_executor(None, self._generate_rca, anomaly)
            self.db.insert_finding(
                finding_type=atype,
                severity=rca_data.get("severity", anomaly.get("default_severity", "medium")),
                title=anomaly["title"],
                summary=rca_data.get("summary", anomaly.get("summary", "")),
                rca=rca_data.get("rca", ""),
                recommendation=rca_data.get("recommendation", ""),
                signal_data=json.dumps(anomaly.get("signal", {}), default=str),
                affected_agent=agent,
            )
            log.info("finding_created", type=atype, agent=agent, severity=rca_data.get("severity"))

    def _detect_anomalies(self) -> list[dict]:
        anomalies: list[dict] = []

        # 1. Open circuit breakers
        cbs = _gov("/enforcement/circuit-breakers") or []
        for cb in cbs if isinstance(cbs, list) else []:
            if cb.get("state") == "OPEN":
                anomalies.append({
                    "type": "circuit_breaker_open",
                    "agent": cb.get("agent_role", "unknown"),
                    "title": f"Circuit Breaker OPEN: {cb.get('agent_role', 'unknown')}",
                    "summary": f"Agent {cb.get('agent_role')} circuit breaker is OPEN — all actions are blocked.",
                    "default_severity": "critical",
                    "signal": {
                        "state": cb.get("state"),
                        "failure_count": cb.get("failure_count"),
                        "opened_at": str(cb.get("opened_at", "")),
                        "quarantine_reason": cb.get("quarantine_reason", ""),
                    },
                })

        # 2. HITL requests — quality gate entries shown immediately (5-min review window);
        #    all other pending entries shown after HITL_TIMEOUT_MINUTES
        hitl = _gov(f"/hitl/queue?status=pending&limit=50") or []
        now = datetime.now(timezone.utc)
        for req in hitl if isinstance(hitl, list) else []:
            created_raw = str(req.get("created_at", ""))
            try:
                created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                if not created_dt.tzinfo:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                wait_minutes = (now - created_dt).total_seconds() / 60
            except Exception:
                continue

            action_type = str(req.get("action_type", ""))
            payload = req.get("payload", "{}")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            agent = payload.get("agent_role", req.get("run_id", "unknown"))
            ctx   = payload.get("context", {}) if isinstance(payload.get("context"), dict) else {}

            if action_type == "quality_gate_hold":
                # Show quality gate holds immediately — operator has 5 min to approve/forgive
                metric    = ctx.get("metric", "unknown")
                score     = ctx.get("score", 0)
                threshold = ctx.get("threshold", 0)
                query     = ctx.get("query", "")
                rca_hint  = (
                    f"{metric} scored {score:.3f} against threshold {threshold}. "
                    + (f'Prompt: "{query[:120]}"' if query else "No prompt context available.")
                )
                anomalies.append({
                    "type": "quality_gate_hold_pending",
                    "agent": agent,
                    "title": f"Quality Gate Hold Awaiting Review: {agent} [{metric}]",
                    "summary": (
                        f"Agent {agent} has a quality gate hold pending review "
                        f"({int(wait_minutes)}m ago). {rca_hint} "
                        f"Approve to forgive (no CB impact). Reject to confirm failure "
                        f"(counts toward block threshold). Auto-expires in "
                        f"{max(0, 5 - int(wait_minutes))}m."
                    ),
                    "default_severity": "high",
                    "signal": {
                        "request_id":  req.get("request_id"),
                        "metric":      metric,
                        "score":       score,
                        "threshold":   threshold,
                        "wait_minutes": round(wait_minutes, 1),
                        "query":       query[:200] if query else "",
                    },
                })

            elif action_type == "quality_gate_block":
                # Block fires CB failure immediately — show for operator override window
                metric    = ctx.get("metric", "unknown")
                score     = ctx.get("score", 0)
                threshold = ctx.get("threshold", 0)
                anomalies.append({
                    "type": "quality_gate_block_pending",
                    "agent": agent,
                    "title": f"Quality Gate Block — CB Failure Recorded: {agent} [{metric}]",
                    "summary": (
                        f"Agent {agent} quality gate block fired ({int(wait_minutes)}m ago). "
                        f"{metric} scored {score:.3f} (threshold {threshold}). "
                        f"CB failure has been recorded. "
                        f"Approve to override and decrement CB failure count. "
                        f"Reject to confirm — CB failure stands."
                    ),
                    "default_severity": "critical",
                    "signal": {
                        "request_id": req.get("request_id"),
                        "metric":     metric,
                        "score":      score,
                        "threshold":  threshold,
                        "wait_minutes": round(wait_minutes, 1),
                    },
                })

            elif wait_minutes >= HITL_TIMEOUT_MINUTES:
                # Non-quality-gate HITL: show after standard timeout
                anomalies.append({
                    "type": "hitl_timeout",
                    "agent": agent,
                    "title": f"HITL Request Waiting {int(wait_minutes)}m: {agent}",
                    "summary": (
                        f"Agent {agent} is blocked waiting for HITL approval for "
                        f"{int(wait_minutes)} minutes (threshold: {HITL_TIMEOUT_MINUTES}m)."
                    ),
                    "default_severity": "high",
                    "signal": {
                        "request_id":  req.get("request_id"),
                        "risk_tier":   req.get("risk_tier"),
                        "action_type": action_type,
                        "wait_minutes": round(wait_minutes, 1),
                    },
                })

        # 3. Rogue agents with quarantine recommended
        rogue = _gov("/enforcement/rogue-assessments") or []
        for r in rogue if isinstance(rogue, list) else []:
            if r.get("quarantine_recommended"):
                agent = r.get("agent_role", "unknown")
                anomalies.append({
                    "type": "rogue_agent_detected",
                    "agent": agent,
                    "title": f"Rogue Agent Detected: {agent}",
                    "summary": f"Agent {agent} has anomalous behavior patterns — quarantine is recommended by the rogue detection system.",
                    "default_severity": "critical",
                    "signal": {
                        "composite_score": r.get("composite_score"),
                        "frequency_score": r.get("frequency_score"),
                        "entropy_score": r.get("entropy_score"),
                        "capability_score": r.get("capability_score"),
                    },
                })

        # 4. New open incidents
        incidents = _gov("/incidents?status=open&limit=20") or []
        if isinstance(incidents, list):
            for inc in incidents:
                sev = inc.get("severity", "p2")
                if sev in ("p0", "p1"):
                    agent = inc.get("agent_role", "system")
                    anomalies.append({
                        "type": "critical_incident",
                        "agent": agent,
                        "title": f"Open Incident [{sev.upper()}]: {inc.get('incident_type', 'unknown')} — {agent}",
                        "summary": f"A {sev} severity incident of type '{inc.get('incident_type')}' is open for agent {agent}.",
                        "default_severity": "critical" if sev == "p0" else "high",
                        "signal": {
                            "incident_id": inc.get("incident_id"),
                            "incident_type": inc.get("incident_type"),
                            "opened_at": str(inc.get("opened_at", "")),
                            "detail": inc.get("detail", "")[:200],
                        },
                    })

        # 5. Low trust scores (below 0.4)
        trust = _gov("/enforcement/trust-scores") or []
        for t in trust if isinstance(trust, list) else []:
            score = float(t.get("trust_score", 1.0))
            if score < 0.4:
                agent = t.get("agent_role", "unknown")
                anomalies.append({
                    "type": "low_trust_score",
                    "agent": agent,
                    "title": f"Low Trust Score: {agent} ({score:.2f})",
                    "summary": f"Agent {agent} has a trust score of {score:.2f} (threshold: 0.4). This indicates identity, behavior, or compliance issues.",
                    "default_severity": "high",
                    "signal": {
                        "trust_score": score,
                        "trust_tier": t.get("trust_tier"),
                        "identity_score": t.get("identity_score"),
                        "behavior_score": t.get("behavior_score"),
                        "compliance_score": t.get("compliance_score"),
                    },
                })

        # 6. Safety violations (injections, jailbreaks, toxic/bias) — group by agent
        safety = _gov("/safety/events", {"hours": 1, "limit": 100}) or []
        safety_by_agent: dict = {}
        for ev in safety if isinstance(safety, list) else []:
            if not ev.get("detected"):
                continue
            agent = ev.get("agent_role", "system")
            safety_by_agent.setdefault(agent, []).append(ev)
        for agent, evts in safety_by_agent.items():
            types = list({e.get("event_type", "unknown") for e in evts})
            anomalies.append({
                "type": "safety_violation",
                "agent": agent,
                "title": f"Safety Violation: {agent} — {', '.join(types)} ({len(evts)} event{'s' if len(evts) > 1 else ''})",
                "summary": f"Agent {agent} triggered {len(evts)} safety detection(s) in the last hour: {', '.join(types)}.",
                "default_severity": "high",
                "signal": {
                    "event_count": len(evts),
                    "event_types": types,
                    "patterns": list({e.get("pattern_name", "") for e in evts if e.get("pattern_name")}),
                    "sample_detail": evts[0].get("detail", "")[:200] if evts else "",
                },
            })

        # 7. Policy hard blocks — agent triggered a block decision in last hour
        try:
            policy_blocks = self.db._run(
                "SELECT agent_role, count() AS cnt, groupArray(metric)[1] AS sample_metric, "
                "groupArray(message)[1] AS sample_message "
                "FROM otel.gov_policy_decisions "
                "WHERE decision = 'block' AND ts >= now() - INTERVAL 1 HOUR "
                "GROUP BY agent_role ORDER BY cnt DESC LIMIT 20"
            )
            for row in policy_blocks or []:
                agent = row.get("agent_role", "system")
                anomalies.append({
                    "type": "policy_block",
                    "agent": agent,
                    "title": f"Policy Hard Block: {agent} ({int(row.get('cnt', 1))} block{'s' if int(row.get('cnt', 1)) > 1 else ''} in last hour)",
                    "summary": f"Agent {agent} was hard-blocked by the policy engine {row.get('cnt')} time(s) in the last hour. Metric: {row.get('sample_metric', 'unknown')}.",
                    "default_severity": "high",
                    "signal": {
                        "block_count": row.get("cnt"),
                        "sample_metric": row.get("sample_metric"),
                        "sample_message": row.get("sample_message", "")[:200],
                    },
                })
        except Exception as exc:
            log.warning("monitor_policy_blocks_failed", error=str(exc))

        # 8. Statistical anomalies — z-score >= 3 in last hour (significant deviation)
        stat_anomalies = _gov("/anomalies", {"hours": 1, "limit": 100}) or []
        stat_by_agent: dict = {}
        for ev in stat_anomalies if isinstance(stat_anomalies, list) else []:
            if float(ev.get("z_score", 0) or 0) < 3.0:
                continue
            agent = ev.get("agent_role", "system")
            stat_by_agent.setdefault(agent, []).append(ev)
        for agent, evts in stat_by_agent.items():
            metrics = list({e.get("metric", "unknown") for e in evts})
            worst = max(evts, key=lambda e: float(e.get("z_score", 0) or 0))
            anomalies.append({
                "type": "metric_anomaly",
                "agent": agent,
                "title": f"Metric Anomaly: {agent} — {', '.join(metrics[:3])} (z={float(worst.get('z_score', 0)):.1f})",
                "summary": f"Agent {agent} has {len(evts)} metric(s) with statistically significant deviations (z≥3) in the last hour: {', '.join(metrics[:3])}.",
                "default_severity": "medium",
                "signal": {
                    "anomaly_count": len(evts),
                    "metrics": metrics,
                    "worst_metric": worst.get("metric"),
                    "worst_z_score": float(worst.get("z_score", 0) or 0),
                    "worst_observed": worst.get("observed_value"),
                    "worst_baseline": worst.get("baseline_mean"),
                },
            })

        # 9. Behavior scope violations in last hour
        behavior = _gov("/behavior/events", {"hours": 1, "limit": 100}) or []
        behavior_by_agent: dict = {}
        for ev in behavior if isinstance(behavior, list) else []:
            if ev.get("event_type") not in ("scope_violation", "policy_violation", "capability_abuse"):
                continue
            agent = ev.get("agent_role", "system")
            behavior_by_agent.setdefault(agent, []).append(ev)
        for agent, evts in behavior_by_agent.items():
            types = list({e.get("event_type", "unknown") for e in evts})
            anomalies.append({
                "type": "behavior_violation",
                "agent": agent,
                "title": f"Behavior Violation: {agent} — {', '.join(types)} ({len(evts)} event{'s' if len(evts) > 1 else ''})",
                "summary": f"Agent {agent} had {len(evts)} behavior violation(s) in the last hour: {', '.join(types)}.",
                "default_severity": "high",
                "signal": {
                    "event_count": len(evts),
                    "event_types": types,
                    "sample_detail": evts[0].get("detail", "")[:200] if evts else "",
                },
            })

        return anomalies

    def _generate_rca(self, anomaly: dict) -> dict:
        signal_str = json.dumps({
            "anomaly_type": anomaly["type"],
            "affected_agent": anomaly.get("agent"),
            "title": anomaly["title"],
            "description": anomaly.get("summary", ""),
            "signal_data": anomaly.get("signal", {}),
        }, indent=2, default=str)

        try:
            from agent import generate_rca
            return generate_rca(signal_str)
        except Exception as exc:
            log.warning("rca_generation_failed", error=str(exc))
            sev = anomaly.get("default_severity", "medium")
            return {
                "severity": sev,
                "summary": anomaly.get("summary", ""),
                "rca": "Automated RCA unavailable — check signal data.",
                "recommendation": "Review the affected agent's recent traces and governance logs.",
            }
