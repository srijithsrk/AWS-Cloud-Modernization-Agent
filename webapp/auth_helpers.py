"""Shared request authentication for the chat webapp's API Lambda.

HTTP APIs (API Gateway v2) don't support the classic "API Gateway API Keys +
Usage Plan" feature - that's a REST API (v1) only capability. For a
personal/demo project, a simple shared-secret header checked in the Lambda
itself is an equivalent, much lower-effort form of access control.
"""
from __future__ import annotations

import hmac
import os


def is_authorized(event: dict) -> bool:
    """Check the caller sent the correct shared secret in the x-api-key
    header. Uses a timing-safe comparison (hmac.compare_digest) rather than
    `==`, since naive string comparison can leak the secret's length/prefix
    through response-time differences."""
    expected = os.environ.get("API_SHARED_SECRET", "")
    headers = event.get("headers") or {}
    # API Gateway HTTP API lowercases header names, but don't rely on that.
    provided = headers.get("x-api-key") or headers.get("X-Api-Key") or ""
    return bool(expected) and hmac.compare_digest(provided, expected)
