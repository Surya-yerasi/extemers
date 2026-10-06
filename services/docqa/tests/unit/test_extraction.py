import pytest

from docqa.domain.extraction import clean_transcription


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("```markdown\n# Card\n\nMember: A\n```", "# Card\n\nMember: A"),
        ("  ```\n| a | b |\n```  ", "| a | b |"),
        ("# Plain page", "# Plain page"),
        ("Text with ```inline``` fence", "Text with ```inline``` fence"),
        ("", ""),
    ],
)
def test_clean_transcription(raw: str, expected: str) -> None:
    assert clean_transcription(raw) == expected
