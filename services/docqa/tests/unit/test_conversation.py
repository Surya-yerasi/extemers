import pytest

from docqa.domain.conversation import (
    HISTORY_TURNS,
    Exchange,
    build_rewrite_prompt,
    is_conversation_id,
    make_title,
    new_conversation_id,
    parse_rewrite,
    parse_turn_id,
    turn_id,
)


def test_ids_round_trip() -> None:
    cid = new_conversation_id()
    assert is_conversation_id(cid)
    assert cid != new_conversation_id()
    assert parse_turn_id(turn_id(cid, 12)) == (cid, 12)


@pytest.mark.parametrize(
    "value",
    ["", "c_123", "c_0123456789ABCDEF", "../c_0123456789abcdef", "c_0123456789abcdef/x"],
)
def test_conversation_ids_are_strict(value: str) -> None:
    assert not is_conversation_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "c_0123456789abcdef",
        "c_0123456789abcdef.0",
        "c_0123456789abcdef.01",
        "x.1",
        "c_0123456789abcdef.12345",
    ],
)
def test_turn_ids_are_strict(value: str) -> None:
    assert parse_turn_id(value) is None


def test_rewrite_prompt_keeps_recent_history_and_truncates_answers() -> None:
    history = [Exchange(question=f"q{i}", answer="a" * 900) for i in range(HISTORY_TURNS + 2)]
    prompt = build_rewrite_prompt(history, "and that one?")
    assert "User: q0" not in prompt  # older than the window
    assert f"User: q{HISTORY_TURNS + 1}" in prompt
    assert "a" * 501 not in prompt
    assert prompt.endswith("Follow-up question: and that one?\n\nStandalone question:")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("What was my master's GPA?", "What was my master's GPA?"),
        ('\n  "What was my master\'s GPA?"\nIt was 3.72.', "What was my master's GPA?"),
        ("Standalone question: Which school?", "Which school?"),
        ("", "original"),
        ("   \n  ", "original"),
        ("x" * 50, "original"),  # longer than allowed
    ],
)
def test_parse_rewrite(raw: str, expected: str) -> None:
    assert parse_rewrite(raw, "original", max_chars=40) == expected


def test_make_title() -> None:
    assert make_title("  What   was my\nGPA? ") == "What was my GPA?"
    long = make_title("word " * 40)
    assert len(long) == 80
    assert long.endswith("…")
