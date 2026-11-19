from __future__ import annotations

import re

from .state import State


def normalize_phone(s: str) -> str:
    return re.sub(r"[^0-9]", "", s)


def contact_ok(
    contact_user_id: int | None, from_id: int, phone: str, allowed: str
) -> bool:
    """Contato próprio (user_id == from_id) com telefone igual ao permitido."""
    allowed_digits = normalize_phone(allowed)
    return (
        contact_user_id is not None
        and contact_user_id == from_id
        and allowed_digits != ""
        and normalize_phone(phone) == allowed_digits
    )


def is_authorized(st: State, user_id: int | None, chat_type: str) -> bool:
    return st.owner_id is not None and user_id == st.owner_id and chat_type == "private"
