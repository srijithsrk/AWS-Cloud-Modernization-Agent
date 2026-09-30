"""Shared DynamoDB access for the two chat webapp Lambdas.

Table schema (single table, one item per chat request):
    job_id (str, partition key)
    status ("pending" | "completed" | "failed")
    session_id (str) - the AgentCore Runtime session id, so follow-up
        questions continue the same agent conversation
    result (str, JSON-encoded) - present once status == "completed"
    error (str) - present once status == "failed"
    created_at / updated_at (int, epoch seconds)
    ttl (int, epoch seconds) - DynamoDB auto-deletes the item after this
        time so job records don't accumulate forever
"""
from __future__ import annotations

import os
import time
from typing import Any

import boto3

_JOB_TTL_SECONDS = 24 * 60 * 60  # auto-expire job records after 1 day


def _table():
    table_name = os.environ["JOBS_TABLE_NAME"]
    return boto3.resource("dynamodb").Table(table_name)


def put_pending(job_id: str, session_id: str) -> None:
    now = int(time.time())
    _table().put_item(
        Item={
            "job_id": job_id,
            "status": "pending",
            "session_id": session_id,
            "created_at": now,
            "updated_at": now,
            "ttl": now + _JOB_TTL_SECONDS,
        }
    )


def mark_completed(job_id: str, result: dict) -> None:
    import json

    _table().update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :status, #r = :result, updated_at = :now",
        ExpressionAttributeNames={"#s": "status", "#r": "result"},
        ExpressionAttributeValues={
            ":status": "completed",
            ":result": json.dumps(result),
            ":now": int(time.time()),
        },
    )


def mark_failed(job_id: str, error: str) -> None:
    _table().update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :status, #e = :error, updated_at = :now",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={
            ":status": "failed",
            ":error": error,
            ":now": int(time.time()),
        },
    )


def get_job(job_id: str) -> dict[str, Any] | None:
    response = _table().get_item(Key={"job_id": job_id})
    return response.get("Item")
