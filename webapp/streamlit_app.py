"""Chat UI for the Cloud Modernization Agent, talking to the async webapp
backend (API Gateway + 2 Lambdas + DynamoDB - see deploy/deploy_webapp.py
and README.md's "Chat webapp" section).

Run locally:
    pip install -r webapp/requirements.txt
    $env:API_BASE_URL = "https://<api-id>.execute-api.<region>.amazonaws.com"
    $env:API_SHARED_SECRET = "<the secret deploy_webapp.py printed>"
    streamlit run webapp/streamlit_app.py

Why polling instead of a single request: API Gateway can't hold a request
open past ~30 seconds, but this agent can legitimately take up to a couple
of minutes (e.g. generating and validating Terraform). So a message is
submitted, a job_id comes back almost instantly, and this app polls for the
result every couple of seconds instead of waiting on one long request.
"""
from __future__ import annotations

import os
import secrets
import time

import requests
import streamlit as st

from response_parsing import extract_answer_text

_POLL_INTERVAL_SECONDS = 2
_MAX_WAIT_SECONDS = 180


def _api_base_url() -> str:
    return st.session_state.get("api_base_url") or os.environ.get("API_BASE_URL", "")


def _api_key() -> str:
    return st.session_state.get("api_key") or os.environ.get("API_SHARED_SECRET", "")


def _submit_chat(prompt: str) -> str:
    response = requests.post(
        f"{_api_base_url()}/chat",
        json={"prompt": prompt, "session_id": st.session_state.session_id},
        headers={"x-api-key": _api_key()},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["job_id"]


def _poll_until_done(job_id: str, status_placeholder) -> dict:
    waited = 0
    while waited < _MAX_WAIT_SECONDS:
        response = requests.get(
            f"{_api_base_url()}/chat/{job_id}", headers={"x-api-key": _api_key()}, timeout=10
        )
        response.raise_for_status()
        body = response.json()
        if body["status"] != "pending":
            return body
        status_placeholder.markdown(f"_Thinking... ({waited}s)_")
        time.sleep(_POLL_INTERVAL_SECONDS)
        waited += _POLL_INTERVAL_SECONDS
    return {"status": "failed", "error": f"Timed out after waiting {_MAX_WAIT_SECONDS}s for a response."}


def main() -> None:
    st.set_page_config(page_title="Cloud Modernization Agent", page_icon="\u2601\ufe0f")
    st.title("\u2601\ufe0f Cloud Modernization Agent")

    if "session_id" not in st.session_state:
        st.session_state.session_id = secrets.token_hex(20)
    if "messages" not in st.session_state:
        st.session_state.messages = []

    with st.sidebar:
        st.subheader("Connection")
        st.session_state.api_base_url = st.text_input("API base URL", value=_api_base_url())
        st.session_state.api_key = st.text_input("API key (x-api-key)", value=_api_key(), type="password")
        st.caption(f"Session ID: `{st.session_state.session_id[:12]}...`")
        if st.button("Start a new conversation"):
            st.session_state.session_id = secrets.token_hex(20)
            st.session_state.messages = []
            st.rerun()

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    prompt = st.chat_input("Ask about architecture, cost, or Terraform for your AWS workload...")
    if not prompt:
        return

    if not _api_base_url() or not _api_key():
        st.error("Set the API base URL and API key in the sidebar first.")
        return

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        status_placeholder = st.empty()
        status_placeholder.markdown("_Sending..._")
        try:
            job_id = _submit_chat(prompt)
            result = _poll_until_done(job_id, status_placeholder)
        except requests.RequestException as e:
            result = {"status": "failed", "error": f"Request to the API failed: {e}"}

        if result["status"] == "completed":
            answer = extract_answer_text(result)
        else:
            answer = f"**Error:** {result.get('error', 'Unknown error')}"

        status_placeholder.markdown(answer)

    st.session_state.messages.append({"role": "assistant", "content": answer})


if __name__ == "__main__":
    main()
