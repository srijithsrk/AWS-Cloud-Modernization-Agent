"""Creates the IAM execution role Bedrock AgentCore Runtime assumes to run
this agent. Run this once before the first deployment.

Usage:
    python deploy/create_role.py --role-name cloud-modernization-agent-role
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import boto3

_IAM_DIR = Path(__file__).resolve().parent.parent / "iam"


def _load_policy(filename: str, account_id: str, region: str) -> dict:
    text = (_IAM_DIR / filename).read_text(encoding="utf-8")
    text = text.replace("<YOUR_ACCOUNT_ID>", account_id).replace("<YOUR_REGION>", region)
    return json.loads(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role-name", default="cloud-modernization-agent-role")
    parser.add_argument("--region", default=None, help="Defaults to the boto3 session's region")
    args = parser.parse_args()

    session = boto3.session.Session(region_name=args.region)
    region = session.region_name
    account_id = session.client("sts").get_caller_identity()["Account"]
    iam = session.client("iam")

    trust_policy = _load_policy("trust-policy.json", account_id, region)
    execution_policy = _load_policy("execution-policy.json", account_id, region)

    try:
        create_resp = iam.create_role(
            RoleName=args.role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Execution role for the Cloud Modernization Bedrock AgentCore agent",
        )
        role_arn = create_resp["Role"]["Arn"]
        print(f"Created role: {role_arn}")
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=args.role_name)["Role"]["Arn"]
        print(f"Role already exists, reusing: {role_arn}")
        iam.update_assume_role_policy(
            RoleName=args.role_name, PolicyDocument=json.dumps(trust_policy)
        )

    iam.put_role_policy(
        RoleName=args.role_name,
        PolicyName="cloud-modernization-agent-execution-policy",
        PolicyDocument=json.dumps(execution_policy),
    )
    print("Attached/updated inline execution policy.")
    print(f"\nROLE_ARN={role_arn}")


if __name__ == "__main__":
    main()
