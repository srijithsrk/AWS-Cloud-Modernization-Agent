"""Central configuration for the Cloud Modernization Agent.

All values can be overridden via environment variables so the same code
runs unchanged locally, in tests, and inside Bedrock AgentCore Runtime.
"""
from __future__ import annotations

import os


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Foundation model
# ---------------------------------------------------------------------------
# Default: Claude Sonnet 4.5 ($3.00 / $15.00 per 1M input/output tokens,
# same price tier as every other current Sonnet generation). Chosen by
# weighing cost, security/governance, and real measured performance across
# the realistic Bedrock candidates for this agent's heavy multi-tool
# workflow (docs search -> pricing -> diagram -> Terraform validation, often
# several chained per request).
#
# IMPORTANT: Claude 3.7 Sonnet (the previous default) was fully retired by
# AWS - not just moved to LEGACY, removed entirely - causing a real
# ResourceNotFoundException: "This model version has reached the end of its
# life." Bedrock model IDs/inference profiles DO get retired over time.
# Before ever changing this again: verify the target ID is ACTUALLY ACTIVE
# in this account/region first with
#   aws bedrock list-foundation-models --region us-east-1 --by-provider anthropic \
#     --query "modelSummaries[].{Id:modelId,Status:modelLifecycle.status}" --output table
# and confirm it actually responds with a real (non-deployed) invoke *before*
# touching this file or redeploying:
#   aws bedrock-runtime converse --model-id <id> --region us-east-1 \
#     --messages file://messages.json   (a small JSON file, NOT an inline
#     string - PowerShell mangles escaped JSON on the command line)
#
#   Model                          $/1M in   $/1M out  Notes
#   amazon.nova-micro/-lite-v1:0    0.03-0.06  0.14-0.24  Cheapest, but same
#                                                          tool-calling lineage
#                                                          as Nova Pro (below)
#   amazon.nova-pro-v1:0            0.80       3.20       PROVEN BROKEN - real
#                                                          CloudWatch logs show
#                                                          repeated
#                                                          ModelErrorException:
#                                                          "Model produced
#                                                          invalid sequence as
#                                                          part of ToolUse",
#                                                          reproducible on the
#                                                          same multi-tool
#                                                          request, NOT fixed
#                                                          by disabling
#                                                          streaming or a retry
#   amazon.nova-premier-v1:0        2.50       12.50      Untested; same Nova
#                                                          tool-use lineage
#   anthropic.claude-3-5-haiku      0.80       4.00       Untested here, but a
#                                                          real, ~3.75x cheaper
#                                                          fallback if cost
#                                                          matters more - same
#                                                          vendor/tool-use
#                                                          lineage as Sonnet
#   anthropic.claude-sonnet-4-5      3.00       15.00     PROVEN WORKING - real
#      (DEFAULT)                                          diagram + pricing
#                                                          tool calls succeeded
#                                                          end-to-end (on
#                                                          Claude 3.7 Sonnet,
#                                                          since retired; this
#                                                          Sonnet generation
#                                                          verified ACTIVE and
#                                                          invocable directly
#                                                          before switching)
#   anthropic.claude-opus-4-*       15.00       75.00     Overkill; no evidence
#                                                          this task needs it
#
# Security/governance: Bedrock's data handling (no training on customer data,
# encryption at rest/in transit, IAM/VPC-scoped access) is IDENTICAL for every
# model above regardless of vendor. The only real difference is
# procurement/vendor-risk posture: Nova has no third-party subprocessor (it's
# Amazon's own model); Claude is a third-party model hosted inside the same
# AWS security boundary. For most use cases this isn't a technical security
# gap, just a compliance-framework nuance worth knowing about.
#
# Given proven reliability only exists for a current Claude Sonnet in this
# agent's actual usage, that's the default. To trade reliability for cost,
# set (after verifying it's still ACTIVE - see above):
#   BEDROCK_MODEL_ID=anthropic.claude-3-5-haiku-20241022-v1:0   (cheaper, same vendor)
#   BEDROCK_MODEL_ID=amazon.nova-pro-v1:0                        (cheapest, proven broken here)
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
BEDROCK_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

