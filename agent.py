"""Amazon Bedrock AgentCore entrypoint for the Cloud Modernization Agent.

This agent replicates the agentic cloud-modernization workflow described in
"Agentic Cloud Modernization: Accelerating Modernization with AWS MCPs and
Kiro" (AWS Migration & Modernization blog), but as a standalone Bedrock
AgentCore agent instead of an IDE copilot: given a prompt, it can analyze an
existing system, design a target AWS architecture (with diagrams and cost
estimates), and generate production-ready Infrastructure as Code.

Run locally:
    python agent.py
    curl -X POST http://localhost:8080/invocations \
      -H "Content-Type: application/json" \
      -d '{"prompt": "Design a cost-optimized 3-tier web app on AWS and give me Terraform"}'

Deploy: see README.md.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict

from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from strands import Agent
from strands.models import BedrockModel
from strands.types.agent import Limits
from strands.types.exceptions import EventLoopException, MaxTokensReachedException

import config
from mcp_setup import mcp_tools_session
from tools.diagrams import generate_architecture_diagram
from tools.repo_analyzer import analyze_repository
from tools.terraform_validate import validate_terraform

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """\
You are an AWS Cloud Modernization Agent. You help engineers analyze existing
systems, design target AWS architectures, and generate deployment-ready
Infrastructure as Code, following the same three-phase workflow a senior AWS
architect uses.

Never expose your internal reasoning in your answer - do not write
`<thinking>` tags or similar chain-of-thought preambles into the response
text. Reason privately, then give only the final, direct answer.

When a request asks for multiple things (e.g. "diagram, cost, and code"),
you MUST address every part of it using the matching tool below - never
silently drop one part just because another tool call failed or errored.
Producing a diagram never depends on pricing or code succeeding, and vice
versa; treat them as independent sub-tasks.

1. Analysis Phase - When given existing code/config, use `analyze_repository`
   to summarize current architecture, dependencies, and modernization
   opportunities. Use `generate_architecture_diagram` to draw the CURRENT
   state.
2. Planning Phase - Use `aws___search_documentation`, `aws___read_documentation`,
   and `aws___retrieve_skill` (the AWS MCP Server's curated, evaluated
   procedures for AWS tasks) to ground recommendations in current AWS best
   practices and the Well-Architected Framework. Use `aws___list_regions` /
   `aws___get_regional_availability` to confirm a service is available where
   you're proposing to use it. Use `generate_architecture_diagram` (a local
   tool, unrelated to any AWS API) to draw the TARGET state - always call it
   when a diagram is requested, regardless of what happens with other tools.

   For cost estimates, use the aws-pricing-mcp-server (NOT `aws___run_script`)
   and follow this EXACT workflow - skipping steps is the #1 cause of
   failed pricing lookups:
     a. Call `get_pricing_service_attributes(service_code)` to see which
        fields you can even filter on for that service (e.g. AmazonS3's
        fields are NOT the same as EC2's, and you cannot guess them).
     b. Call `get_pricing_attribute_values(service_code, [attribute_names])`
        to get the EXACT valid values for the fields you plan to filter on.
        NEVER pass a filter value (e.g. a storageClass, requestType, or
        instanceType) to `get_pricing` unless it came verbatim from this
        call - guessed values like "STANDARD" or "GET" almost always differ
        from the API's actual enum (e.g. it may be "General Purpose" or
        "Get Requests") and produce an `empty_results` error.
     c. Call `get_pricing(service_code, region, filters)` with only the
        confirmed values. Omit the `region` argument entirely for global
        services like `AWSDataTransfer` or `AmazonCloudFront` - passing a
        region for these causes empty results. ALWAYS pass
        `output_options={"pricing_terms": ["OnDemand"]}` (not just for
        large services) to keep the response small - this is a long-running
        chat, and unfiltered pricing responses bloat every later turn's
        context unnecessarily.
     d. If `get_pricing` still errors, call `get_pricing_attribute_values`
        ONCE more to correct the specific field that failed, then retry
        `get_pricing` ONCE - do not keep guessing filter values in a loop.
        For simple/small services, prefer fewer filters (or none beyond
        region) over risking a wrong guess.
   State your usage assumptions (storage, requests, data transfer, etc.)
   explicitly alongside the resulting numeric cost.
