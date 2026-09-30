"""Creates (or updates) the Bedrock AgentCore Runtime for this agent, given
an already-built and pushed ARM64 container image.

Usage:
    python deploy/deploy_runtime.py \
        --image-uri 123456789012.dkr.ecr.us-east-1.amazonaws.com/cloud-modernization-agent:latest \
        --role-arn arn:aws:iam::123456789012:role/cloud-modernization-agent-role
"""
from __future__ import annotations

import argparse

import boto3


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-uri", required=True)
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--runtime-name", default="cloud_modernization_agent")
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Environment variable to set on the runtime, e.g. --env BEDROCK_MODEL_ID=amazon.nova-pro-v1:0. Repeatable.",
    )
    args = parser.parse_args()

    env_vars = dict(kv.split("=", 1) for kv in args.env)

    client = boto3.client("bedrock-agentcore-control", region_name=args.region)

    kwargs = dict(
        agentRuntimeName=args.runtime_name,
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": args.image_uri}},
        networkConfiguration={"networkMode": "PUBLIC"},
        roleArn=args.role_arn,
    )
    if env_vars:
        kwargs["environmentVariables"] = env_vars

    try:
        response = client.create_agent_runtime(**kwargs)
        print("Created new agent runtime.")
    except client.exceptions.ConflictException:
        print(
            f"A runtime named '{args.runtime_name}' already exists. Delete it first with:\n"
            f"  aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id <id>\n"
            "or re-run with a different --runtime-name."
        )
        raise

    print(f"Agent Runtime ARN: {response['agentRuntimeArn']}")
    print(f"Status: {response.get('status', 'UNKNOWN')}")


if __name__ == "__main__":
    main()
