"""Terraform validation & security-scanning tool.

Replaces the deprecated standalone `terraform-mcp-server` (no longer
published in https://github.com/awslabs/mcp/tree/main/src - Terraform
support was dropped when the IaC servers were consolidated into
`aws-iac-mcp-server`, which only covers CDK + CloudFormation). This tool
restores equivalent "security compliance with Checkov" behavior described
in the original workflow by shelling out to the `terraform` and `checkov`
CLIs, which must be present in the runtime container (see Dockerfile).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from strands import tool

_SUBPROCESS_TIMEOUT_SECONDS = 90


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
        return proc.returncode, (proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired:
        return 1, f"Command timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s: {' '.join(cmd)}"
    except FileNotFoundError:
        return 127, f"Executable not found: {cmd[0]}"


@tool
def validate_terraform(terraform_code: str, filename: str = "main.tf") -> str:
    """Validate Terraform HCL syntax and scan it for security issues.

    Use this after generating Terraform code during the Implementation
    phase, before presenting it to the user as final output. Runs
    `terraform validate` (syntax/schema) and, when available, `checkov`
    (security & compliance scanning), mirroring the checks the deprecated
    terraform-mcp-server used to provide.

    Args:
        terraform_code: The full contents of a single .tf file to validate.
        filename: Name to give the file on disk, must end in .tf.

    Returns:
        A human-readable report combining terraform validate and checkov
        output, or a message explaining which tool(s) were unavailable.
    """
    if not filename.endswith(".tf"):
        filename += ".tf"

    terraform_bin = shutil.which("terraform")
    checkov_bin = shutil.which("checkov")

    if terraform_bin is None and checkov_bin is None:
        return (
            "Neither `terraform` nor `checkov` is installed in this runtime, so "
            "validation was skipped. Install both in the container image to "
            "enable this check (see Dockerfile), or review the code manually."
        )

    report_sections: list[str] = []
    with tempfile.TemporaryDirectory(prefix="tf-validate-") as tmp_dir:
        work_dir = Path(tmp_dir)
        (work_dir / filename).write_text(terraform_code, encoding="utf-8")

        if terraform_bin:
            init_code, init_out = _run([terraform_bin, "init", "-backend=false", "-input=false"], work_dir)
            if init_code != 0:
                report_sections.append("## terraform init\nFAILED\n```\n" + init_out.strip() + "\n```")
            else:
                val_code, val_out = _run([terraform_bin, "validate"], work_dir)
                status = "PASSED" if val_code == 0 else "FAILED"
                report_sections.append(f"## terraform validate\n{status}\n```\n{val_out.strip()}\n```")
        else:
            report_sections.append("## terraform validate\nSKIPPED - `terraform` CLI not installed")

        if checkov_bin:
            chk_code, chk_out = _run(
                [checkov_bin, "-d", str(work_dir), "--compact", "--quiet"], work_dir
            )
            status = "PASSED" if chk_code == 0 else "FINDINGS REPORTED"
            report_sections.append(f"## checkov security scan\n{status}\n```\n{chk_out.strip()}\n```")
        else:
            report_sections.append("## checkov security scan\nSKIPPED - `checkov` CLI not installed")

    return "\n\n".join(report_sections)
