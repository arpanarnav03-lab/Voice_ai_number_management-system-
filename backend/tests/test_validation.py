import pytest

from app.validation import is_valid_phone, mask_phone
from tests.conftest import RECIPIENT


@pytest.mark.parametrize("p", ["+919876543210", "+918000000001"])
def test_valid(p):
    assert is_valid_phone(p)


@pytest.mark.parametrize("p", ["9876543210", "+9198765", "+14155550123", "+91987654321012", "+91 9876543210", "", None])
def test_invalid(p):
    assert not is_valid_phone(p)


def test_mask():
    assert mask_phone("+919876543201") == "+91 98XXXXXX01"


def test_pool_numbers_and_recipients_are_validated(h):
    for bad in ["9876543210", "+9198765", "+14155550123"]:
        r = h.client.post("/api/numbers", json={"phone_number": bad})
        assert r.status_code == 422 and r.json()["code"] == "INVALID_PHONE"
        assert r.json()["message"] == "Use +91 followed by 10 digits"
    n = h.add_number()
    a = h.add_agent("agent-1", n)
    for bad in ["9876543210", "+9198765", "+14155550123"]:
        res = h.trigger((a, bad))[0]
        assert res["ok"] is False and res["code"] == "INVALID_PHONE"
    assert h.trigger((a, RECIPIENT))[0]["ok"] is True
    assert not h.bolna.placed[0]["recipient"].startswith("+1")


def test_number_must_exist_on_bolna(h):
    r = h.client.post("/api/numbers", json={"phone_number": "+917777777777"})
    assert r.status_code == 422 and r.json()["code"] == "NUMBER_NOT_FOUND_ON_BOLNA"


def test_duplicate_number_rejected(h):
    h.add_number("+918000000001")
    r = h.client.post("/api/numbers", json={"phone_number": "+918000000001"})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_ALREADY_IN_POOL"


def test_bolna_linked_number_warns_and_is_not_auto_used(h):
    h.bolna.numbers[0]["agent_id"] = "some-inbound-agent"
    r = h.client.post("/api/numbers", json={"phone_number": h.bolna.numbers[0]["phone_number"]})
    assert r.status_code == 201 and r.json()["warning"]
    assert r.json()["number"]["linked"] is True
