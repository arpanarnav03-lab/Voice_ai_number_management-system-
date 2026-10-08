"""Pure classification of a Bolna execution record (ported from the terminal MVP)."""
from __future__ import annotations

# Bolna's documented terminal statuses ("stopped" and "balance-low" are in the docs'
# own polling example, so we treat them as final too). "call-disconnected" is NOT final.
TERMINAL = {
    "completed", "no-answer", "busy", "failed",
    "canceled", "cancelled", "stopped", "error", "balance-low",
}


def _num(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _duration(ex: dict):
    td = ex.get("telephony_data") if isinstance(ex.get("telephony_data"), dict) else {}
    for v in (
        ex.get("conversation_duration"),
        ex.get("conversation_time"),
        ex.get("duration"),
        td.get("duration"),
    ):
        n = _num(v)
        if n is not None:
            return n
    return None


def classify(ex: dict, min_seconds: float = 1.0):
    """Return (is_final, connected: bool|None, status, duration, reason)."""
    status = str(ex.get("status") or "").lower()
    if status not in TERMINAL:
        return (False, None, status, None, "")
    dur = _duration(ex)
    if status != "completed":
        return (True, False, status, dur, f"call ended as '{status}'")
    td = ex.get("telephony_data") if isinstance(ex.get("telephony_data"), dict) else {}
    if ex.get("answered_by_voice_mail") or str(td.get("hangup_reason") or "").lower() == "voicemail_detected":
        return (True, False, status, dur, "voicemail / machine detected")
    if dur is None:
        return (True, None, status, None, "no duration returned")
    if dur < min_seconds:
        return (True, False, status, dur, "nobody picked up (duration 0)")
    return (True, True, status, dur, "picked up")
