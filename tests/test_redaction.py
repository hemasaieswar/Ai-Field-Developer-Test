import pytest

from assessment.redaction import Redactor


@pytest.mark.parametrize(
    "source", ["alice@example.com", "123-45-6789", "4111 1111 1111 1111", "4111111111111111"]
)
def test_every_split(source):
    for index in range(len(source) + 1):
        redactor = Redactor()
        output = redactor.feed("Before " + source[:index])
        output += redactor.feed(source[index:] + "; after") + redactor.finish()
        assert source not in output
        assert output == "Before [REDACTED]; after"


def test_single_character_chunks():
    source = "Email a+b@example.org, SSN 123-45-6789; card 4111-1111-1111-1111."
    redactor = Redactor()
    output = "".join(redactor.feed(c) for c in source) + redactor.finish()
    assert output == "Email [REDACTED], SSN [REDACTED]; card [REDACTED]."


def test_long_candidate_bounded():
    redactor = Redactor()
    output = ""
    for _ in range(10_000):
        output += redactor.feed("a")
        assert len(redactor.word) <= 320
        assert len(redactor.number) <= 128
    output += redactor.feed("@example.com ") + redactor.finish()
    assert output == "[REDACTED] "


def test_ordinary_text_and_responsiveness():
    redactor = Redactor()
    assert redactor.feed("Hello there ") == "Hello there "
    assert redactor.feed("42 dollars.") + redactor.finish() == "42 dollars."
