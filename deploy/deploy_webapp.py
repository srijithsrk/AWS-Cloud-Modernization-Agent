"""Deploys the async chat webapp that fronts the already-deployed Bedrock
AgentCore Runtime agent: a DynamoDB job table, two Lambda functions
("api-handler" and "worker"), and an HTTP API Gateway in front of them.

See README.md's "Chat webapp" section for the full architecture and why
this is split into two Lambdas (API Gateway's ~29s integration timeout
ceiling can't be raised, so the Lambda behind it must always return
almost instantly - see webapp/lambda_api.py's docstring).

Usage:
    python deploy/deploy_webapp.py \
        --agent-runtime-arn arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/cloud_modernization_agent-abc123 \
        --region us-east-1

Requires the IAM identity running this script to be able to create/use:
IAM roles, DynamoDB tables, Lambda functions, and API Gateway (HTTP APIs).
"""
from __future__ import annotations

import argparse
import io
import json
import secrets
import time
import zipfile
from pathlib import Path

import boto3

_WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"

_LAMBDA_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
}

_LOGGING_STATEMENT = {
    "Sid": "WriteLogs",
    "Effect": "Allow",
    "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
    "Resource": "*",
}

# API Gateway HTTP API integrations cap out at 30000ms (30s) - this is the
# same hard platform ceiling this whole two-Lambda design exists to work
# around. The worker Lambda is invoked asynchronously (never through this
# integration path), so this constant only matters for the fast api-handler.
_API_LAMBDA_TIMEOUT_SECONDS = 15
# The worker calls the agent directly and can legitimately take 90+ seconds
# (see real CloudWatch evidence in memory/session notes) - give it generous
# headroom well above anything observed so far.
_WORKER_LAMBDA_TIMEOUT_SECONDS = 300


def _zip_files(filenames: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in filenames:
            zf.write(_WEBAPP_DIR / name, arcname=name)
    return buffer.getvalue()


def _ensure_table(dynamodb, table_name: str) -> str:
    try:
        resp = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.get_waiter("table_exists").wait(TableName=table_name)
        dynamodb.update_time_to_live(
            TableName=table_name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"}
        )
        return resp["TableDescription"]["TableArn"]
    except dynamodb.exceptions.ResourceInUseException:
        return dynamodb.describe_table(TableName=table_name)["Table"]["TableArn"]


def _ensure_role(iam, role_name: str, policy_statements: list[dict], description: str) -> str:
    try:
        role_arn = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(_LAMBDA_TRUST_POLICY),
            Description=description,
        )["Role"]["Arn"]
        created = True
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        created = False

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=f"{role_name}-policy",
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [_LOGGING_STATEMENT, *policy_statements]}),
    )
    if created:
        print("  Waiting for IAM role propagation ...")
        time.sleep(10)  # newly created IAM roles can take a few seconds to become assumable
    return role_arn


def _ensure_lambda(
    lambda_client, function_name: str, role_arn: str, handler: str, zip_bytes: bytes, env_vars: dict, timeout: int
) -> str:
    try:
        resp = lambda_client.create_function(
            FunctionName=function_name,
            Runtime="python3.12",
            Role=role_arn,
            Handler=handler,
            Code={"ZipFile": zip_bytes},
            Environment={"Variables": env_vars},
            Timeout=timeout,
            MemorySize=256,
        )
        lambda_client.get_waiter("function_active").wait(FunctionName=function_name)
        return resp["FunctionArn"]
    except lambda_client.exceptions.ResourceConflictException:
        lambda_client.update_function_code(FunctionName=function_name, ZipFile=zip_bytes)
        lambda_client.get_waiter("function_updated").wait(FunctionName=function_name)
        resp = lambda_client.update_function_configuration(
            FunctionName=function_name,
            Role=role_arn,
            Handler=handler,
            Environment={"Variables": env_vars},
            Timeout=timeout,
        )
        lambda_client.get_waiter("function_updated").wait(FunctionName=function_name)
        return resp["FunctionArn"]


def _ensure_http_api(apigw, api_name: str) -> str:
    for page in apigw.get_paginator("get_apis").paginate():
        for api in page["Items"]:
            if api["Name"] == api_name:
                return api["ApiId"]
    return apigw.create_api(Name=api_name, ProtocolType="HTTP")["ApiId"]


