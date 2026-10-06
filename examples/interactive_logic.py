"""Pure functions shared by the interactive app scaffold and quick logic checks."""

from __future__ import annotations


def update_count(value: int) -> int:
    """Return the next counter value."""
    return int(value) + 1


def validate_form(value: str) -> str:
    """Return trimmed form text or raise a friendly validation error."""
    result = (value or "").strip()
    if not result:
        raise ValueError("Enter a note before saving.")
    return result


def decode_status(payload: bytes) -> str:
    """Decode a bounded response body for display in the status view."""
    return payload.decode("utf-8", errors="replace").strip()[:500]
