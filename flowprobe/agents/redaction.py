"""Centralized sensitive-data redaction.

Historically four separate sites redacted secrets with slightly different
regexes and logic (prompt construction, structured logs, evidence args, the
dispatch console line). They drifted apart, and the fill_field *result* string
was masked at none of them — leaking the substituted credential back into the
LLM context, evidence.json, and the console.

This module is the single source of truth so those sites cannot diverge again.
See CLAUDE.md design decision #8: credentials never go to the LLM.
"""
from __future__ import annotations

import re

# Matches a *key name* (e.g. ``app_password``) or a *field label* (e.g.
# ``Password``) that identifies sensitive data. Login identifiers
# (username/email) are included so they are masked alongside secrets — the
# stored login identifier is often an email address.
SENSITIVE = re.compile(
    r"password|passwd|secret|token|credential|username|user_name|email",
    re.IGNORECASE,
)

REDACTED = "[REDACTED]"


def is_sensitive(name: str) -> bool:
    """True when a key name or field label refers to sensitive data."""
    return bool(SENSITIVE.search(name or ""))


def redact_result(result: str, field_label: str, value: str) -> str:
    """Mask a substituted value inside a tool result string.

    ``fill_field`` returns e.g. ``filled 'Password' with 'hunter2' (label_exact)``.
    When the field is sensitive, replace every occurrence of the raw value with
    a placeholder so it never reaches the LLM context, evidence, or console.
    """
    if value and is_sensitive(field_label):
        return result.replace(value, REDACTED)
    return result