def _ensure_integration(apigw, api_id: str, lambda_arn: str) -> str:
    for page in apigw.get_paginator("get_integrations").paginate(ApiId=api_id):
        for integration in page["Items"]:
            if integration.get("IntegrationUri") == lambda_arn:
                return integration["IntegrationId"]
    return apigw.create_integration(
        ApiId=api_id,
        IntegrationType="AWS_PROXY",
        IntegrationUri=lambda_arn,
        IntegrationMethod="POST",  # how API Gateway invokes the Lambda Invoke API itself - always POST for proxy integrations
        PayloadFormatVersion="2.0",
        TimeoutInMillis=_API_LAMBDA_TIMEOUT_SECONDS * 1000,
    )["IntegrationId"]


def _ensure_route(apigw, api_id: str, route_key: str, integration_id: str) -> None:
    target = f"integrations/{integration_id}"
    for page in apigw.get_paginator("get_routes").paginate(ApiId=api_id):
        for route in page["Items"]:
            if route["RouteKey"] == route_key:
                if route["Target"] != target:
                    apigw.update_route(ApiId=api_id, RouteId=route["RouteId"], Target=target)
                return
    apigw.create_route(ApiId=api_id, RouteKey=route_key, Target=target)


def _ensure_stage(apigw, api_id: str, default_route_settings: dict, route_settings: dict) -> None:
    for page in apigw.get_paginator("get_stages").paginate(ApiId=api_id):
        for stage in page["Items"]:
            if stage["StageName"] == "$default":
                # Re-applied on every run (not just at creation) so changing
                # throttle limits and re-running this script takes effect.
                apigw.update_stage(
                    ApiId=api_id,
                    StageName="$default",
                    DefaultRouteSettings=default_route_settings,
                    RouteSettings=route_settings,
                )
                return
    apigw.create_stage(
        ApiId=api_id,
        StageName="$default",
        AutoDeploy=True,
        DefaultRouteSettings=default_route_settings,
        RouteSettings=route_settings,
    )


def _ensure_concurrency(lambda_client, function_name: str, limit: int) -> None:
    """Set reserved concurrency, or remove it (unbounded, up to the account's
    shared pool) if limit is 0 or negative."""
    if limit <= 0:
        lambda_client.delete_function_concurrency(FunctionName=function_name)
    else:
        lambda_client.put_function_concurrency(FunctionName=function_name, ReservedConcurrentExecutions=limit)


