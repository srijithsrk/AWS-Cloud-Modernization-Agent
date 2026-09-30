# Amazon Bedrock AgentCore Runtime requires linux/arm64 container images.
# Build with: docker buildx build --platform linux/arm64 -t <tag> --load .
# Pulled via Amazon ECR Public's mirror (not docker.io directly) to avoid
# Docker Hub's anonymous-pull rate limit (429 Too Many Requests), which
# CodeBuild's shared IP pool hits periodically.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

ARG TERRAFORM_VERSION=1.9.8

WORKDIR /app

# curl/unzip: fetch the Terraform CLI binary. ca-certificates: TLS for pip,
# uvx (PyPI) and the Terraform download.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        unzip \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Terraform CLI, used by tools/terraform_validate.py ("terraform validate").
RUN curl -fsSL -o /tmp/terraform.zip \
        "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_arm64.zip" \
    && unzip /tmp/terraform.zip -d /usr/local/bin \
    && rm /tmp/terraform.zip \
    && chmod +x /usr/local/bin/terraform

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Pre-warm the uv package cache for each AWS MCP server so the first real
# invocation does not pay a PyPI download. stdin is redirected from
# /dev/null and each attempt is capped with `timeout` because MCP servers
# speak stdio and would otherwise block forever waiting for a client
# handshake; a failure here is non-fatal, the server will just install
# lazily on first use instead. This mirrors the troubleshooting command
# documented at https://github.com/awslabs/mcp#for-windows.
RUN timeout 30s uvx mcp-proxy-for-aws-cli@latest < /dev/null > /dev/null 2>&1 || true \
    && timeout 60s uvx awslabs.aws-pricing-mcp-server@latest < /dev/null > /dev/null 2>&1 || true

COPY . .

EXPOSE 8080

# opentelemetry-instrument enables Bedrock AgentCore observability (traces
# visible in CloudWatch GenAI Observability) with no code changes required.
CMD ["opentelemetry-instrument", "python", "agent.py"]
