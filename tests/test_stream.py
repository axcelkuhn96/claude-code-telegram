from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.stream import Event, parse_line

FIXTURES = Path(__file__).parent / "fixtures"


def parse_file(name: str) -> list[Event]:
    events: list[Event] = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        events.extend(parse_line(line))
    return events


def assistant(*blocks: dict) -> str:
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})


def tool_event(input_: dict) -> Event:
    (ev,) = parse_line(assistant({"type": "tool_use", "name": "Read", "input": input_}))
    return ev


@pytest.mark.parametrize(
    "line", ["", "   ", "not json", "{broken", "[1, 2]", "42", '"str"', "null"]
)
def test_invalid_or_non_object_returns_empty(line):
    assert parse_line(line) == []


def test_real_sample_ok():
    events = parse_file("stream_ok.jsonl")
    assert [e.kind for e in events] == ["init", "text", "other", "result"]
    init, text, _, result = events
    assert init.session_id
    assert text.text == "OK"
    assert result.text == "OK"
    assert result.is_error is False
    assert result.session_id == init.session_id
    assert result.cost_usd is not None and result.cost_usd > 0
    assert result.duration_ms is not None and result.duration_ms > 0


def test_real_sample_tool_use():
    events = parse_file("stream_tool.jsonl")
    kinds = [e.kind for e in events]
    assert kinds[0] == "init" and kinds[-1] == "result"
    (tool,) = [e for e in events if e.kind == "tool"]
    assert tool.tool == "Bash"
    assert tool.detail == "echo oi"
    # tool_result volta como type=user -> other
    assert "other" in kinds
    assert events[-1].text == "OK"


def test_detail_priority_and_fallbacks():
    assert tool_event({"file_path": "/a/b.py", "command": "x"}).detail == "/a/b.py"
    assert tool_event({"command": "ls -la", "pattern": "p"}).detail == "ls -la"
    assert tool_event({"pattern": "foo.*bar", "url": "u"}).detail == "foo.*bar"
    assert tool_event({"url": "https://x.dev/y"}).detail == "https://x.dev/y"
    assert tool_event({"path": "/srv/dir"}).detail == "/srv/dir"
    assert tool_event({"unrelated": 1}).detail is None
    assert tool_event({}).detail is None
    assert tool_event({"file_path": 123}).detail is None  # tipo inesperado não explode


def test_detail_truncated_to_60_and_single_line():
    ev = tool_event({"command": "echo " + "a" * 200})
    assert ev.detail is not None
    assert len(ev.detail) == 60
    assert ev.detail.endswith("…")
    ev = tool_event({"command": "line1\nline2\n\tline3"})
    assert ev.detail == "line1 line2 line3"
    assert len(tool_event({"command": "x" * 60}).detail) == 60  # no limite: não trunca


def test_assistant_multiple_blocks_in_order_and_unknown_ignored():
    line = assistant(
        {"type": "thinking", "thinking": "hm"},
        {"type": "text", "text": "vou ler"},
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "foo"}},
        {"type": "text", "text": "pronto"},
    )
    events = parse_line(line)
    assert [(e.kind, e.tool, e.detail, e.text) for e in events] == [
        ("text", None, None, "vou ler"),
        ("tool", "Grep", "foo", None),
        ("text", None, None, "pronto"),
    ]


def test_assistant_without_content_is_other():
    assert [
        e.kind for e in parse_line(json.dumps({"type": "assistant", "message": {}}))
    ] == ["other"]
    assert [e.kind for e in parse_line(json.dumps({"type": "assistant"}))] == ["other"]


def test_result_error_without_text():
    line = json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "session_id": "abc",
            "duration_ms": 12,
        }
    )
    (ev,) = parse_line(line)
    assert ev.kind == "result"
    assert ev.is_error is True
    assert ev.text is None
    assert ev.session_id == "abc"
    assert ev.duration_ms == 12
    assert ev.cost_usd is None


def test_result_error_with_limit_text():
    line = json.dumps(
        {"type": "result", "is_error": True, "result": "Usage limit reached"}
    )
    (ev,) = parse_line(line)
    assert ev.is_error is True and ev.text == "Usage limit reached"


def test_unknown_type_is_other():
    (ev,) = parse_line(json.dumps({"type": "rate_limit_event"}))
    assert ev.kind == "other"
    (ev,) = parse_line(json.dumps({"foo": "bar"}))
    assert ev.kind == "other"


def test_system_init_session_id():
    (ev,) = parse_line(
        json.dumps({"type": "system", "subtype": "init", "session_id": "sid-1"})
    )
    assert ev.kind == "init" and ev.session_id == "sid-1"
    (ev,) = parse_line(json.dumps({"type": "system", "subtype": "hook_started"}))
    assert ev.kind == "other"


def test_parse_line_never_raises_on_hostile_input():
    assert parse_line("[" * 200_000) == []  # RecursionError no json.loads
    (ev,) = parse_line(
        '{"type": "result", "duration_ms": 1e999, "total_cost_usd": 1e999}'
    )
    assert ev.duration_ms is None  # int(inf) -> OverflowError
    assert ev.cost_usd == float("inf")


def test_non_string_session_id_is_dropped():
    (ev,) = parse_line(
        json.dumps({"type": "system", "subtype": "init", "session_id": 123})
    )
    assert ev.session_id is None
    (ev,) = parse_line(json.dumps({"type": "result", "session_id": {"x": 1}}))
    assert ev.session_id is None