# Keep responses deterministic-ish and bounded; architecture/cost answers
# don't benefit from high temperature creativity.
MODEL_TEMPERATURE = float(os.environ.get("BEDROCK_MODEL_TEMPERATURE", "0.2"))
# 4096 was too low in practice - a full multi-resource Terraform file (e.g.
# S3 + CloudFront + ACM + Route53) plus explanation routinely exceeds it,
# causing a MaxTokensReachedException mid-response. 8192 is the safe ceiling
# for Claude on Bedrock's Converse API without needing special beta headers.
# agent.py also auto-continues generation if this is still hit, so raising
# this doesn't remove that safety net - it just makes hitting it less likely.
MODEL_MAX_TOKENS = int(os.environ.get("BEDROCK_MODEL_MAX_TOKENS", "8192"))

# ---------------------------------------------------------------------------
# Guardrail (topic restriction)
# ---------------------------------------------------------------------------
# Confines the agent to its intended purpose (AWS architecture/cost/Terraform
# only) and gives a hard, cost-saving stop for anything else: a Bedrock
# Guardrail evaluates the user's input BEFORE the model is invoked, so an
# off-topic message never triggers a (potentially multi-tool-call, multi-
# dollar) agent loop at all. See deploy/deploy_guardrail.py to create/update
# the guardrail resource itself. Left unset by default so the agent still
# runs fine locally/in tests without one provisioned.
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION")

# Shown to the user both when the guardrail blocks an off-topic input AND,
# as a second line of defense, by the system prompt instructing the model to
# decline the same way itself - see agent.py's SYSTEM_PROMPT.
OFF_TOPIC_DECLINE_MESSAGE = (
    "I'm an AWS Cloud Modernization Agent, so I can only help with AWS "
    "architecture design, AWS cost estimates, and generating Terraform for "
    "AWS deployments. Could you rephrase your question around one of those topics?"
)

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------
# Terraform validation shells out to the `terraform` and `checkov` CLIs.
# Disable if those binaries are not installed in your runtime image.
ENABLE_TERRAFORM_VALIDATION = _env_bool("ENABLE_TERRAFORM_VALIDATION", True)

# awslabs.aws-pricing-mcp-server accepts an optional AWS_PROFILE env var for
# users authenticating with a named CLI profile. IMPORTANT: only set this
# when AWS_PROFILE is genuinely configured. Forcing a literal "default"
# value (as its docs' example configs do) breaks it under Bedrock AgentCore
# Runtime, which authenticates via the attached IAM execution role rather
# than a ~/.aws profile file - the server then fails immediately with "The
# config profile (default) could not be found" instead of falling through
# to the role's credentials.
AWS_PROFILE_FOR_PRICING = os.environ.get("AWS_PROFILE")


