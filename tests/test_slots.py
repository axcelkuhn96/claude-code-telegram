import copy

import pytest

from bot import slots

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
C = "33333333-3333-4333-8333-333333333333"
D = "44444444-4444-4444-8444-444444444444"
E = "55555555-5555-4555-8555-555555555555"


def entry(sid, project="/srv/app", account="main"):
    return {"session_id": sid, "project": project, "account": account}


# parse_prefix


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2: texto", (2, "texto")),
        ("  1 :  oi", (1, "oi")),
        ("4：japonês", (4, "japonês")),
        ("3:a\nb", (3, "a\nb")),
        ("5: x", (None, "5: x")),
        ("0: x", (None, "0: x")),
        ("2:", (None, "2:")),
        ("2: ", (None, "2: ")),
        ("sem prefixo", (None, "sem prefixo")),
        ("12: x", (None, "12: x")),
        ("x 2: y", (None, "x 2: y")),
    ],
)
def test_parse_prefix(text, expected):
    assert slots.parse_prefix(text) == expected


# pin


def test_pin_first_free():
    new, out = slots.pin({"1": entry(A)}, None, entry(B))
    assert out == "pinned"
    assert new["2"]["session_id"] == B


def test_pin_first_free_fills_gap():
    new, out = slots.pin({"2": entry(A)}, None, entry(B))
    assert (out, new["1"]["session_id"]) == ("pinned", B)


def test_pin_full():
    s = {str(i): entry(x) for i, x in zip(range(1, 5), (A, B, C, D))}
    new, out = slots.pin(s, None, entry(E))
    assert out == "full"
    assert new == s


def test_pin_explicit_free_slot():
    new, out = slots.pin({}, 3, entry(A))
    assert (out, set(new)) == ("pinned", {"3"})


def test_pin_replaces_occupied_slot():
    new, out = slots.pin({"2": entry(A)}, 2, entry(B))
    assert out == "replaced"
    assert new["2"]["session_id"] == B


def test_pin_already_same_slot():
    s = {"2": entry(A)}
    new, out = slots.pin(s, 2, entry(A))
    assert out == "already"
    assert new == s


def test_pin_already_without_slot_does_not_move():
    s = {"2": entry(A)}
    new, out = slots.pin(s, None, entry(A))
    assert out == "already"
    assert new == s


def test_pin_same_session_moves_to_free_slot():
    new, out = slots.pin({"1": entry(A)}, 3, entry(A))
    assert out == "pinned"
    assert set(new) == {"3"}


def test_pin_same_session_moves_over_other():
    new, out = slots.pin({"1": entry(A), "2": entry(B)}, 2, entry(A))
    assert out == "replaced"
    assert set(new) == {"2"}
    assert new["2"]["session_id"] == A


def test_pin_does_not_mutate_input():
    s = {"1": entry(A), "2": entry(B)}
    snap = copy.deepcopy(s)
    new, _ = slots.pin(s, 2, entry(A))
    assert s == snap
    new["1"] = entry(C)
    assert s == snap


@pytest.mark.parametrize("n", [0, 5, -1])
def test_pin_invalid_slot_raises(n):
    with pytest.raises(ValueError):
        slots.pin({}, n, entry(A))


# unpin


def test_unpin_removes_without_mutating():
    s = {"1": entry(A), "2": entry(B)}
    snap = copy.deepcopy(s)
    assert set(slots.unpin(s, 1)) == {"2"}
    assert s == snap


def test_unpin_missing_is_noop():
    assert slots.unpin({"1": entry(A)}, 3) == {"1": entry(A)}


# label / keyboard_row / slot_for_button


def test_label_basic_and_running():
    e = entry(A, "/srv/meu-projeto")
    assert slots.label(1, e, False) == "1·meu-projet"
    assert slots.label(1, e, True).endswith(" ⏳")


def test_label_truncates_to_10():
    assert slots.label(2, entry(A, "/x/abcdefghijklmno"), False) == "2·abcdefghij"


def test_label_home_is_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/home/you")
    assert slots.label(3, entry(A, "/home/you"), False) == "3·~"


def test_keyboard_row_ordered_with_running():
    s = {"3": entry(C, "/p/c"), "1": entry(A, "/p/a")}
    assert slots.keyboard_row(s, {C}) == ["1·a", "3·c ⏳"]


def test_keyboard_row_empty():
    assert slots.keyboard_row({}, set()) == []


def test_slot_for_button_roundtrip():
    s = {"1": entry(A, "/p/a"), "4": entry(D, "/p/d")}
    for text, n in zip(slots.keyboard_row(s, {D}), (1, 4)):
        assert slots.slot_for_button(text, s) == n


def test_slot_for_button_rejects_unknown():
    s = {"1": entry(A, "/p/a")}
    assert slots.slot_for_button("2·a", s) is None
    assert slots.slot_for_button("oi", s) is None
    assert slots.slot_for_button("", s) is None
    assert slots.slot_for_button("1a", s) is None


def test_slot_for_button_tolerates_hourglass():
    assert slots.slot_for_button("1·a ⏳", {"1": entry(A, "/p/a")}) == 1


# validate


def test_validate_keeps_good_drops_bad():
    raw = {
        "1": entry(A),
        "2": entry("not-a-uuid"),
        "5": entry(B),
        "x": entry(C),
        "3": "string",
        "4": {"session_id": D},
    }
    assert slots.validate(raw) == {"1": entry(A)}


def test_validate_non_dict_returns_empty():
    assert slots.validate(None) == {}
    assert slots.validate([1]) == {}


def test_validate_normalizes_extra_keys_and_rejects_non_str():
    raw = {"1": {**entry(A), "extra": 1}, "2": {**entry(B), "project": 5}}
    assert slots.validate(raw) == {"1": entry(A)}


def test_validate_drops_duplicate_session():
    out = slots.validate({"1": entry(A), "2": entry(A)})
    assert out == {"1": entry(A)}
