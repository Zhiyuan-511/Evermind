"""V6.1.1: ThinkStripper state machine — keeps <think> out of files while
letting the model reason internally."""

import pytest

from ai_bridge import ThinkStripper, strip_think_tags_full


def _feed_all(chunks):
    s = ThinkStripper()
    out = "".join(s.feed(c) for c in chunks)
    out += s.flush()
    return out, s.reasoning_text()


def test_empty_input_is_safe():
    s = ThinkStripper()
    assert s.feed("") == ""
    assert s.flush() == ""


def test_plain_content_passes_through():
    out, reason = _feed_all(["<!DOCTYPE html>", "<body>hi</body>"])
    assert out == "<!DOCTYPE html><body>hi</body>"
    assert reason == ""


def test_single_think_block_stripped():
    out, reason = _feed_all(["hello ", "<think>internal monologue</think>", "world"])
    assert out == "hello world"
    assert reason == "internal monologue"


def test_think_block_split_across_chunks():
    out, reason = _feed_all(["hello <thi", "nk>still think", "ing</thi", "nk>done"])
    assert out == "hello done"
    assert "think" not in out
    assert "thinking" in reason  # reasoning preserved for logs


def test_multiple_think_blocks():
    out, reason = _feed_all([
        "a<think>one</think>b",
        "<think>two</think>c",
    ])
    assert out == "abc"
    assert "one" in reason and "two" in reason


def test_unclosed_think_at_end_dropped():
    """Model truncated mid-thought — drop the unclosed block."""
    out, reason = _feed_all(["clean start <think>never finished"])
    assert out == "clean start "
    assert reason == "never finished"


def test_close_tag_split_across_chunks():
    out, _ = _feed_all(["<think>ab</thi", "nk>real"])
    assert out == "real"


def test_think_tag_inside_code_string_still_stripped():
    """Expected trade-off: if 'builder' writes '<think>' literally inside
    code (rare but possible), it gets stripped. Matches AI SDK default."""
    out, _ = _feed_all(["const tag = '<think>oops</think>';"])
    assert "<think>" not in out


def test_content_after_close_appended_cleanly():
    out, _ = _feed_all(["<think>reason</think><!DOCTYPE html>"])
    assert out == "<!DOCTYPE html>"


def test_reasoning_accumulates_across_blocks():
    s = ThinkStripper()
    s.feed("a<think>first</think>")
    s.feed("b<think>second</think>c")
    assert "first" in s.reasoning_text()
    assert "second" in s.reasoning_text()


def test_angle_bracket_not_a_think_tag_flows_through():
    out, _ = _feed_all(["before <div>", "<span>x</span>", "</div> after"])
    assert out == "before <div><span>x</span></div> after"


def test_strip_full_non_streaming():
    text = "start<think>hidden</think>middle<THINK>still hidden</THINK>end"
    assert strip_think_tags_full(text) == "startmiddleend"


def test_strip_full_passthrough_when_no_think():
    text = "<!DOCTYPE html><body>no thinking here</body>"
    assert strip_think_tags_full(text) == text


def test_strip_full_removes_orphan_closing_tag():
    assert strip_think_tags_full("hello </think>world") == "hello world"


def test_unlikely_partial_lt_at_end_buffered():
    """If delta ends with '<' we can't know yet if it's <think>. Buffer it."""
    s = ThinkStripper()
    out1 = s.feed("hello <")
    assert out1 == "hello "
    out2 = s.feed("div>ok")
    assert out2 == "<div>ok"


def test_partial_open_tag_prefix_buffered():
    s = ThinkStripper()
    out1 = s.feed("x <th")
    assert out1 == "x "
    out2 = s.feed("ink>hidden</think>y")
    assert out2 == "y"


def test_tool_call_args_are_not_touched():
    """ThinkStripper only processes content deltas. Tool-call arguments live
    in a separate delta field on OpenAI-compat SSE, so this is covered by
    contract; the unit layer just documents that .feed is content-only."""
    # No tool_call arg passes through ThinkStripper in production — this test
    # simply guards against someone accidentally routing tool args through it.
    out, _ = _feed_all(['{"file":"index.html","content":"<!DOCTYPE>"}'])
    assert "<!DOCTYPE>" in out
