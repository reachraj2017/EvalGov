"""PII detection using compiled regex patterns.

Scans task.input and task.output span attributes for common PII categories.
Actual matched values are NOT stored — only the type and redacted count, so
the scanner itself does not cause additional PII leakage.
"""

import re
from dataclasses import dataclass, field


@dataclass
class PiiScanResult:
    """Result of a PII scan over a single text string."""

    has_pii: bool
    findings: dict[str, int] = field(default_factory=dict)  # {pii_type: match_count}

    @property
    def pii_types(self) -> list[str]:
        """Return list of PII types found."""
        return [k for k, v in self.findings.items() if v > 0]

    @property
    def total_count(self) -> int:
        return sum(self.findings.values())


# Compiled patterns — one compile per process startup.
_PII_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        re.IGNORECASE,
    ),
    "ssn": re.compile(
        r"\b\d{3}-\d{2}-\d{4}\b",
    ),
    "credit_card": re.compile(
        r"\b(?:\d[ \-]?){13,16}\b",
    ),
    "phone_us": re.compile(
        r"\b(?:\+1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}\b",
    ),
    "ip_address": re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
    ),
    "aws_access_key": re.compile(
        r"\b(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b",
    ),
    "api_key": re.compile(
        r"\b(?:sk-|pk_live_|rk_live_|Bearer\s+)[A-Za-z0-9_\-]{20,}\b",
    ),
}


def scan_pii(text: str) -> PiiScanResult:
    """
    Scan text for PII. Returns PiiScanResult with type counts.

    Actual matched values are never stored — only counts and types are
    returned so the scanner does not itself propagate PII.
    """
    if not text:
        return PiiScanResult(has_pii=False)

    findings: dict[str, int] = {}
    for label, pattern in _PII_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            findings[label] = len(matches)

    return PiiScanResult(has_pii=bool(findings), findings=findings)
