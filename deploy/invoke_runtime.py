"""Invoke a deployed Bedrock AgentCore agent runtime.

Single-shot usage:
    python deploy/invoke_runtime.py \
        --runtime-arn arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/cloud_modernization_agent-abc123 \
        --prompt "Design a cost-optimized 3-tier web app on AWS and generate the Terraform for it"

Interactive chat (keeps asking follow-up questions in the same session):
    python deploy/invoke_runtime.py \
        --runtime-arn arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/cloud_modernization_agent-abc123 \
        --chat
"""
from __future__ import annotations

import argparse
import json
import secrets

import boto3


def _invoke(client, runtime_arn: str, session_id: str, prompt: str) -> dict:
    payload = json.dumps({"prompt": prompt}).encode()
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        payload=payload,
        qualifier="DEFAULT",
    )
    return json.loads(response["response"].read())


def _run_chat(client, runtime_arn: str, session_id: str) -> None:
    print(f"Chat session {session_id} - reusing this session id keeps conversation history.")
    print("Type your message and press Enter. Ctrl+C or an empty line to quit.\n")
    while True:
        try:
            prompt = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not prompt:
            break
        result = _invoke(client, runtime_arn, session_id, prompt)
        if "warning" in result:
            print(f"[warning] {result['warning']}")
        message = result.get("result", result)
        print(f"agent> {json.dumps(message, indent=2)}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime-arn", required=True)
    parser.add_argument("--prompt", help="Single message to send (omit when using --chat).")
    parser.add_argument("--chat", action="store_true", help="Start an interactive multi-turn chat instead.")
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--session-id",
        default=None,
        help="Must be 33+ characters. A random one is generated if omitted. "
        "Reuse the same value across calls to continue a conversation.",
    )
    args = parser.parse_args()
    if not args.chat and not args.prompt:
        parser.error("either --prompt or --chat is required")

    session_id = args.session_id or secrets.token_hex(20)  # 40 hex chars, safely over the 33 minimum
    client = boto3.client("bedrock-agentcore", region_name=args.region)

    if args.chat:
        _run_chat(client, args.runtime_arn, session_id)
    else:
        print(json.dumps(_invoke(client, args.runtime_arn, session_id, args.prompt), indent=2))


if __name__ == "__main__":
    main()
