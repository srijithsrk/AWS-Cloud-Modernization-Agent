"""Lambda A ("api-handler"): the only Lambda API Gateway talks to directly.

Handles two routes, both required to return almost instantly (API
Gateway's integration timeout is a hard ~29s ceiling it cannot exceed):

    POST /chat            - starts a job, returns a job_id immediately
    GET  /chat/{job_id}   - reports whether that job is done yet

The actual (potentially slow, 3-90+ second) call to the Bedrock AgentCore
agent happens in a *separate* Lambda ("worker", see lambda_worker.py),
invoked here asynchronously (InvocationType="Event") so this function never
waits for it - see README.md's webapp architecture section for the full
request lifecycle diagram.
"""
from __future__ import annotations

import json
import os
import secrets
import uuid

import boto3

import dynamo_helpers
from auth_helpers import is_authorized


def _lambda_client():
    return boto3.client("lambda")


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _handle_post_chat(event: dict) -> dict:
    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Request body must be valid JSON"})

    prompt = payload.get("prompt")
    if not prompt:
        return _response(400, {"error": "Missing 'prompt' in request body"})

    session_id = payload.get("session_id") or secrets.token_hex(20)
    job_id = str(uuid.uuid4())

    dynamo_helpers.put_pending(job_id, session_id)

    _lambda_client().invoke(
        FunctionName=os.environ["WORKER_FUNCTION_NAME"],
        InvocationType="Event",  # fire-and-forget: don't wait for the agent call to finish
        Payload=json.dumps({"job_id": job_id, "prompt": prompt, "session_id": session_id}).encode(),
    )

    return _response(202, {"job_id": job_id, "session_id": session_id})


def _handle_get_job(event: dict) -> dict:
    job_id = (event.get("pathParameters") or {}).get("job_id")
    if not job_id:
        return _response(400, {"error": "Missing job_id path parameter"})

    item = dynamo_helpers.get_job(job_id)
    if item is None:
        return _response(404, {"error": f"No job found with id '{job_id}'"})

    body = {"status": item["status"]}
    if item["status"] == "completed":
        # Merge (not nest) - the stored value is agent.py's own invoke()
        # return shape, {"result": {...message}, "warning": optional}.
        # Nesting it under another "result" key here would shadow that.
        body.update(json.loads(item["result"]))
    elif item["status"] == "failed":
        body["error"] = item.get("error", "Unknown error")
    return _response(200, body)


def handler(event: dict, context) -> dict:
    if not is_authorized(event):
        return _response(401, {"error": "Missing or invalid x-api-key header"})

    method = event.get("requestContext", {}).get("http", {}).get("method")
    route_key = event.get("routeKey", "")

    try:
        if method == "POST" and route_key == "POST /chat":
            return _handle_post_chat(event)
        if method == "GET" and route_key.startswith("GET /chat/"):
            return _handle_get_job(event)
        return _response(404, {"error": f"No route for {method} {event.get('rawPath')}"})
    except Exception as e:  # noqa: BLE001 - always return structured JSON, never a raw 500
        return _response(500, {"error": str(e)})
