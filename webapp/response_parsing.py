"""Pure response-parsing logic for the Streamlit chat app, kept separate
from streamlit_app.py so it can be unit tested without installing
streamlit into the main project environment (which the agent container
itself never needs)."""
from __future__ import annotations


def extract_answer_text(api_response: dict) -> str:
    """Pull the assistant's reply text out of a completed job's API
    response. `api_response` is GET /chat/{job_id}'s JSON body, which
    merges agent.py's own invoke() return shape directly in:
    {"status": "completed", "result": {"role": "assistant", "content": [...]}, "warning": optional}.
    """
    message = api_response.get("result") or {}
    content = message.get("content") or []
    text_parts = [block["text"] for block in content if isinstance(block, dict) and "text" in block]
    answer = "\n".join(text_parts) if text_parts else "(no text in response)"
    if api_response.get("warning"):
        answer += f"\n\n> **Note:** {api_response['warning']}"
    return answer
