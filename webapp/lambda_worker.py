"""Lambda B ("worker"): does the actual (slow) Bedrock AgentCore call.

Invoked asynchronously by lambda_api.py (InvocationType="Event") - nothing
is waiting on this function synchronously, so it's free to take as long as
the agent needs (bounded only by this Lambda's own configured timeout,
which deploy_webapp.py sets generously high - see README.md).
"""
from __future__ import annotations

import json
import os

import boto3

import dynamo_helpers


def _agentcore_client():
    return boto3.client("bedrock-agentcore")


def handler(event: dict, context) -> None:
    job_id = event["job_id"]
    prompt = event["prompt"]
    session_id = event["session_id"]

    try:
        response = _agentcore_client().invoke_agent_runtime(
            agentRuntimeArn=os.environ["AGENT_RUNTIME_ARN"],
            runtimeSessionId=session_id,
            payload=json.dumps({"prompt": prompt}).encode(),
            qualifier="DEFAULT",
        )
        result = json.loads(response["response"].read())
        dynamo_helpers.mark_completed(job_id, result)
    except Exception as e:  # noqa: BLE001 - always record a failure, never let this Lambda just vanish
        dynamo_helpers.mark_failed(job_id, str(e))
