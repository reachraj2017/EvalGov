"""PII detection using compiled regex patterns."""

import re
from dataclasses import dataclass, field


@dataclass
class PiiScanResult:
    has_pii: bool
    findings: dict[str, int] = field(default_factory=dict)

    @property
    def pii_types(self) -> list[str]:
        return [k for k, v in self.findings.items() if v > 0]

    @property
    def total_count(self) -> int:
        return sum(self.findings.values())


_PII_PATTERNS: dict[str, re.Pattern] = {
    "email":        re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.I),
    "ssn":          re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card":  re.compile(r"\b(?:\d[ \-]?){13,16}\b"),
    "phone_us":     re.compile(r"\b(?:\+1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "ip_address":   re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"),
    "aws_key":      re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b"),
    "api_key":      re.compile(r"\b(?:sk-|pk_live_|rk_live_|Bearer\s+)[A-Za-z0-9_\-]{20,}\b"),
}


def scan_pii(text: str) -> PiiScanResult:
    if not text:
        return PiiScanResult(has_pii=False)
    findings: dict[str, int] = {}
    for label, pattern in _PII_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            findings[label] = len(matches)
    return PiiScanResult(has_pii=bool(findings), findings=findings)