3. Implementation Phase - Generate Infrastructure as Code. ALWAYS write
   Terraform (HCL) for deployment code - never CDK, CloudFormation, Pulumi,
   or any other IaC tool, even if the user's phrasing suggests one of those
   (e.g. "deployment code", "IaC", "CDK", "CloudFormation" all mean
   Terraform here). Ground the HCL in `aws___search_documentation` for the
   specific resources involved, write idiomatic HCL yourself, and then you
   MUST call `validate_terraform` on it before presenting it as your final
   answer - never show Terraform code you have not run through that tool,
   even if you are confident it is correct.

`aws___run_script` cannot reliably access AWS APIs in this environment - its
sandbox blocks `boto3` imports even for read-only calls, so do not use it
for pricing, resource discovery, or anything else; use the dedicated tools
above instead (aws-pricing-mcp-server for cost, aws___search_documentation/
aws___list_regions for everything else). Never use any tool to provision,
deploy, or modify real infrastructure - when the user asks for "code",
write the actual Terraform source yourself as your answer's text; do not
try to execute it against AWS.

Always state your assumptions explicitly (region, scale, compliance
requirements) when the user hasn't specified them, and prefer secure
defaults (encryption at rest/in transit, least-privilege IAM, private
networking) in every design and code sample you produce.

Call each tool at most once per phase unless its previous result was an
error. Never call the same tool with the same (or equivalent) arguments
more than once - reuse the result you already have and move on to the next
phase instead. For example, once `generate_architecture_diagram` has
returned a diagram, do not call it again for the same architecture; once
you have searched documentation and found (or exhausted) relevant results,
move on rather than re-running similar searches. If you notice you are
about to repeat a call you already made, stop and produce your final
answer using what you already know. This "don't repeat a call" rule
applies across the whole conversation, not just the current message - if
an earlier turn already produced a diagram, cost estimate, or Terraform for
the same architecture, reuse it when answering a follow-up instead of
regenerating it; only call a tool again if the follow-up genuinely changes
its inputs (e.g. a different region or service).

