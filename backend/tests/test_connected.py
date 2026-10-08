import pytest

from app.connected import classify


def rec(status, **kw):
    return {"status": status, **kw}


@pytest.mark.parametrize("record,final,connected", [
    (rec("completed", conversation_duration=18, answered_by_voice_mail=False), True, True),
    (rec("completed", conversation_duration=10, answered_by_voice_mail=True,
         telephony_data={"hangup_reason": "voicemail_detected"}), True, False),
    (rec("completed", conversation_duration=10, telephony_data={"hangup_reason": "voicemail_detected"}), True, False),
    (rec("completed", conversation_duration=0), True, False),
    (rec("busy"), True, False),
    (rec("no-answer"), True, False),
    (rec("failed"), True, False),
    (rec("canceled"), True, False),
    (rec("balance-low"), True, False),
    (rec("completed"), True, None),                      # no duration -> unclear
    (rec("ringing"), False, None),
    (rec("in-progress"), False, None),
    (rec("call-disconnected", conversation_duration=0), False, None),
])
def test_connected_rule(record, final, connected):
    is_final, conn, *_ = classify(record)
    assert is_final is final
    assert conn is connected


def test_unclear_has_reason_and_duration_fallbacks():
    _, conn, _, dur, reason = classify(rec("completed"))
    assert conn is None and dur is None and reason == "no duration returned"
    _, conn, _, dur, _ = classify(rec("completed", telephony_data={"duration": 7}))
    assert conn is True and dur == 7.0


def test_min_seconds_is_configurable():
    assert classify(rec("completed", conversation_duration=3), min_seconds=5)[1] is False
