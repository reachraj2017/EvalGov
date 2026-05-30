"""Agent identity and API key authentication.

When GOVERNANCE_REQUIRE_AUTH=true, callers must include X-Agent-Key header.
Keys are stored as SHA-256 hashes in gov_agent_keys — plaintext never persisted.
"""

import hashlib
import hmac
import os
import secrets
import string
from typing import Optional

import structlog
from fastapi import HTTPException, Request

log = structlog.get_logger(__name__)

_REQUIRE_AUTH  = os.getenv("GOVERNANCE_REQUIRE_AUTH", "false").lower() == "true"
_MASTER_KEY    = os.getenv("GOVERNANCE_MASTER_KEY", "")
_ALPHABET      = string.ascii_letters + string.digits
_KEY_BODY_LEN  = 40


def generate_key() -> tuple[str, str, str]:
    """Generate a new API key.

    Returns (full_key, display_prefix, sha256_hash).
    full_key is returned once and never stored.
    """
    body      = "".join(secrets.choice(_ALPHABET) for _ in range(_KEY_BODY_LEN))
    full_key  = f"gov_{body}"
    prefix    = full_key[:12]   # "gov_" + 8 chars shown in UI
    key_hash  = hashlib.sha256(full_key.encode()).hexdigest()
    return full_key, prefix, key_hash


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def resolve_agent_key(request: Request, db) -> Optional[str]:
    """Validate X-Agent-Key and return the bound agent_role, or None if auth is off.

    Raises HTTP 401 if auth is enabled but the key is missing/invalid.
    """
    if not _REQUIRE_AUTH:
        return None

    key = request.headers.get("X-Agent-Key", "").strip()
    if not key:
        raise HTTPException(status_code=401, detail="X-Agent-Key header required")

    # Master key bypass (emergency access)
    if _MASTER_KEY and hmac.compare_digest(key, _MASTER_KEY):
        log.warning("master_key_used", path=str(request.url.path))
        return "__master__"

    key_hash = hash_key(key)
    row = db.fetch_one(
        "SELECT agent_role, key_id FROM otel.gov_agent_keys FINAL "
        "WHERE key_hash = %(h)s AND enabled = 1 "
        "  AND revoked_at = '1970-01-01 00:00:00'",
        {"h": key_hash},
    )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    # Bump last_used_at asynchronously (best-effort)
    try:
        db.execute(
            "ALTER TABLE otel.gov_agent_keys UPDATE last_used_at = now() "
            "WHERE key_id = %(kid)s",
            {"kid": row["key_id"]},
        )
    except Exception:
        pass

    return str(row["agent_role"])
