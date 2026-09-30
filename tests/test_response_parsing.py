"""Unit tests for webapp/response_parsing.py.

Specifically locks in the correct (flattened, not double-nested) shape
that flows from agent.py -> lambda_worker -> DynamoDB -> lambda_api ->
here, since a naming collision between agent.py's own "result" key and an
earlier (buggy) API-response wrapper was caught and fixed during review.
"""
from response_parsing import extract_answer_text


def test_extracts_single_text_block():
    api_response = {
        "status": "completed",
        "result": {"role": "assistant", "content": [{"text": "Here is your answer."}]},
    }
    assert extract_answer_text(api_response) == "Here is your answer."


def test_joins_multiple_text_blocks():
    api_response = {
        "status": "completed",
        "result": {"role": "assistant", "content": [{"text": "Part one."}, {"text": "Part two."}]},
    }
    assert extract_answer_text(api_response) == "Part one.\nPart two."


def test_appends_warning_when_present():
    api_response = {
        "status": "completed",
        "result": {"role": "assistant", "content": [{"text": "Partial answer."}]},
        "warning": "Stopped early (limit_total_tokens) before producing a complete answer.",
    }
    text = extract_answer_text(api_response)
    assert text.startswith("Partial answer.")
    assert "Stopped early (limit_total_tokens)" in text


def test_handles_missing_content_gracefully():
    assert extract_answer_text({"status": "completed", "result": {"role": "assistant", "content": []}}) == (
        "(no text in response)"
    )
