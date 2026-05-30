"""Prompt snapshot and template-versioning checks.

Verifies that each agent.task span carries the governance-required
prompt instrumentation attributes:

  prompt.snapshot_hash   — SHA-256 of the complete prompt (system +
                           history + context + user message) captured
                           immediately before the LLM call.
  prompt.template_id     — Version identifier for the prompt template
                           (e.g. "orchestrator-v1.2").

Both attributes are optional in the OTel schema but are *required* by
the governance framework to meet the Prompt Snapshot Completeness gate.
"""

from dataclasses import dataclass


@dataclass
class PromptCheckResult:
    """Outcome of checking prompt governance attributes on a single span."""

    snapshot_present:    bool   # prompt.snapshot_hash attribute exists
    template_versioned:  bool   # prompt.template_id attribute exists
    snapshot_hash:       str    # value (empty string if absent)
    template_id:         str    # value (empty string if absent)


def check_prompt_attributes(span_attrs: dict) -> PromptCheckResult:
    """
    Inspect span attributes for prompt governance compliance.

    Args:
        span_attrs: The ``attributes`` dict from a normalised OTel span.

    Returns:
        PromptCheckResult with boolean flags and the raw attribute values.
    """
    snapshot_hash = str(span_attrs.get("prompt.snapshot_hash") or "").strip()
    template_id   = str(span_attrs.get("prompt.template_id")   or "").strip()

    return PromptCheckResult(
        snapshot_present   = bool(snapshot_hash),
        template_versioned = bool(template_id),
        snapshot_hash      = snapshot_hash,
        template_id        = template_id,
    )