Never estimate or guess AWS costs from memory. Only report a cost figure
that came from actually calling `get_pricing` in this conversation, using
filter values you first confirmed via `get_pricing_attribute_values` per the
workflow above. If no pricing tool is available, or it still errors after
one corrective retry, say so explicitly instead of fabricating a number.
"""

# Hard cap on the agentic loop so a model that gets stuck repeating a tool
# call (rather than progressing) fails fast instead of silently running up
# Bedrock usage. `turns` bounds loop iterations; `total_tokens` is a backstop
# in case a single turn is unusually expensive. Each turn resends the full
# conversation so far, so verbose tool results (pricing discovery calls, IaC
# doc/sample search, full Terraform files) make token usage grow faster than
# the turn count alone suggests - especially since this is a multi-turn
# CHAT agent where the whole session's history accumulates before each new
# turn even starts. Sized generously so legitimate long conversations don't
# get cut off; still a real backstop against genuine runaway/repeated-call
# loops (the original bug this was added for). Tune via env vars.
_AGENT_LIMITS = Limits(
    turns=int(os.environ.get("AGENT_MAX_TURNS", "30")),
    total_tokens=int(os.environ.get("AGENT_MAX_TOTAL_TOKENS", "600000")),
)

# How many times to transparently let the model continue a single response
# that got cut off by hitting its own per-turn output token limit (Strands'
# MaxTokensReachedException - the partial message is already saved to
# history, so calling the agent again with no new prompt resumes generation
# from exactly where it stopped). This must never surface as a raw 500 to a
# caller - a conversational agent generating a long Terraform file hitting
# an output limit is an expected, recoverable event, not a server error.
_MAX_CONTINUATIONS = int(os.environ.get("AGENT_MAX_CONTINUATIONS", "3"))

_model = BedrockModel(
    model_id=config.BEDROCK_MODEL_ID,
    region_name=config.BEDROCK_REGION,
    # Non-streaming Converse instead of ConverseStream: real CloudWatch logs
    # showed intermittent `modelStreamErrorException: Model produced invalid
    # sequence as part of ToolUse` from amazon.nova-pro-v1:0 specifically
    # during streaming tool-use generation. Converse still returns the same
    # message/tool-call structure to Strands (see BedrockModel's
    # convert_non_streaming_to_streaming), so this doesn't change behavior
    # for this agent, which only reads the final result anyway.
    streaming=False,
    temperature=config.MODEL_TEMPERATURE,
    max_tokens=config.MODEL_MAX_TOKENS,
)

_CUSTOM_TOOLS = [generate_architecture_diagram, analyze_repository]
if config.ENABLE_TERRAFORM_VALIDATION:
    _CUSTOM_TOOLS.append(validate_terraform)

# Per-session conversation history so follow-up questions on the same
# AgentCore runtime session (same `runtimeSessionId`) continue the same
# chat instead of starting fresh every call - see README's "Follow-up
# questions" section for how callers opt into this. This is a simple
# in-process dict, NOT durable storage: history is lost if this container
# restarts or a later turn is routed to a different container instance.
# That's an acceptable tradeoff for interactive testing; for persistence
# across restarts, use bedrock_agentcore.memory (AgentCore Memory) instead.
_MAX_SESSIONS = int(os.environ.get("AGENT_MAX_SESSIONS", "50"))
_session_lock = threading.Lock()
_session_messages: "OrderedDict[str, list]" = OrderedDict()


def _get_session_messages(session_id: str | None) -> list:
    """Return the mutable message-history list for a session, creating one
    on first use. Passing `None` (no session id supplied) yields a fresh,
    unshared list each call, matching the old single-shot behavior."""
    if session_id is None:
        return []
    with _session_lock:
        if session_id not in _session_messages:
            if len(_session_messages) >= _MAX_SESSIONS:
                _session_messages.popitem(last=False)  # evict oldest session
            _session_messages[session_id] = []
        _session_messages.move_to_end(session_id)
        return _session_messages[session_id]


def _invoke_with_recovery(agent: Agent, user_message: str):
    """Call the agent, transparently recovering from two expected,
    non-bug conditions instead of letting them surface as a raw 500:

    - `EventLoopException` (a transient Bedrock/model error, e.g. the
      modelStreamErrorException seen in production) - retried once with
      the same user turn.
    - `MaxTokensReachedException` (the model ran out of its per-turn output
      token budget mid-response, e.g. generating a long Terraform file) -
      the partial message is already saved to `agent.messages`, so calling
      the agent again with no new prompt (`agent(None, ...)`) resumes
      generation exactly where it stopped. Bounded by `_MAX_CONTINUATIONS`
      so a response that's pathologically too long still fails eventually
      rather than looping forever.
    """
    prompt: str | None = user_message
    event_loop_retried = False
    continuations = 0
    while True:
        try:
            return agent(prompt, limits=_AGENT_LIMITS)
        except EventLoopException:
            if event_loop_retried:
                raise
            logger.warning("Event loop error, retrying once", exc_info=True)
            event_loop_retried = True
            prompt = user_message
        except MaxTokensReachedException:
            if continuations >= _MAX_CONTINUATIONS:
                raise
            continuations += 1
            logger.warning(
                "Model hit its output token limit mid-response; auto-continuing (attempt %d/%d)",
                continuations,
                _MAX_CONTINUATIONS,
            )
            prompt = None


@app.entrypoint
def invoke(payload: dict, context: RequestContext | None = None) -> dict:
    """AgentCore Runtime entrypoint: one call per user turn.

    Calls sharing the same `runtimeSessionId` continue the same
    conversation - prior messages (including tool results) are replayed to
    the model so it can answer follow-ups without redoing work already
    done earlier in the session.
    """
    user_message = payload.get("prompt")
    if not user_message:
        return {"error": "Missing 'prompt' in request payload."}

    session_id = getattr(context, "session_id", None)
    history = _get_session_messages(session_id)

    with mcp_tools_session() as mcp_tools:
        agent = Agent(
            model=_model,
            tools=[*mcp_tools, *_CUSTOM_TOOLS],
            system_prompt=SYSTEM_PROMPT,
            messages=history,
        )
        try:
            result = _invoke_with_recovery(agent, user_message)
        except MaxTokensReachedException:
            logger.warning("Still truncated after %d auto-continuation attempts", _MAX_CONTINUATIONS)
            return {
                "result": agent.messages[-1] if agent.messages else None,
                "warning": (
                    f"The response was too long to complete even after automatically continuing "
                    f"{_MAX_CONTINUATIONS} times. Try asking for a smaller piece (e.g. one resource "
                    "at a time), or raise BEDROCK_MODEL_MAX_TOKENS / AGENT_MAX_CONTINUATIONS."
                ),
            }

    if result.stop_reason in ("limit_turns", "limit_total_tokens", "limit_output_tokens"):
        logger.warning("Agent hit a loop limit (%s) before finishing.", result.stop_reason)
        return {
            "result": result.message,
            "warning": (
                f"Stopped early ({result.stop_reason}) before producing a complete answer. "
                "Try a narrower prompt (e.g. one phase at a time), or raise AGENT_MAX_TURNS / "
                "AGENT_MAX_TOTAL_TOKENS if this is a legitimately large request."
            ),
        }

    return {"result": result.message}


if __name__ == "__main__":
    app.run()
