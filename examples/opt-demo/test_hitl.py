"""
HITL Phase 3 end-to-end test.

What this does:
  1. Calls GovernedToolkit.gate_check() with a high-risk tool (delete_file → high tier)
  2. Gate returns 'pause' → SDK starts polling for human approval
  3. Script blocks here — go to the HITL Queue page in the eval UI and approve/reject
  4. On approval   → prints "APPROVED — tool would execute"
  5. On rejection  → prints "[GOVERNANCE BLOCK] ..."

Prerequisites:
  - governance service running (make up or docker compose up)
  - Phase 2 enforcement ENABLED (Governance Enforcement page → Enable)
  - Searcher circuit breaker CLOSED (otherwise gets 'block' not 'pause')

Run:
  cd /Users/rajlearn/aieval/examples/multi_agent_demo2
  source venv/bin/activate
  python test_hitl.py
"""

import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from instrumentation.governed_toolkit import GovernedToolkit, GateBlockedError

# Use a role whose CB is CLOSED — change to another role if needed
toolkit = GovernedToolkit(agent_role="searcher")


def delete_records(table: str) -> str:
    return f"Deleted all records from '{table}'"


print("=" * 60)
print("HITL Test — calling gate check for 'delete_file' action")
print("This maps to HIGH tier → gate decision = PAUSE")
print("Waiting for human approval in the HITL Queue UI...")
print("=" * 60)

try:
    # 'delete_file' is in _TOOL_ACTION_MAP → action_type='delete' → high tier → pause
    toolkit.gate_check("delete_file", context={"args": {"table": "users", "rows": "all"}})

    print("\nAPPROVED — tool would execute now.")
    result = delete_records("users")
    print(f"Tool result: {result}")

except GateBlockedError as exc:
    print(f"\n[GOVERNANCE BLOCK] {exc}")