def _allow_apigateway_to_invoke(lambda_client, function_name: str, account_id: str, region: str, api_id: str) -> None:
    try:
        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId="AllowApiGatewayInvoke",
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=f"arn:aws:execute-api:{region}:{account_id}:{api_id}/*/*",
        )
    except lambda_client.exceptions.ResourceConflictException:
        pass  # permission already granted from a previous run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-runtime-arn", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--table-name", default="cloud-modernization-agent-jobs")
    parser.add_argument("--api-lambda-name", default="cloud-modernization-agent-api")
    parser.add_argument("--worker-lambda-name", default="cloud-modernization-agent-worker")
    parser.add_argument("--api-name", default="cloud-modernization-agent-chat-api")
    parser.add_argument(
        "--api-secret",
        default=None,
        help="Shared secret clients must send as the x-api-key header. Auto-generated and printed if omitted.",
    )
    parser.add_argument(
        "--chat-rate-limit", type=float, default=2.0,
        help="Steady-state req/sec for POST /chat (the expensive, Bedrock-invoking route). Default 2.",
    )
    parser.add_argument(
        "--chat-burst-limit", type=int, default=5,
        help="Burst capacity for POST /chat. Default 5.",
    )
    parser.add_argument(
        "--poll-rate-limit", type=float, default=10.0,
        help="Steady-state req/sec for GET /chat/{job_id} (cheap polling reads, called every ~2s by Streamlit). Default 10.",
    )
    parser.add_argument(
        "--poll-burst-limit", type=int, default=20,
        help="Burst capacity for GET /chat/{job_id}. Default 20.",
    )
    parser.add_argument(
        "--worker-concurrency", type=int, default=5,
        help="Reserved concurrency for the worker Lambda. AgentCore Runtime itself only processes "
        "~2 concurrent invocations per container, so a small margin above that is enough; pass 0 to leave unbounded. Default 5.",
    )
    parser.add_argument(
        "--api-concurrency", type=int, default=20,
        help="Reserved concurrency for the api-handler Lambda (fast path); pass 0 to leave unbounded. Default 20.",
    )
    args = parser.parse_args()

    session = boto3.session.Session(region_name=args.region)
    region = session.region_name
    account_id = session.client("sts").get_caller_identity()["Account"]
    api_secret = args.api_secret or secrets.token_urlsafe(32)

    print(f"Account: {account_id}  Region: {region}")

    print(f"Ensuring DynamoDB table '{args.table_name}' exists ...")
    dynamodb = session.client("dynamodb")
    table_arn = _ensure_table(dynamodb, args.table_name)
    print(f"Table ARN: {table_arn}")

    print("Ensuring IAM roles exist ...")
    iam = session.client("iam")
    worker_role_arn = _ensure_role(
        iam,
        "cloud-modernization-agent-worker-role",
        [
            {
                "Sid": "UpdateJobTable",
                "Effect": "Allow",
                "Action": ["dynamodb:UpdateItem"],
                "Resource": table_arn,
            },
            {
                "Sid": "InvokeAgent",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
                "Resource": f"{args.agent_runtime_arn}*",
            },
        ],
        "Runs the Bedrock AgentCore call for the chat webapp's worker Lambda",
    )
    api_role_arn = _ensure_role(
        iam,
        "cloud-modernization-agent-api-role",
        [
            {
                "Sid": "ReadWriteJobTable",
                "Effect": "Allow",
                "Action": ["dynamodb:PutItem", "dynamodb:GetItem"],
                "Resource": table_arn,
            },
            {
                "Sid": "InvokeWorker",
                "Effect": "Allow",
                "Action": ["lambda:InvokeFunction"],
                "Resource": f"arn:aws:lambda:{region}:{account_id}:function:{args.worker_lambda_name}",
            },
        ],
        "Handles API Gateway requests for the chat webapp (fast path only)",
    )

    print("Deploying worker Lambda ...")
    lambda_client = session.client("lambda")
    worker_zip = _zip_files(["dynamo_helpers.py", "lambda_worker.py"])
    worker_arn = _ensure_lambda(
        lambda_client,
        args.worker_lambda_name,
        worker_role_arn,
        "lambda_worker.handler",
        worker_zip,
        {"JOBS_TABLE_NAME": args.table_name, "AGENT_RUNTIME_ARN": args.agent_runtime_arn},
        _WORKER_LAMBDA_TIMEOUT_SECONDS,
    )
    print(f"Worker Lambda ARN: {worker_arn}")
    _ensure_concurrency(lambda_client, args.worker_lambda_name, args.worker_concurrency)

    print("Deploying api Lambda ...")
    api_zip = _zip_files(["dynamo_helpers.py", "auth_helpers.py", "lambda_api.py"])
    api_lambda_arn = _ensure_lambda(
        lambda_client,
        args.api_lambda_name,
        api_role_arn,
        "lambda_api.handler",
        api_zip,
        {
            "JOBS_TABLE_NAME": args.table_name,
            "WORKER_FUNCTION_NAME": args.worker_lambda_name,
            "API_SHARED_SECRET": api_secret,
        },
        _API_LAMBDA_TIMEOUT_SECONDS,
    )
    print(f"Api Lambda ARN: {api_lambda_arn}")
    _ensure_concurrency(lambda_client, args.api_lambda_name, args.api_concurrency)

    print(f"Ensuring HTTP API '{args.api_name}' exists ...")
    apigw = session.client("apigatewayv2")
    api_id = _ensure_http_api(apigw, args.api_name)
    integration_id = _ensure_integration(apigw, api_id, api_lambda_arn)
    _ensure_route(apigw, api_id, "POST /chat", integration_id)
    _ensure_route(apigw, api_id, "GET /chat/{job_id}", integration_id)
    _ensure_stage(
        apigw,
        api_id,
        default_route_settings={"ThrottlingBurstLimit": args.poll_burst_limit, "ThrottlingRateLimit": args.poll_rate_limit},
        route_settings={
            "POST /chat": {"ThrottlingBurstLimit": args.chat_burst_limit, "ThrottlingRateLimit": args.chat_rate_limit},
            "GET /chat/{job_id}": {"ThrottlingBurstLimit": args.poll_burst_limit, "ThrottlingRateLimit": args.poll_rate_limit},
        },
    )
    _allow_apigateway_to_invoke(lambda_client, args.api_lambda_name, account_id, region, api_id)

    api_base_url = f"https://{api_id}.execute-api.{region}.amazonaws.com"

    print("\nDeployment complete.")
    print(f"API_BASE_URL={api_base_url}")
    print(f"API_SHARED_SECRET={api_secret}")
    print("\nKeep the shared secret private - anyone with it can invoke your agent (and run up Bedrock costs).")
    print("Set these as environment variables before running the Streamlit app (see README.md).")


if __name__ == "__main__":
    main()
