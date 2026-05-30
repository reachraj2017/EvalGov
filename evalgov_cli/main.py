#!/usr/bin/env python3
"""evalgov — CLI for the EvalGov Intelligence Agent.

Usage:
    evalgov health
    evalgov ask "what's wrong right now?"
    evalgov hitl list [--status pending]
    evalgov hitl approve <request_id> [--reviewer NAME] [--notes TEXT]
    evalgov hitl reject  <request_id> --notes TEXT [--reviewer NAME]
    evalgov incidents list [--status open]
    evalgov incidents resolve <incident_id>
    evalgov cb list [--agent ROLE]
    evalgov cb reset <agent_role>
    evalgov cb quarantine <agent_role> --reason TEXT
    evalgov agents status
    evalgov trust [--agent ROLE]
    evalgov rogue
    evalgov findings list [--severity critical] [--hours 24]
    evalgov findings ack <finding_id> [--by NAME]
    evalgov traces [--agent ROLE] [--hours 6]
    evalgov costs [--hours 24]
    evalgov scores [--hours 24]
"""

import json
import os
import sys
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table
from rich import print as rprint

app = typer.Typer(help="EvalGov Intelligence Agent CLI", no_args_is_help=True)
console = Console()

AGENT_URL = os.getenv("EVALGOV_AGENT_URL", "http://localhost:8003")
GOV_URL   = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")


def _get(url: str, params: dict | None = None) -> dict | list | None:
    try:
        r = httpx.get(url, params=params or {}, timeout=15)
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to {url}[/red]")
        sys.exit(1)
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return None


