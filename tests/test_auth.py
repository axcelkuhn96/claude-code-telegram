import pytest

from bot.auth import contact_ok, is_authorized, normalize_phone
from bot.state import State

ALLOWED = "5511999999999"


def test_normalize_phone_removes_non_digits():
    assert normalize_phone("+55 (11) 99999-9999") == ALLOWED
    assert normalize_phone(ALLOWED) == ALLOWED
    assert normalize_phone("") == ""


def test_contact_ok_own_contact_with_formatted_phone():
    assert contact_ok(42, 42, "+55 11 99999-9999", ALLOWED) is True


def test_contact_ok_rejects_third_party_contact():
    assert contact_ok(99, 42, ALLOWED, ALLOWED) is False


def test_contact_ok_rejects_missing_contact_user_id():
    assert contact_ok(None, 42, ALLOWED, ALLOWED) is False


def test_contact_ok_rejects_other_phone():
    assert contact_ok(42, 42, "5511888888888", ALLOWED) is False


def test_contact_ok_rejects_empty_allowed_and_phone():
    assert contact_ok(42, 42, "", "") is False
    assert contact_ok(42, 42, "abc", "---") is False


def test_is_authorized_requires_owner_set():
    assert is_authorized(State(), 42, "private") is False


def test_is_authorized_owner_private():
    assert is_authorized(State(owner_id=42), 42, "private") is True


def test_is_authorized_other_user():
    assert is_authorized(State(owner_id=42), 43, "private") is False


def test_is_authorized_user_none():
    assert is_authorized(State(owner_id=42), None, "private") is False


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel", ""])
def test_is_authorized_non_private_chat(chat_type):
    assert is_authorized(State(owner_id=42), 42, chat_type) is False


def test_normalize_phone_ascii_digits_only():
    arabic = "٥٥٢٧٩٩٨٥٠٥٥٠٢"
    assert normalize_phone(arabic) == ""
    assert normalize_phone("5٥") == "5"
    assert contact_ok(42, 42, arabic, ALLOWED) is False
