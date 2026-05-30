"""GateClient — agent-side SDK for the pre-execution governance gate.

Agents import this and call gate.check() before executing any sensitive action.
Supports fail-open mode (default) so governance service outages don't break agents.

Usage — low-level:
    from instrumentation.gate_client import GateClient, GateBlockedError

    gate = GateClient()
    try:
        result = gate.check("delete", agent_role="data-analyst",
                            context={"target": "users table"}, trace_id=trace_id)
    except GateBlockedError as e:
        print(f"Blocked: {e}")

Usage — decorator:
    from instrumentation.gate_client import governed

    @governed("send_email", agent_role="emailer-agent")
    def send_invoice(to: str, body: str): ...
"""

import functools
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx
import structlog

log = structlog.get_logger(__name__)

_GOVERNANCE_URL     = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")
_GATE_TIMEOUT       = float(os.getenv("GATE_TIMEOUT_SECONDS",          "5.0"))
_POLL_INTERVAL      = float(os.getenv("GATE_POLL_INTERVAL_SECONDS",    "10.0"))
_APPROVAL_TIMEOUT   = float(os.getenv("GATE_APPROVAL_TIMEOUT_SECONDS", "300.0"))
_FAIL_OPEN          = os.getenv("GATE_FAIL_OPEN", "true").lower() == "true"


@dataclass
class GateResult:
    decision:           str
    risk_tier:          str
    base_tier:          str
    request_id:         str
    requires_approval:  bool
    message:            str
    escalation_reasons: list[str]
    allowed:            bool


class GateBlockedError(Exception):
    """Raised when an action is hard-blocked by the governance gate."""
    def __init__(self, message: str, result: GateResult):
        super().__init__(message)
        self.result = result


class GateApprovalTimeoutError(Exception):
    """Raised when a paused action isn't approved within the timeout window."""


