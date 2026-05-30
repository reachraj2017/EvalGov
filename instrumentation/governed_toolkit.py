"""GovernedToolkit — Phase 2 pre-execution enforcement SDK.

Wraps agent tool calls with a governance gate check before each execution.
Integrates with Phase 1 circuit breaker — if the breaker is OPEN the call
is blocked immediately without a round-trip to the gate service.

Controlled by GOVERNANCE_ENFORCEMENT_ENABLED env var (default: false).
When disabled the toolkit is a transparent pass-through — no latency added.

Usage — wrap a callable:
    from instrumentation.governed_toolkit import GovernedToolkit

    toolkit = GovernedToolkit(agent_role="searcher")
    governed_search = toolkit.wrap("web_search", web_search)
    result = governed_search(query="AI news")

Usage — inline call:
    result = toolkit.call("web_search", web_search, query="AI news")

Usage — ADK before_tool_callback:
    before_cb = toolkit.adk_before_tool_callback()
    agent = LlmAgent(..., before_tool_callback=before_cb)

Gate decisions are stamped as OTel span attributes on the current span:
    gate.decision       allow | block | flag | pause
    gate.risk_tier      low | medium | high | critical
    gate.request_id     HITL queue ID (when decision=pause)
    gate.enforcement    true | false  (whether enforcement was active)
"""

import functools
import logging
import os
import time
from typing import Callable

import httpx
from opentelemetry import trace

log = logging.getLogger(__name__)

_GOVERNANCE_URL  = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")
_GATE_TIMEOUT    = float(os.getenv("GATE_TIMEOUT_SECONDS", "3.0"))
_FAIL_OPEN       = os.getenv("GATE_FAIL_OPEN", "true").lower() == "true"
_HITL_TIMEOUT    = float(os.getenv("HITL_TIMEOUT_SECONDS", "300"))   # 5 min default
_HITL_POLL_SECS  = float(os.getenv("HITL_POLL_SECONDS", "3.0"))

# Cache for circuit breaker state — avoids per-call DB lookup overhead
_CB_CACHE: dict[str, tuple[str, float]] = {}   # role → (state, expires_at)
_CB_CACHE_TTL = 30.0  # seconds


# Tool name → gate action type (controls risk tier in gate.py)
_TOOL_ACTION_MAP: dict[str, str] = {
    "web_search":     "search",
    "search":         "search",
    "fetch":          "fetch",
    "read_file":      "read",
    "list_files":     "list",
    "write_file":     "write",
    "create_file":    "create",
    "update_file":    "update",
    "delete_file":    "delete",
    "execute_code":   "execute_code",
    "run_script":     "execute_code",
    "send_email":     "send_email",
    "send_message":   "send_message",
    "post":           "post",
    "publish":        "publish",
    "deploy":         "deploy",
    "delete":         "delete",
    "remove":         "remove",
    "insert":         "insert",
    "upload":         "upload",
}


class GateBlockedError(Exception):
    """Raised when governance gate hard-blocks a tool call."""
    def __init__(self, message: str, decision: str, risk_tier: str):
        super().__init__(message)
        self.decision  = decision
        self.risk_tier = risk_tier


