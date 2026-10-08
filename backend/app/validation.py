from __future__ import annotations

import re

from .errors import AppError

PHONE_RE = re.compile(r"\+91[0-9]{10}")
PHONE_MESSAGE = "Use +91 followed by 10 digits"


def is_valid_phone(value: object) -> bool:
    return isinstance(value, str) and PHONE_RE.fullmatch(value) is not None


def validate_phone(value: object) -> str:
    """Return the cleaned number or raise INVALID_PHONE."""
    if isinstance(value, str):
        value = value.strip()
    if not is_valid_phone(value):
        raise AppError("INVALID_PHONE", PHONE_MESSAGE, 422)
    return value  # type: ignore[return-value]


def mask_phone(value: str | None) -> str:
    """+919876543201 -> '+91 98XXXXXX01'. Used in the API output and in logs."""
    if not value or not PHONE_RE.fullmatch(value):
        return "XXXX"
    return f"+91 {value[3:5]}{'X' * 6}{value[-2:]}"