class GateClient:
    """Pre-execution governance gate client."""

    def __init__(
        self,
        governance_url: str | None = None,
        agent_key: str | None = None,
        fail_open: bool = _FAIL_OPEN,
    ) -> None:
        self.base_url  = (governance_url or _GOVERNANCE_URL).rstrip("/")
        self.agent_key = agent_key or os.getenv("GOVERNANCE_AGENT_KEY", "")
        self.fail_open = fail_open

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def check(
        self,
        action_type: str,
        agent_role: str,
        context: dict | None = None,
        trace_id: str = "",
        run_id: str = "",
        wait_for_approval: bool = True,
    ) -> GateResult:
        """
        Check the gate for an action.  Blocks until a decision is reached.

        Raises GateBlockedError if the action must not proceed.
        Raises GateApprovalTimeoutError if pause + no human decision in time.
        Returns GateResult with .allowed = True to proceed.
        """
        try:
            result = self._call_gate(action_type, agent_role,
                                     context or {}, trace_id, run_id)
        except (GateBlockedError, GateApprovalTimeoutError):
            raise
        except Exception as exc:
            log.warning("gate_check_failed", action=action_type, error=str(exc))
            if self.fail_open:
                return GateResult(
                    decision="auto_approve", risk_tier="low", base_tier="low",
                    request_id="", requires_approval=False,
                    message=f"gate_unreachable_fail_open",
                    escalation_reasons=[], allowed=True,
                )
            raise

        if result.decision == "block":
            raise GateBlockedError(
                f"Action '{action_type}' BLOCKED: {result.message}", result
            )

        if result.decision == "pause" and wait_for_approval:
            result = self._await_approval(result)

        return result

    def check_routing(
        self,
        task_input: str,
        tool_count: int = 0,
        is_multi_agent: bool = False,
    ) -> dict:
        """Ask the governance service which model to use for this task."""
        try:
            with httpx.Client(timeout=_GATE_TIMEOUT) as client:
                resp = client.get(
                    f"{self.base_url}/routing/recommend",
                    params={"task_input": task_input,
                            "tool_count": tool_count,
                            "is_multi_agent": is_multi_agent},
                    headers=self._headers(),
                )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.warning("routing_check_failed", error=str(exc))
            return {}

    # ──────────────────────────────────────────────────────────────────────
    # Private helpers
    # ──────────────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.agent_key:
            h["X-Agent-Key"] = self.agent_key
        return h

    def _call_gate(
        self,
        action_type: str,
        agent_role: str,
        context: dict,
        trace_id: str,
        run_id: str,
    ) -> GateResult:
        with httpx.Client(timeout=_GATE_TIMEOUT) as client:
            resp = client.post(
                f"{self.base_url}/gate/check",
                json={"action_type": action_type, "agent_role": agent_role,
                      "context": context, "trace_id": trace_id, "run_id": run_id},
                headers=self._headers(),
            )
        resp.raise_for_status()
        d = resp.json()
        return GateResult(
            decision=d.get("decision", "auto_approve"),
            risk_tier=d.get("risk_tier", "low"),
            base_tier=d.get("base_tier", "low"),
            request_id=d.get("request_id", ""),
            requires_approval=bool(d.get("requires_approval")),
            message=d.get("message", ""),
            escalation_reasons=d.get("escalation_reasons", []),
            allowed=d.get("decision") not in ("block",),
        )

    def _await_approval(self, initial: GateResult) -> GateResult:
        """Poll until approved/rejected/timed-out."""
        rid      = initial.request_id
        deadline = time.monotonic() + _APPROVAL_TIMEOUT
        log.info("gate_waiting_approval", request_id=rid, timeout=_APPROVAL_TIMEOUT)

        while time.monotonic() < deadline:
            try:
                status = self._poll_status(rid)
                if status == "approved":
                    log.info("gate_approval_granted", request_id=rid)
                    return GateResult(**{**initial.__dict__,
                                        "decision": "approved", "allowed": True})
                if status == "rejected":
                    raise GateBlockedError(
                        f"HITL request {rid} rejected by reviewer", initial
                    )
            except (GateBlockedError, GateApprovalTimeoutError):
                raise
            except Exception as exc:
                log.warning("gate_poll_error", error=str(exc))
            time.sleep(_POLL_INTERVAL)

        raise GateApprovalTimeoutError(
            f"Approval for {rid} not received within {_APPROVAL_TIMEOUT}s"
        )

    def _poll_status(self, request_id: str) -> str:
        with httpx.Client(timeout=_GATE_TIMEOUT) as client:
            resp = client.get(f"{self.base_url}/hitl/{request_id}/status",
                              headers=self._headers())
        resp.raise_for_status()
        return resp.json().get("status", "pending")


# ── Module-level singleton ────────────────────────────────────────────────────
_default_gate: Optional[GateClient] = None


def get_gate() -> GateClient:
    """Return the module-level GateClient singleton."""
    global _default_gate
    if _default_gate is None:
        _default_gate = GateClient()
    return _default_gate


def governed(
    action_type: str,
    agent_role: str,
    context_fn: Callable | None = None,
    gate: GateClient | None = None,
) -> Callable:
    """Decorator: run a pre-execution gate check before the decorated function.

    Args:
        action_type:  Gate action type string (e.g. 'delete', 'send_email').
        agent_role:   Agent role string.
        context_fn:   Optional callable(*args, **kwargs) → dict for gate context.
        gate:         Optional GateClient; uses module singleton if not provided.

    Example:
        @governed("delete", agent_role="cleanup-agent")
        def purge_old_records(table: str, before_date: str): ...
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            _gate = gate or get_gate()
            ctx   = {}
            if context_fn:
                try:
                    ctx = context_fn(*args, **kwargs) or {}
                except Exception:
                    pass
            _gate.check(action_type=action_type, agent_role=agent_role, context=ctx)
            return fn(*args, **kwargs)
        return wrapper
    return decorator