def _forward_aws_credentials() -> dict:
    """Forward this process's ambient AWS-related env vars to a spawned MCP
    server subprocess.

    mcp's stdio transport only inherits a minimal, safe subset of the
    parent environment by default - it does NOT automatically pass through
    whatever this process is authenticating with. Without this, MCP
    servers that make real AWS API calls (the "aws" server,
    aws-pricing-mcp-server) fail with credential errors even when this
    process is authenticated fine.

    This forwards every AWS_* env var rather than a fixed allowlist,
    because "how this process is authenticated" varies by environment:
    plain AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN when
    testing locally, but a *different* set (container credential endpoint
    vars such as AWS_CONTAINER_CREDENTIALS_RELATIVE_URI/
    AWS_CONTAINER_AUTHORIZATION_TOKEN) when running under the AgentCore
    Runtime execution role. AWS_PROFILE is deliberately excluded: forwarding
    a profile name to a server that expects env-var/role credentials can
    make it try (and fail) to resolve a named profile that doesn't exist on
    disk.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key.startswith("AWS_") and key != "AWS_PROFILE" and value
    }


def _optional_profile_env() -> dict:
    """Only include AWS_PROFILE when one is genuinely configured - see the
    AWS_PROFILE_FOR_PRICING comment above for why forcing a value is unsafe."""
    return {"AWS_PROFILE": AWS_PROFILE_FOR_PRICING} if AWS_PROFILE_FOR_PRICING else {}

# ---------------------------------------------------------------------------
# MCP server definitions
# ---------------------------------------------------------------------------
# Same shape as the `mcpServers` block in Kiro/Cline's `mcp.json`, so it can
# be loaded directly with `strands.tools.mcp.MCPClient.load_servers()`.
#
# This agent uses the official Agent Toolkit for AWS
# (https://github.com/aws/agent-toolkit-for-aws) as its primary AWS
# knowledge/API tool - the documented successor to the individual AWS Labs
# MCP servers the original blog references. Two older servers are
# intentionally NOT used here:
#   - awslabs.aws-documentation-mcp-server -> superseded by aws___search_documentation /
#                                             aws___read_documentation below
#   - aws-knowledge-mcp-server              -> AWS's own setup guide says to remove this
#                                             one in favor of the AWS MCP Server to avoid
#                                             duplicate/conflicting tools, see:
#                                             https://docs.aws.amazon.com/agent-toolkit/latest/userguide/getting-started-aws-mcp-server.html
#
# awslabs.aws-pricing-mcp-server IS still used (rather than the AWS MCP
# Server's aws___run_script) because in practice the run_script sandbox
# blocks `import boto3` outright, even for read-only pricing lookups - so it
# cannot actually produce real cost estimates. The dedicated pricing server
# runs as its own process with normal boto3 access and no such restriction.
#
# There is deliberately no CDK/CloudFormation IaC MCP server here - this
# agent only ever generates Terraform for deployment code (see agent.py's
# system prompt), validated locally by tools/terraform_validate.py instead.
#
# `continue_on_error` means a server that fails to start (e.g. a transient
# network blip downloading the package) is skipped instead of crashing the
# whole agent; see mcp_setup.py for the runtime-connection-failure handling.
MCP_SERVERS = {
    # Official AWS MCP Server (Agent Toolkit for AWS). Authenticates over
    # SigV4 using this process's ambient AWS credentials (the AgentCore
    # execution role in production) via the MCP Proxy for AWS CLI - no
    # interactive OAuth login required, which is what makes it suitable for
    # a headless/automated agent. Exposes: aws___search_documentation,
    # aws___read_documentation, aws___retrieve_skill (curated AWS "skills",
    # the successor to the blog's Agent SOPs), aws___list_regions,
    # aws___get_regional_availability, and aws___run_script (sandboxed
    # Python - reliable for read-only discovery that doesn't need boto3,
    # unreliable for anything that does; see aws-pricing-mcp-server below
    # for real cost estimates).
    "aws": {
        "command": "uvx",
        "args": [
            "mcp-proxy-for-aws-cli@latest",
            "https://aws-mcp.us-east-1.api.aws/mcp",
            "--metadata",
            "AWS_REGION=" + BEDROCK_REGION,
        ],
        "env": {**_forward_aws_credentials(), "AWS_REGION": BEDROCK_REGION},
        "startup_timeout": 90,  # first connection can take a while per AWS's own docs
        "continue_on_error": True,
        # Its tools are already namespaced (aws___search_documentation, etc.),
        # so skip mcp_setup.py's automatic server-name prefixing here.
        "prefix": "",
    },
    "awslabs.aws-pricing-mcp-server": {
        "command": "uvx",
        "args": ["awslabs.aws-pricing-mcp-server@latest"],
        "env": {
            **_forward_aws_credentials(),
            **_optional_profile_env(),
            "FASTMCP_LOG_LEVEL": "ERROR",
        },
        "continue_on_error": True,
        "prefix": "",
    },
}
