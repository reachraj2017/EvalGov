"""Prompt snapshot and template-versioning checks."""

from dataclasses import dataclass


@dataclass
class PromptCheckResult:
    snapshot_present:    bool
    template_versioned:  bool
    snapshot_hash:       str
    template_id:         str


def check_prompt_attributes(span_attrs: dict) -> PromptCheckResult:
    snapshot_hash = str(span_attrs.get("prompt.snapshot_hash") or "").strip()
    template_id   = str(span_attrs.get("prompt.template_id")   or "").strip()
    return PromptCheckResult(
        snapshot_present   = bool(snapshot_hash),
        template_versioned = bool(template_id),
        snapshot_hash      = snapshot_hash,
        template_id        = template_id,
    )
