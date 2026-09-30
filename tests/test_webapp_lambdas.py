"""Unit tests for the chat webapp's two Lambda handlers.

boto3 clients and dynamo_helpers are mocked throughout - these tests never
touch real AWS, matching the project's existing hermetic-test convention
(see tests/test_terraform_validate.py).
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("JOBS_TABLE_NAME", "test-jobs-table")
os.environ.setdefault("WORKER_FUNCTION_NAME", "test-worker-fn")
os.environ.setdefault("API_SHARED_SECRET", "test-secret")
os.environ.setdefault("AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test")

import lambda_api
import lambda_worker


def _api_gateway_event(method: str, route_key: str, *, body=None, path_params=None, api_key="test-secret") -> dict:
    return {
        "requestContext": {"http": {"method": method}},
        "routeKey": route_key,
        "rawPath": route_key.split(" ", 1)[1] if " " in route_key else route_key,
        "headers": {"x-api-key": api_key} if api_key is not None else {},
        "body": json.dumps(body) if body is not None else None,
        "pathParameters": path_params or {},
    }


class TestLambdaApiAuth:
    def test_rejects_missing_api_key(self):
        event = _api_gateway_event("POST", "POST /chat", body={"prompt": "hi"}, api_key=None)
        response = lambda_api.handler(event, None)
        assert response["statusCode"] == 401

    def test_rejects_wrong_api_key(self):
        event = _api_gateway_event("POST", "POST /chat", body={"prompt": "hi"}, api_key="wrong")
        response = lambda_api.handler(event, None)
        assert response["statusCode"] == 401


class TestPostChat:
    def test_starts_job_and_invokes_worker_asynchronously(self):
        event = _api_gateway_event("POST", "POST /chat", body={"prompt": "hello"})
        fake_lambda_client = MagicMock()

        with patch("lambda_api.dynamo_helpers.put_pending") as put_pending, patch(
            "lambda_api._lambda_client", return_value=fake_lambda_client
        ):
            response = lambda_api.handler(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert "job_id" in body and "session_id" in body

        put_pending.assert_called_once()
        called_job_id, called_session_id = put_pending.call_args[0]
        assert called_job_id == body["job_id"]
        assert called_session_id == body["session_id"]

        fake_lambda_client.invoke.assert_called_once()
        invoke_kwargs = fake_lambda_client.invoke.call_args.kwargs
        assert invoke_kwargs["FunctionName"] == "test-worker-fn"
        assert invoke_kwargs["InvocationType"] == "Event"  # must be fire-and-forget, not RequestResponse
        payload = json.loads(invoke_kwargs["Payload"])
        assert payload == {"job_id": body["job_id"], "prompt": "hello", "session_id": body["session_id"]}

    def test_reuses_provided_session_id(self):
        event = _api_gateway_event("POST", "POST /chat", body={"prompt": "hello", "session_id": "my-session-123"})
        with patch("lambda_api.dynamo_helpers.put_pending"), patch("lambda_api._lambda_client", return_value=MagicMock()):
            response = lambda_api.handler(event, None)
        assert json.loads(response["body"])["session_id"] == "my-session-123"

    def test_rejects_missing_prompt(self):
        event = _api_gateway_event("POST", "POST /chat", body={})
        response = lambda_api.handler(event, None)
        assert response["statusCode"] == 400

    def test_rejects_invalid_json_body(self):
        event = _api_gateway_event("POST", "POST /chat", api_key="test-secret")
        event["body"] = "{not valid json"
        response = lambda_api.handler(event, None)
        assert response["statusCode"] == 400


class TestGetJob:
    def test_returns_pending_status(self):
        event = _api_gateway_event("GET", "GET /chat/{job_id}", path_params={"job_id": "abc123"})
        with patch("lambda_api.dynamo_helpers.get_job", return_value={"job_id": "abc123", "status": "pending"}):
            response = lambda_api.handler(event, None)
        assert response["statusCode"] == 200
        assert json.loads(response["body"]) == {"status": "pending"}

    def test_returns_completed_result(self):
        event = _api_gateway_event("GET", "GET /chat/{job_id}", path_params={"job_id": "abc123"})
        # This is agent.py's own invoke() return shape - lambda_api must
        # merge it directly into the response, not nest it under another
        # "result" key (that would shadow the real one).
        stored_agent_response = {"result": {"role": "assistant", "content": [{"text": "here you go"}]}}
        with patch(
            "lambda_api.dynamo_helpers.get_job",
            return_value={"job_id": "abc123", "status": "completed", "result": json.dumps(stored_agent_response)},
        ):
            response = lambda_api.handler(event, None)
        body = json.loads(response["body"])
        assert body["status"] == "completed"
        assert body["result"] == stored_agent_response["result"]

    def test_returns_failed_with_error(self):
        event = _api_gateway_event("GET", "GET /chat/{job_id}", path_params={"job_id": "abc123"})
        with patch(
            "lambda_api.dynamo_helpers.get_job",
            return_value={"job_id": "abc123", "status": "failed", "error": "boom"},
        ):
            response = lambda_api.handler(event, None)
        body = json.loads(response["body"])
        assert body == {"status": "failed", "error": "boom"}

    def test_returns_404_for_unknown_job(self):
        event = _api_gateway_event("GET", "GET /chat/{job_id}", path_params={"job_id": "nope"})
        with patch("lambda_api.dynamo_helpers.get_job", return_value=None):
            response = lambda_api.handler(event, None)
        assert response["statusCode"] == 404


class TestLambdaWorker:
    def test_success_marks_job_completed(self):
        fake_body = MagicMock()
        fake_body.read.return_value = json.dumps({"result": {"role": "assistant", "content": [{"text": "hi"}]}}).encode()
        fake_client = MagicMock()
        fake_client.invoke_agent_runtime.return_value = {"response": fake_body}

        with patch("lambda_worker._agentcore_client", return_value=fake_client), patch(
            "lambda_worker.dynamo_helpers.mark_completed"
        ) as mark_completed:
            lambda_worker.handler({"job_id": "j1", "prompt": "hi", "session_id": "s1"}, None)

        fake_client.invoke_agent_runtime.assert_called_once_with(
            agentRuntimeArn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
            runtimeSessionId="s1",
            payload=json.dumps({"prompt": "hi"}).encode(),
            qualifier="DEFAULT",
        )
        mark_completed.assert_called_once_with("j1", {"result": {"role": "assistant", "content": [{"text": "hi"}]}})

    def test_failure_marks_job_failed(self):
        fake_client = MagicMock()
        fake_client.invoke_agent_runtime.side_effect = RuntimeError("bedrock exploded")

        with patch("lambda_worker._agentcore_client", return_value=fake_client), patch(
            "lambda_worker.dynamo_helpers.mark_failed"
        ) as mark_failed:
            lambda_worker.handler({"job_id": "j1", "prompt": "hi", "session_id": "s1"}, None)

        mark_failed.assert_called_once_with("j1", "bedrock exploded")
