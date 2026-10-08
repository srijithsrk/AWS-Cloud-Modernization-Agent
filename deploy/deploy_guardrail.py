"""Creates (or updates) the Bedrock Guardrail that restricts this agent to
AWS architecture, AWS cost estimates, and Terraform IaC topics, then
publishes a new immutable version of it.

Re-runnable: if a guardrail with --name already exists, its policy/messaging
is updated in place (DRAFT) and a new numbered version is published from
that updated DRAFT, so editing the denied-topic wording below and re-running
this script is the normal workflow.

Usage:
    python deploy/deploy_guardrail.py

Then wire the printed ID/version into the running agent:
    python deploy/deploy_runtime.py --image-uri ... --role-arn ... \
        --env GUARDRAIL_ID=<printed id> --env GUARDRAIL_VERSION=<printed version>

Also requires the executing IAM role/user to have `bedrock:ApplyGuardrail`
on the resulting guardrail ARN - see iam/execution-policy.json's
"ApplyTopicGuardrail" statement (fill in the account/region placeholders).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, for `import config`
import config

TOPIC_POLICY_CONFIG = {
    # IMPORTANT: Bedrock Guardrails' topic classifier cannot reliably handle a
    # NEGATED topic definition (e.g. "anything not about AWS") - confirmed in
    # production it instead matches on keyword/semantic overlap with the
    # definition's own text, so a prompt full of AWS terms gets DENIED by a
    # topic literally defined using those same AWS terms, regardless of the
    # "not" in the wording. Every topic below is instead a POSITIVE
    # description of a concrete thing to block, which is what the classifier
    # is actually designed to recognize.
    "topicsConfig": [
        {
            "name": "GeneralKnowledgeQuestions",
            "definition": (
                "Trivia or factual questions unrelated to AWS, such as geography, "
                "history, science, or current events."
            ),
            "examples": [
                "What is the capital of France",
                "Who won the world cup",
                "What year did WWII end",
                "How tall is Mount Everest",
            ],
            "type": "DENY",
        },
        {
            "name": "CreativeWritingRequests",
            "definition": "Requests to write creative content such as poems, stories, jokes, or lyrics.",
            "examples": [
                "Write me a poem about the ocean",
                "Tell me a joke",
                "Write a short story about a dragon",
            ],
            "type": "DENY",
        },
        {
            "name": "PersonalAdviceRequests",
            "definition": "Requests for personal, medical, legal, or financial advice.",
            "examples": [
                "Should I see a doctor for my headache",
                "Is this contract legally binding",
                "Should I invest in stocks",
            ],
            "type": "DENY",
        },
        # NOTE: a "NonAwsTechnologyRequests" topic (non-AWS clouds / unrelated
        # programming help) was tried and REMOVED - confirmed via Bedrock's
        # ApplyGuardrail API it's too broad: it matched generic technical/
        # programming language regardless of AWS-specificity, and even
        # flagged this agent's OWN system prompt (which discusses tool-calling
        # and AWS workflows) as a match, causing legitimate AWS questions
        # (e.g. "What does EC2 cost for a t3.medium") to be blocked. The
        # system prompt's own scope-restriction instruction is relied on
        # instead for that category - less airtight, but doesn't break real
        # usage. If revisiting this, verify ANY new topic against
        # agent.SYSTEM_PROMPT itself via apply_guardrail() before deploying.
    ]
}


def _find_guardrail_by_name(client, name: str) -> dict | None:
    next_token = None
    while True:
        kwargs = {"maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        response = client.list_guardrails(**kwargs)
        for summary in response.get("guardrails", []):
            if summary["name"] == name:
                return summary
        next_token = response.get("nextToken")
        if not next_token:
            return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="cloud-modernization-agent-scope-guardrail")
    parser.add_argument("--region", default=config.BEDROCK_REGION)
    args = parser.parse_args()

    client = boto3.client("bedrock", region_name=args.region)

    existing = _find_guardrail_by_name(client, args.name)
    if existing:
        guardrail_id = existing["id"]
        client.update_guardrail(
            guardrailIdentifier=guardrail_id,
            name=args.name,
            topicPolicyConfig=TOPIC_POLICY_CONFIG,
            blockedInputMessaging=config.OFF_TOPIC_DECLINE_MESSAGE,
            blockedOutputsMessaging=config.OFF_TOPIC_DECLINE_MESSAGE,
        )
        print(f"Updated existing guardrail '{args.name}' (id={guardrail_id}).")
    else:
        response = client.create_guardrail(
            name=args.name,
            description="Restricts the Cloud Modernization Agent to AWS architecture/cost/Terraform topics.",
            topicPolicyConfig=TOPIC_POLICY_CONFIG,
            blockedInputMessaging=config.OFF_TOPIC_DECLINE_MESSAGE,
            blockedOutputsMessaging=config.OFF_TOPIC_DECLINE_MESSAGE,
        )
        guardrail_id = response["guardrailId"]
        print(f"Created new guardrail '{args.name}' (id={guardrail_id}).")

    version_response = client.create_guardrail_version(guardrailIdentifier=guardrail_id)
    version = version_response["version"]

    print(f"\nGUARDRAIL_ID={guardrail_id}")
    print(f"GUARDRAIL_VERSION={version}")
    print(
        "\nPass these as --env GUARDRAIL_ID=... --env GUARDRAIL_VERSION=... to "
        "deploy/deploy_runtime.py, and ensure the execution role has "
        "bedrock:ApplyGuardrail on this guardrail's ARN (iam/execution-policy.json)."
    )


if __name__ == "__main__":
    main()