def _put(url: str, body: dict) -> dict | None:
    try:
        r = httpx.put(url, json=body, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return None


def _post(url: str, body: dict | None = None) -> dict | None:
    try:
        r = httpx.post(url, json=body or {}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return None


def _json_out(data):
    rprint(json.dumps(data, indent=2, default=str))


# ── health ────────────────────────────────────────────────────────────────────

@app.command()
def health():
    """Check EvalGov Agent and Governance Service health."""
    agent = _get(f"{AGENT_URL}/health")
    gov   = _get(f"{GOV_URL}/health")
    t = Table(title="Service Health")
    t.add_column("Service")
    t.add_column("Status")
    t.add_row("EvalGov Agent",       "[green]OK[/green]" if agent else "[red]DOWN[/red]")
    t.add_row("Governance Service",  "[green]OK[/green]" if gov   else "[red]DOWN[/red]")
    console.print(t)


# ── ask ───────────────────────────────────────────────────────────────────────

@app.command()
def ask(
    question: str = typer.Argument(..., help="Natural language question for the agent"),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON"),
):
    """Ask the EvalGov agent a question in plain English."""
    console.print(f"[dim]Asking agent: {question}[/dim]")
    data = _post(f"{AGENT_URL}/chat", {"message": question, "history": []})
    if not data:
        return
    if json_out:
        _json_out(data)
    else:
        console.print(f"\n[bold]Agent:[/bold] {data.get('response', '')}")
        calls = data.get("tool_calls", [])
        if calls:
            console.print(f"\n[dim]Tools used: {', '.join(c['name'] for c in calls)}[/dim]")


# ── hitl ──────────────────────────────────────────────────────────────────────

hitl_app = typer.Typer(help="HITL request management")
app.add_typer(hitl_app, name="hitl")


@hitl_app.command("list")
def hitl_list(
    status: str = typer.Option("pending", "--status", "-s", help="pending|approved|rejected|all"),
    hours:  int = typer.Option(24, "--hours", "-h"),
    json_out: bool = typer.Option(False, "--json"),
):
    """List HITL requests."""
    params: dict = {"limit": 100, "hours": hours}
    if status != "all":
        params["status"] = status
    data = _get(f"{GOV_URL}/hitl/queue", params) or []
    if json_out:
        _json_out(data)
        return
    t = Table(title=f"HITL Queue ({status})")
    for col in ["request_id", "risk_tier", "action_type", "status", "created_at"]:
        t.add_column(col)
    for row in data:
        status_str = row.get("status", "")
        color = {"pending": "yellow", "approved": "green", "rejected": "red"}.get(status_str, "white")
        t.add_row(
            row.get("request_id", "")[:12] + "...",
            row.get("risk_tier", ""),
            row.get("action_type", ""),
            f"[{color}]{status_str}[/{color}]",
            str(row.get("created_at", ""))[:19],
        )
    console.print(t)


@hitl_app.command("approve")
def hitl_approve(
    request_id: str = typer.Argument(...),
    reviewer: str = typer.Option("operator", "--reviewer", "-r"),
    notes: str = typer.Option("Approved via CLI", "--notes", "-n"),
):
    """Approve a pending HITL request."""
    data = _put(f"{GOV_URL}/hitl/{request_id}", {"status": "approved", "reviewer": reviewer, "notes": notes})
    if data:
        console.print(f"[green]✅ Approved {request_id}[/green]")


@hitl_app.command("reject")
def hitl_reject(
    request_id: str = typer.Argument(...),
    notes: str = typer.Option(..., "--notes", "-n", help="Rejection reason (required)"),
    reviewer: str = typer.Option("operator", "--reviewer", "-r"),
):
    """Reject a pending HITL request."""
    data = _put(f"{GOV_URL}/hitl/{request_id}", {"status": "rejected", "reviewer": reviewer, "notes": notes})
    if data:
        console.print(f"[red]🚫 Rejected {request_id}[/red]")


# ── incidents ─────────────────────────────────────────────────────────────────

inc_app = typer.Typer(help="Incident management")
app.add_typer(inc_app, name="incidents")


@inc_app.command("list")
def incidents_list(
    status: str = typer.Option("open", "--status"),
    hours:  int = typer.Option(48, "--hours"),
    json_out: bool = typer.Option(False, "--json"),
):
    """List incidents."""
    params: dict = {"limit": 50, "hours": hours}
    if status:
        params["status"] = status
    data = _get(f"{GOV_URL}/incidents", params) or []
    if isinstance(data, dict):
        data = data.get("incidents", [])
    if json_out:
        _json_out(data)
        return
    t = Table(title=f"Incidents ({status})")
    for col in ["incident_id", "agent_role", "type", "severity", "status", "opened_at"]:
        t.add_column(col)
    for row in data:
        sev = row.get("severity", "p2")
        sev_color = {"p0": "red", "p1": "yellow", "p2": "white", "p3": "dim"}.get(sev, "white")
        t.add_row(
            str(row.get("incident_id", ""))[:12] + "...",
            row.get("agent_role", ""),
            row.get("incident_type", ""),
            f"[{sev_color}]{sev}[/{sev_color}]",
            row.get("status", ""),
            str(row.get("opened_at", ""))[:19],
        )
    console.print(t)


@inc_app.command("resolve")
def incidents_resolve(incident_id: str = typer.Argument(...)):
    """Resolve an incident."""
    data = _put(f"{GOV_URL}/incidents/{incident_id}", {"status": "resolved"})
    if data:
        console.print(f"[green]✅ Resolved {incident_id}[/green]")


# ── cb (circuit breakers) ─────────────────────────────────────────────────────

cb_app = typer.Typer(help="Circuit breaker management")
app.add_typer(cb_app, name="cb")


@cb_app.command("list")
def cb_list(
    agent: str = typer.Option("", "--agent", "-a"),
    json_out: bool = typer.Option(False, "--json"),
):
    """List circuit breaker states."""
    data = _get(f"{GOV_URL}/enforcement/circuit-breakers") or []
    if agent:
        data = [c for c in data if isinstance(c, dict) and c.get("agent_role") == agent]
    if json_out:
        _json_out(data)
        return
    t = Table(title="Circuit Breakers")
    for col in ["agent_role", "state", "failure_count", "opened_at"]:
        t.add_column(col)
    for row in data if isinstance(data, list) else []:
        state = row.get("state", "")
        color = {"CLOSED": "green", "OPEN": "red", "HALF_OPEN": "yellow"}.get(state, "white")
        t.add_row(
            row.get("agent_role", ""),
            f"[{color}]{state}[/{color}]",
            str(row.get("failure_count", 0)),
            str(row.get("opened_at", ""))[:19],
        )
    console.print(t)


@cb_app.command("reset")
def cb_reset(agent_role: str = typer.Argument(...)):
    """Reset (close) a circuit breaker."""
    data = _post(f"{GOV_URL}/enforcement/circuit-breakers/{agent_role}/reset")
    if data is not None:
        console.print(f"[green]✅ CB reset for {agent_role}[/green]")


@cb_app.command("quarantine")
def cb_quarantine(
    agent_role: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason", "-r", help="Quarantine reason"),
):
    """Quarantine an agent (open CB with quarantine flag)."""
    data = _post(f"{GOV_URL}/enforcement/circuit-breakers/{agent_role}/quarantine", {"reason": reason})
    if data is not None:
        console.print(f"[red]🔒 Quarantined {agent_role}[/red]")


# ── agents ────────────────────────────────────────────────────────────────────

@app.command()
def agents():
    """Show agent status summary (trust scores + CB states)."""
    trust = _get(f"{GOV_URL}/enforcement/trust-scores") or []
    cbs = {c["agent_role"]: c for c in (_get(f"{GOV_URL}/enforcement/circuit-breakers") or []) if isinstance(c, dict)}
    t = Table(title="Agent Status")
    for col in ["agent", "trust_score", "tier", "cb_state", "quarantine"]:
        t.add_column(col)
    for row in trust if isinstance(trust, list) else []:
        agent_role = row.get("agent_role", "")
        cb = cbs.get(agent_role, {})
        state = cb.get("state", "CLOSED")
        cb_color = {"CLOSED": "green", "OPEN": "red", "HALF_OPEN": "yellow"}.get(state, "white")
        score = float(row.get("trust_score", 1.0))
        score_color = "green" if score >= 0.7 else "yellow" if score >= 0.4 else "red"
        t.add_row(
            agent_role,
            f"[{score_color}]{score:.2f}[/{score_color}]",
            row.get("trust_tier", ""),
            f"[{cb_color}]{state}[/{cb_color}]",
            "⚠️" if cb.get("quarantine_reason") else "",
        )
    console.print(t)


# ── trust ─────────────────────────────────────────────────────────────────────

@app.command()
def trust(
    agent: str = typer.Option("", "--agent", "-a"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Show trust scores for all agents."""
    data = _get(f"{GOV_URL}/enforcement/trust-scores") or []
    if agent:
        data = [t for t in data if isinstance(t, dict) and t.get("agent_role") == agent]
    if json_out:
        _json_out(data)
        return
    t = Table(title="Trust Scores")
    for col in ["agent_role", "trust_score", "tier", "identity", "behavior", "compliance"]:
        t.add_column(col)
    for row in data if isinstance(data, list) else []:
        score = float(row.get("trust_score", 1.0))
        color = "green" if score >= 0.7 else "yellow" if score >= 0.4 else "red"
        t.add_row(
            row.get("agent_role", ""),
            f"[{color}]{score:.3f}[/{color}]",
            row.get("trust_tier", ""),
            str(round(float(row.get("identity_score", 0)), 3)),
            str(round(float(row.get("behavior_score", 0)), 3)),
            str(round(float(row.get("compliance_score", 0)), 3)),
        )
    console.print(t)


# ── rogue ─────────────────────────────────────────────────────────────────────

@app.command()
def rogue(json_out: bool = typer.Option(False, "--json")):
    """Show rogue agent detection assessments."""
    data = _get(f"{GOV_URL}/enforcement/rogue-assessments") or []
    if json_out:
        _json_out(data)
        return
    t = Table(title="Rogue Assessments")
    for col in ["agent_role", "composite", "frequency", "entropy", "capability", "quarantine"]:
        t.add_column(col)
    for row in data if isinstance(data, list) else []:
        qrec = row.get("quarantine_recommended", False)
        t.add_row(
            row.get("agent_role", ""),
            str(round(float(row.get("composite_score", 0)), 3)),
            str(round(float(row.get("frequency_score", 0)), 3)),
            str(round(float(row.get("entropy_score", 0)), 3)),
            str(round(float(row.get("capability_score", 0)), 3)),
            "[red]YES[/red]" if qrec else "[green]no[/green]",
        )
    console.print(t)


# ── findings ──────────────────────────────────────────────────────────────────

findings_app = typer.Typer(help="Proactive monitor findings")
app.add_typer(findings_app, name="findings")


@findings_app.command("list")
def findings_list(
    severity: str = typer.Option("", "--severity"),
    status: str = typer.Option("active", "--status"),
    hours: int = typer.Option(24, "--hours"),
    json_out: bool = typer.Option(False, "--json"),
):
    """List proactive monitor findings."""
    data = _get(f"{AGENT_URL}/findings", {"hours": hours, "severity": severity, "status": status, "limit": 50})
    findings = data.get("findings", []) if data else []
    if json_out:
        _json_out(findings)
        return
    t = Table(title=f"Findings ({status or 'all'})")
    for col in ["finding_id", "severity", "type", "title", "agent", "created_at"]:
        t.add_column(col)
    for f in findings:
        sev = f.get("severity", "medium")
        sev_color = {"critical": "red", "high": "yellow", "medium": "white", "low": "dim"}.get(sev, "white")
        t.add_row(
            str(f.get("finding_id", ""))[:8] + "...",
            f"[{sev_color}]{sev}[/{sev_color}]",
            f.get("finding_type", ""),
            f.get("title", "")[:50],
            f.get("affected_agent", ""),
            str(f.get("created_at", ""))[:16],
        )
    console.print(t)


@findings_app.command("ack")
def findings_ack(
    finding_id: str = typer.Argument(...),
    by: str = typer.Option("operator", "--by"),
):
    """Acknowledge a finding."""
    data = _post(f"{AGENT_URL}/findings/{finding_id}/acknowledge", {"acknowledged_by": by})
    if data:
        console.print(f"[green]✅ Acknowledged {finding_id}[/green]")


# ── traces ────────────────────────────────────────────────────────────────────

@app.command()
def traces(
    agent: str = typer.Option("", "--agent", "-a"),
    hours: int = typer.Option(6, "--hours"),
    limit: int = typer.Option(20, "--limit"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Show recent traces."""
    data = _post(f"{AGENT_URL}/chat", {
        "message": f"Show me the last {limit} traces{' for agent ' + agent if agent else ''} from the last {hours} hours as JSON",
        "history": [],
    })
    resp = data.get("response", "") if data else "Agent unavailable"
    console.print(resp)


# ── costs ─────────────────────────────────────────────────────────────────────

@app.command()
def costs(hours: int = typer.Option(24, "--hours"), json_out: bool = typer.Option(False, "--json")):
    """Show cost breakdown by agent."""
    data = _post(f"{AGENT_URL}/chat", {
        "message": f"Show me the cost breakdown by agent for the last {hours} hours",
        "history": [],
    })
    resp = data.get("response", "") if data else "Agent unavailable"
    console.print(resp)


# ── scores ────────────────────────────────────────────────────────────────────

@app.command()
def scores(hours: int = typer.Option(24, "--hours")):
    """Show eval scores by metric."""
    data = _post(f"{AGENT_URL}/chat", {
        "message": f"Show me the average eval scores by metric for the last {hours} hours",
        "history": [],
    })
    resp = data.get("response", "") if data else "Agent unavailable"
    console.print(resp)


if __name__ == "__main__":
    app()