class GovernedToolkit:
    """
    Pre-execution governance gate wrapper for agent tool calls.

    Args:
        agent_role:      The agent's role string (e.g. "searcher").
        governance_url:  Override for GOVERNANCE_SERVICE_URL env var.
        api_key:         GOVERNANCE_AGENT_KEY for authenticated gate checks.
        fail_open:       If True (default), governance failures are non-blocking.
                         Set False in production to fail closed on service outage.
    """

    def __init__(
        self,
        agent_role: str,
        governance_url: str | None = None,
        api_key: str | None = None,
        fail_open: bool = _FAIL_OPEN,
    ) -> None:
        self.agent_role = agent_role
        self.base_url   = (governance_url or _GOVERNANCE_URL).rstrip("/")
        self.api_key    = api_key or os.getenv("GOVERNANCE_AGENT_KEY", "")
        self.fail_open  = fail_open

    # ── Public API ────────────────────────────────────────────────────────────

    def gate_check(
        self,
        tool_name: str,
        trace_id: str = "",
        run_id: str = "",
        context: dict | None = None,
    ) -> dict:
        """
        Run a pre-execution gate check for tool_name.

        The backend decides whether enforcement is active — no client-side flag
        needed. When Phase 2 is disabled on the backend the response is
        auto_approve; when the circuit breaker is OPEN it is block.

        Returns the gate decision dict.
        Raises GateBlockedError if the gate hard-blocks the action.
        """
        # Fast-path: local circuit breaker cache avoids a round-trip when we
        # already know the breaker is OPEN (refreshes every 30 s).
        cb_state = self._get_circuit_breaker_state()
        if cb_state == "open":
            msg = (
                f"[{self.agent_role}] circuit breaker OPEN — "
                f"tool call '{tool_name}' blocked (cached state)"
            )
            log.warning("gate_cb_block_cached agent_role=%s tool=%s", self.agent_role, tool_name)
            raise GateBlockedError(msg, decision="block", risk_tier="critical")

        action_type = _TOOL_ACTION_MAP.get(tool_name.lower(), "tool_call")

        try:
            result = self._call_gate(action_type, trace_id, run_id, context or {"tool": tool_name})
        except GateBlockedError:
            raise
        except Exception as exc:
            log.warning("gate_check_error agent_role=%s tool=%s error=%s",
                        self.agent_role, tool_name, exc)
            if self.fail_open:
                return {"decision": "auto_approve", "risk_tier": "low",
                        "message": "gate_unreachable_fail_open", "request_id": ""}
            raise

        if result.get("decision") == "block":
            raise GateBlockedError(
                f"[{self.agent_role}] '{tool_name}' BLOCKED: {result.get('message', '')}",
                decision="block",
                risk_tier=result.get("risk_tier", "critical"),
            )

        if result.get("decision") == "pause":
            request_id = result.get("request_id", "")
            log.info("gate_pause agent_role=%s tool=%s request_id=%s",
                     self.agent_role, tool_name, request_id)
            if request_id:
                outcome = self._wait_for_hitl_decision(request_id, tool_name)
                if outcome != "approved":
                    raise GateBlockedError(
                        f"[{self.agent_role}] '{tool_name}' requires human approval "
                        f"— {outcome} (request_id={request_id})",
                        decision="block",
                        risk_tier=result.get("risk_tier", "high"),
                    )
            # approved or no request_id → fall through and allow tool

        return result

    def call(self, tool_name: str, fn: Callable, *args, **kwargs):
        """Gate-check then call fn. Stamps gate.* attributes on current OTel span."""
        result = self.gate_check(tool_name)
        self._stamp_span(result, tool_name)
        return fn(*args, **kwargs)

    def wrap(self, tool_name: str, fn: Callable) -> Callable:
        """Return a governed version of fn that gate-checks before each call."""
        toolkit = self

        @functools.wraps(fn)
        def _governed(*args, **kwargs):
            return toolkit.call(tool_name, fn, *args, **kwargs)

        return _governed

    def adk_before_tool_callback(self) -> Callable:
        """
        Return an ADK-compatible before_tool_callback.

        When the gate blocks, returns a dict so ADK treats it as the tool
        result — the real tool function is never called.

        Usage:
            agent = LlmAgent(
                ...,
                before_tool_callback=toolkit.adk_before_tool_callback(),
            )
        """
        toolkit = self

        def _before_tool_cb(tool, args, tool_context):
            tool_name = getattr(tool, "name", str(tool))

            # Never gate internal ADK routing calls
            if tool_name == "transfer_to_agent":
                return None

            try:
                result = toolkit.gate_check(tool_name, context={"args": args or {}})
                # Stamp attributes on whatever span is current
                span = getattr(tool_context, "_otel_span", None) or trace.get_current_span()
                toolkit._stamp_span(result, tool_name, span=span)
            except GateBlockedError as exc:
                log.warning("adk_tool_blocked agent_role=%s tool=%s reason=%s",
                            toolkit.agent_role, tool_name, exc)
                span = getattr(tool_context, "_otel_span", None) or trace.get_current_span()
                if span and span.is_recording():
                    span.set_attribute("gate.decision", "block")
                    span.set_attribute("gate.enforcement", "true")
                return {"result": f"[GOVERNANCE BLOCK] {exc}"}

            return None  # allow execution to proceed

        return _before_tool_cb

    # ── Private helpers ───────────────────────────────────────────────────────

    def _wait_for_hitl_decision(self, request_id: str, tool_name: str) -> str:
        """Poll governance service until a human approves or rejects the request.

        Returns 'approved', 'rejected', or 'timeout'.
        Blocks the calling thread — keep HITL_TIMEOUT_SECONDS reasonable.
        """
        deadline = time.monotonic() + _HITL_TIMEOUT
        log.info("hitl_waiting agent_role=%s tool=%s request_id=%s timeout_s=%s",
                 self.agent_role, tool_name, request_id, _HITL_TIMEOUT)
        while time.monotonic() < deadline:
            try:
                with httpx.Client(timeout=2.0) as client:
                    resp = client.get(
                        f"{self.base_url}/hitl/{request_id}/status",
                        headers=self._headers(),
                    )
                    if resp.status_code == 200:
                        status = resp.json().get("status", "pending")
                        if status == "approved":
                            log.info("hitl_approved agent_role=%s request_id=%s",
                                     self.agent_role, request_id)
                            return "approved"
                        if status in ("rejected", "expired"):
                            log.warning("hitl_rejected agent_role=%s request_id=%s status=%s",
                                        self.agent_role, request_id, status)
                            return status
            except Exception as exc:
                log.warning("hitl_poll_error agent_role=%s request_id=%s error=%s",
                            self.agent_role, request_id, exc)
            time.sleep(_HITL_POLL_SECS)
        log.warning("hitl_timeout agent_role=%s request_id=%s", self.agent_role, request_id)
        return "timeout"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-Agent-Key"] = self.api_key
        return h

    def _call_gate(
        self,
        action_type: str,
        trace_id: str,
        run_id: str,
        context: dict,
    ) -> dict:
        with httpx.Client(timeout=_GATE_TIMEOUT) as client:
            resp = client.post(
                f"{self.base_url}/gate/check",
                json={
                    "action_type": action_type,
                    "agent_role":  self.agent_role,
                    "context":     context,
                    "trace_id":    trace_id,
                    "run_id":      run_id,
                },
                headers=self._headers(),
            )
        resp.raise_for_status()
        return resp.json()

    def _get_circuit_breaker_state(self) -> str:
        """Return cached circuit breaker state for this agent."""
        cached = _CB_CACHE.get(self.agent_role)
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(
                    f"{self.base_url}/enforcement/circuit-breakers",
                    headers=self._headers(),
                )
            resp.raise_for_status()
            for row in resp.json():
                if row.get("agent_role") == self.agent_role:
                    state = str(row.get("state", "closed"))
                    _CB_CACHE[self.agent_role] = (state, time.monotonic() + _CB_CACHE_TTL)
                    return state
        except Exception:
            pass
        return "closed"  # assume closed on error

    def _stamp_span(
        self,
        gate_result: dict | None,
        tool_name: str,
        span=None,
    ) -> None:
        """Write gate.* attributes onto the OTel span."""
        s = span or trace.get_current_span()
        if not s or not s.is_recording():
            return
        s.set_attribute("gate.enforcement", "true")
        if gate_result:
            s.set_attribute("gate.decision",    gate_result.get("decision", "auto_approve"))
            s.set_attribute("gate.risk_tier",   gate_result.get("risk_tier", "low"))
            s.set_attribute("gate.request_id",  gate_result.get("request_id", "") or "")
            s.set_attribute("gate.tool",        tool_name)
