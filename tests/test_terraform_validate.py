"""Unit tests for tools.terraform_validate.

subprocess and shutil.which are mocked so these tests run without the
`terraform` / `checkov` binaries installed (they are only present in the
Docker image, not necessarily on a developer's machine or CI runner).
"""
from unittest.mock import patch, MagicMock

from tools.terraform_validate import validate_terraform

_SAMPLE_TF = 'resource "aws_s3_bucket" "example" {\n  bucket = "example"\n}\n'


def _fake_completed_process(returncode: int, output: str) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = output
    proc.stderr = ""
    return proc


def test_reports_when_no_binaries_installed():
    with patch("tools.terraform_validate.shutil.which", return_value=None):
        report = validate_terraform(_SAMPLE_TF)
    assert "Neither `terraform` nor `checkov`" in report


def test_reports_passed_validation_and_scan():
    def fake_which(name):
        return f"/usr/local/bin/{name}"

    def fake_run(cmd, cwd, capture_output, text, timeout, check):
        if cmd[1] == "init":
            return _fake_completed_process(0, "Terraform has been successfully initialized!")
        if cmd[1] == "validate":
            return _fake_completed_process(0, "Success! The configuration is valid.")
        return _fake_completed_process(0, "Passed checks: 5, Failed checks: 0")

    with patch("tools.terraform_validate.shutil.which", side_effect=fake_which), patch(
        "tools.terraform_validate.subprocess.run", side_effect=fake_run
    ):
        report = validate_terraform(_SAMPLE_TF)

    assert "terraform validate\nPASSED" in report
    assert "checkov security scan\nPASSED" in report


def test_reports_checkov_findings():
    def fake_which(name):
        return f"/usr/local/bin/{name}"

    def fake_run(cmd, cwd, capture_output, text, timeout, check):
        if cmd[1] == "init":
            return _fake_completed_process(0, "ok")
        if cmd[1] == "validate":
            return _fake_completed_process(0, "Success!")
        return _fake_completed_process(1, "Failed checks: 1 - CKV_AWS_1 S3 bucket not encrypted")

    with patch("tools.terraform_validate.shutil.which", side_effect=fake_which), patch(
        "tools.terraform_validate.subprocess.run", side_effect=fake_run
    ):
        report = validate_terraform(_SAMPLE_TF)

    assert "FINDINGS REPORTED" in report
    assert "CKV_AWS_1" in report
