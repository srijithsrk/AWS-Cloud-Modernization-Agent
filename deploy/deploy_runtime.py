"""Creates (or, if a runtime with the same --runtime-name already exists,
updates it in place - same ARN, no webapp redeploy needed) the Bedrock
AgentCore Runtime for this agent, given an already-built and pushed ARM64
container image.

If a guardrail created by deploy_guardrail.py exists, it's wired in
automatically (its latest published version) unless --no-guardrail is
passed or GUARDRAIL_ID/GUARDRAIL_VERSION are already set via --env.

Usage:
    python deploy/deploy_runtime.py \
        --image-uri 123456789012.dkr.ecr.us-east-1.amazonaws.com/cloud-modernization-agent:latest \
        --role-arn arn:aws:iam::123456789012:role/cloud-modernization-agent-role
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, for `import deploy.deploy_guardrail`
from deploy.deploy_guardrail import DEFAULT_GUARDRAIL_NAME

# Baked-in default for Bedrock AgentCore Observability (see README's
# Observability section): keep full model/tool payloads on OTEL spans
# instead of the ADOT default redaction. `--env` can still override this if
# ever needed.
#
# NOTE: a per-agent-own-log-group span destination (UNIFIED_TRACES_DESTINATION_ENABLED)
# was evaluated but dropped - it requires aws-opentelemetry-distro>=0.18.0,
# which triggers a severe pip dependency-resolver backtrack against
# bedrock-agentcore in a clean install (confirmed: a fresh install backtracks
# through 20+ historical bedrock-agentcore releases trying to satisfy both
# constraints). Not worth the build fragility for a nice-to-have; spans
# still land in the shared aws/spans log group, and agent.py's explicit
# per-turn JSON log line already gives a complete, reliable conversation
# record independent of ADOT version.
DEFAULT_ENV_VARS = {
    "AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT": "true",
}


def _find_runtime_by_name(client, name: str) -> dict | None:
    next_token = None
    while True:
        kwargs = {"maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        response = client.list_agent_runtimes(**kwargs)
        for summary in response.get("agentRuntimes", []):
            if summary["agentRuntimeName"] == name:
                return summary
        next_token = response.get("nextToken")
        if not next_token:
            return None


def _find_latest_guardrail(region: str, name: str) -> tuple[str, str] | None:
    """Look up a guardrail by name and return (id, latest published version),
    skipping the mutable DRAFT version. Returns None if no guardrail with
    that name exists yet (e.g. deploy_guardrail.py hasn't been run)."""
    client = boto3.client("bedrock", region_name=region)

    guardrail_id = None
    next_token = None
    while guardrail_id is None:
        kwargs = {"maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        response = client.list_guardrails(**kwargs)
        for summary in response.get("guardrails", []):
            if summary["name"] == name:
                guardrail_id = summary["id"]
                break
        next_token = response.get("nextToken")
        if not next_token:
            break
    if guardrail_id is None:
        return None

    versions: list[int] = []
    next_token = None
    while True:
        kwargs = {"guardrailIdentifier": guardrail_id, "maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        response = client.list_guardrails(**kwargs)
        versions.extend(int(g["version"]) for g in response.get("guardrails", []) if g["version"] != "DRAFT")
        next_token = response.get("nextToken")
        if not next_token:
            break
    if not versions:
        return None
    return guardrail_id, str(max(versions))


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
    parser.add_argument(
        "--no-guardrail",
        action="store_true",
        help="Skip auto-wiring the guardrail created by deploy_guardrail.py, even if one exists.",
    )
    args = parser.parse_args()

    env_vars = {**DEFAULT_ENV_VARS, **dict(kv.split("=", 1) for kv in args.env)}

    if args.no_guardrail:
        print("--no-guardrail passed: deploying without topic restriction.")
    elif "GUARDRAIL_ID" in env_vars:
        print(f"Using explicitly-provided guardrail: GUARDRAIL_ID={env_vars['GUARDRAIL_ID']}")
    else:
        found = _find_latest_guardrail(args.region, DEFAULT_GUARDRAIL_NAME)
        if found:
            guardrail_id, guardrail_version = found
            env_vars["GUARDRAIL_ID"] = guardrail_id
            env_vars["GUARDRAIL_VERSION"] = guardrail_version
            print(f"Auto-wiring guardrail '{DEFAULT_GUARDRAIL_NAME}': GUARDRAIL_ID={guardrail_id} GUARDRAIL_VERSION={guardrail_version}")
        else:
            print(
                f"No guardrail named '{DEFAULT_GUARDRAIL_NAME}' found - deploying without topic "
                "restriction. Run deploy/deploy_guardrail.py first to enable it (recommended), "
                "or pass --no-guardrail to silence this message."
            )

    client = boto3.client("bedrock-agentcore-control", region_name=args.region)
    artifact = {"containerConfiguration": {"containerUri": args.image_uri}}

    existing = _find_runtime_by_name(client, args.runtime_name)
    if existing:
        response = client.update_agent_runtime(
            agentRuntimeId=existing["agentRuntimeId"],
            agentRuntimeArtifact=artifact,
            networkConfiguration={"networkMode": "PUBLIC"},
            roleArn=args.role_arn,
            environmentVariables=env_vars,
        )
        print(f"Updated existing agent runtime '{args.runtime_name}' in place (ARN unchanged).")
    else:
        response = client.create_agent_runtime(
            agentRuntimeName=args.runtime_name,
            agentRuntimeArtifact=artifact,
            networkConfiguration={"networkMode": "PUBLIC"},
            roleArn=args.role_arn,
            environmentVariables=env_vars,
        )
        print("Created new agent runtime.")


    print(f"Agent Runtime ARN: {response['agentRuntimeArn']}")
    print(f"Status: {response.get('status', 'UNKNOWN')}")


if __name__ == "__main__":
    main()
