"""Prompt drift detection.

Tracks prompt.snapshot_hash values per prompt.template_id over time.
When a new hash appears for a known template, it is flagged as drift.

The first hash seen for a template becomes the baseline.
Subsequent different hashes are recorded as drift_detected=1.
"""

from dataclasses import dataclass


@dataclass
class DriftCheckResult:
    template_id:    str
    snapshot_hash:  str
    is_new_template: bool   # first time this template_id is seen
    is_new_hash:    bool    # first time this specific hash is seen
    is_drift:       bool    # hash differs from baseline
    baseline_hash:  str     # current baseline hash (empty if no baseline)


def check_drift(db, template_id: str, snapshot_hash: str) -> DriftCheckResult:
    """
    Check whether a prompt hash represents drift from the established baseline.

    Side effect: upserts a row in gov_prompt_drift for this (template, hash) pair.

    Args:
        db:            GovernanceDB instance.
        template_id:   The prompt.template_id attribute value.
        snapshot_hash: The prompt.snapshot_hash attribute value.

    Returns:
        DriftCheckResult with drift detection flags.
    """
    history = db.get_prompt_drift_history(template_id)

    if not history:
        # First time seeing this template — establish as baseline.
        db.save_prompt_drift(template_id, snapshot_hash, is_baseline=1, drift_detected=0)
        return DriftCheckResult(
            template_id=template_id, snapshot_hash=snapshot_hash,
            is_new_template=True, is_new_hash=True,
            is_drift=False, baseline_hash=snapshot_hash,
        )

    baseline = next((h for h in history if h.get("is_baseline") == 1), None)
    known_hashes = {h["snapshot_hash"] for h in history}

    if baseline is None:
        # History exists but no baseline row (shouldn't normally happen).
        # Promote the oldest entry as baseline.
        db.save_prompt_drift(template_id, snapshot_hash, is_baseline=1, drift_detected=0)
        return DriftCheckResult(
            template_id=template_id, snapshot_hash=snapshot_hash,
            is_new_template=False, is_new_hash=(snapshot_hash not in known_hashes),
            is_drift=False, baseline_hash=snapshot_hash,
        )

    baseline_hash = baseline["snapshot_hash"]
    is_drift      = (snapshot_hash != baseline_hash)
    is_new_hash   = (snapshot_hash not in known_hashes)

    if is_new_hash:
        db.save_prompt_drift(template_id, snapshot_hash,
                             is_baseline=0, drift_detected=1 if is_drift else 0)
    else:
        db.update_prompt_drift_seen(template_id, snapshot_hash)

    return DriftCheckResult(
        template_id=template_id, snapshot_hash=snapshot_hash,
        is_new_template=False, is_new_hash=is_new_hash,
        is_drift=is_drift, baseline_hash=baseline_hash,
    )


def promote_to_baseline(db, template_id: str, snapshot_hash: str) -> None:
    """
    Manually promote a hash to be the new baseline for a template.
    Clears the is_baseline flag from all other hashes for this template.
    """
    db.set_prompt_drift_baseline(template_id, snapshot_hash)
