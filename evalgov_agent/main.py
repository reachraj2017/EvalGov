"""EvalGov Intelligence Agent — FastAPI service.

Endpoints:
  GET  /health
  POST /chat              Send a message to the agent
  POST /chat/reset        Reset (clear) conversation (stateless — client manages history)
  GET  /findings          List proactive monitor findings
  POST /findings/{id}/acknowledge   Acknowledge a finding
  POST /findings/{id}/resolve       Resolve a finding
  GET  /mcp/sse           MCP SSE stream for Claude Code / external agents
  POST /mcp/messages/     MCP message handler
"""

import asyncio
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

log = structlog.get_logger()


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    from db import AgentDB

    db = AgentDB()
    db.ensure_tables()
    app.state.db = db
    log.info("evalgov_agent_started")

    yield

    log.info("evalgov_agent_stopped")


app = FastAPI(title="EvalGov Intelligence Agent", lifespan=lifespan)

# ── Mount MCP server (optional — graceful degradation if mcp not installed) ───

try:
    from mcp_server import build_mcp_app
    app.mount("/mcp", build_mcp_app())
    log.info("mcp_server_mounted", path="/mcp")
except Exception as exc:
    log.warning("mcp_server_unavailable", reason=str(exc))


# ── Pydantic models ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []  # [{role, content}] — client sends full history


class ChatResponse(BaseModel):
    response: str
    tool_calls: list[dict] = []


class AckRequest(BaseModel):
    acknowledged_by: str = "operator"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "evalgov-agent"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    from agent import chat as agent_chat
    try:
        response_text, tool_calls = agent_chat(req.message, req.history)
        return ChatResponse(response=response_text, tool_calls=tool_calls)
    except Exception as exc:
        log.error("chat_error", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/findings")
def get_findings(
    hours: int = Query(default=24),
    severity: str = Query(default=""),
    status: str = Query(default="active"),
    limit: int = Query(default=30),
):
    from db import AgentDB
    db: AgentDB = app.state.db
    rows = db.get_findings(hours=hours, severity=severity, status=status, limit=limit)
    return {"findings": rows, "count": len(rows)}


@app.get("/findings/active")
def get_active_findings(hours: int = Query(default=24)):
    db: AgentDB = app.state.db
    rows = db.get_all_active_findings(hours=hours)
    by_severity = {"critical": [], "high": [], "medium": [], "low": []}
    for f in rows:
        sev = f.get("severity", "medium")
        by_severity.setdefault(sev, []).append(f)
    return {"findings": rows, "by_severity": by_severity, "count": len(rows)}


@app.post("/findings/{finding_id}/acknowledge")
def acknowledge_finding(finding_id: str, req: AckRequest):
    db: AgentDB = app.state.db
    db.acknowledge_finding(finding_id, req.acknowledged_by)
    return {"status": "acknowledged", "finding_id": finding_id}


@app.post("/findings/{finding_id}/resolve")
def resolve_finding(finding_id: str):
    db: AgentDB = app.state.db
    db.resolve_finding(finding_id)
    return {"status": "resolved", "finding_id": finding_id}


@app.get("/system-state")
def get_system_state(hours: int = Query(default=1)):
    """Live system state: HITL queue, policy violations, incidents, circuit breakers."""
    db: AgentDB = app.state.db
    return {
        "hitl_queue":        db.get_hitl_queue_live(hours=24, limit=25),
        "policy_violations": db.get_policy_decisions(hours=24, decision="block", limit=25),
        "incidents":         db.get_incidents_live(hours=24, limit=25),
        "circuit_breakers":  db.get_circuit_breakers_live(),
    }


@app.post("/findings/bulk-resolve-quality-gates")
def bulk_resolve_quality_gate_findings():
    """Acknowledge all active quality gate hold and block findings.

    Acknowledges (not resolves) so the monitor does not re-create findings
    on the next cycle for the same agent/type combination.
    """
    db: AgentDB = app.state.db
    count = db.bulk_acknowledge_quality_gate_findings()
    return {"acknowledged": count}
