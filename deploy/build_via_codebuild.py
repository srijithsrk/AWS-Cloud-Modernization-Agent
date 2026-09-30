"""Builds and pushes the agent's container image without needing Docker
installed locally, by running the build inside AWS CodeBuild's native
ARM64 environment (see ../buildspec.yml).

This is the recommended path when you can't install Docker Desktop locally
(e.g. a locked-down corporate laptop). If you *do* have Docker with buildx,
you can skip this and follow the "docker buildx build" steps in README.md
instead - this script is only needed as a substitute for that.

Usage:
    python deploy/build_via_codebuild.py --repo-name cloud-modernization-agent --region us-east-1

Requires the IAM identity running this script to be able to create/use:
IAM roles, S3 buckets/objects, ECR repositories, and CodeBuild projects.
On a sandbox account, attaching AdministratorAccess to your test IAM user
is the simplest way to get this (see README.md).
"""
from __future__ import annotations

import argparse
import io
import json
import time
import zipfile
from pathlib import Path

import boto3

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Keep the upload small and avoid shipping secrets/dev-only artifacts.
_EXCLUDE_DIR_NAMES = {".venv", "__pycache__", ".git", ".pytest_cache", "tests", ".vscode"}
_EXCLUDE_FILE_NAMES = {"messages.json"}

_CODEBUILD_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "codebuild.amazonaws.com"}, "Action": "sts:AssumeRole"}
    ],
}

_TERMINAL_BUILD_STATUSES = {"SUCCEEDED", "FAILED", "FAULT", "STOPPED", "TIMED_OUT"}


def _zip_project_source() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in _PROJECT_ROOT.rglob("*"):
            if path.is_dir():
                continue
            if any(part in _EXCLUDE_DIR_NAMES for part in path.relative_to(_PROJECT_ROOT).parts):
                continue
            if path.name in _EXCLUDE_FILE_NAMES:
                continue
            zf.write(path, arcname=str(path.relative_to(_PROJECT_ROOT)))
    return buffer.getvalue()


def _ensure_bucket(s3, bucket_name: str, region: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket_name)
        return
    except Exception:  # noqa: BLE001 - bucket doesn't exist or isn't accessible yet
        pass
    kwargs = {}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(Bucket=bucket_name, **kwargs)


def _ensure_ecr_repository(ecr, repo_name: str) -> str:
    try:
        resp = ecr.create_repository(repositoryName=repo_name, imageScanningConfiguration={"scanOnPush": True})
        return resp["repository"]["repositoryUri"]
    except ecr.exceptions.RepositoryAlreadyExistsException:
        resp = ecr.describe_repositories(repositoryNames=[repo_name])
        return resp["repositories"][0]["repositoryUri"]


def _ensure_codebuild_role(iam, role_name: str, bucket_name: str) -> str:
    execution_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EcrAuth",
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                "Resource": "*",
            },
            {
                "Sid": "EcrPublicAuth",
                "Effect": "Allow",
                "Action": ["ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken"],
                "Resource": "*",
            },
            {
                "Sid": "EcrPush",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:PutImage",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                ],
                "Resource": "*",
            },
            {
                "Sid": "BuildLogs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": "*",
            },
            {
                "Sid": "ReadSource",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Resource": f"arn:aws:s3:::{bucket_name}/*",
            },
        ],
    }

    try:
        role_arn = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(_CODEBUILD_TRUST_POLICY),
            Description="Service role for CodeBuild to build/push the Cloud Modernization Agent image",
        )["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=role_name, PolicyDocument=json.dumps(_CODEBUILD_TRUST_POLICY))

    iam.put_role_policy(
        RoleName=role_name, PolicyName="codebuild-ecr-push-policy", PolicyDocument=json.dumps(execution_policy)
    )
    return role_arn


def _ensure_codebuild_project(
    codebuild, project_name: str, role_arn: str, source_location: str, env_vars: dict
) -> None:
    environment = {
        "type": "ARM_CONTAINER",
        "image": "aws/codebuild/amazonlinux2-aarch64-standard:3.0",
        "computeType": "BUILD_GENERAL1_MEDIUM",  # Rust/cargo compilation in the Dockerfile is CPU-heavy
        "privilegedMode": True,  # required to run `docker build` inside CodeBuild
        "environmentVariables": [{"name": k, "value": v} for k, v in env_vars.items()],
    }
    source = {"type": "S3", "location": source_location, "buildspec": "buildspec.yml"}
    artifacts = {"type": "NO_ARTIFACTS"}

    try:
        codebuild.create_project(
            name=project_name,
            source=source,
            artifacts=artifacts,
            environment=environment,
            serviceRole=role_arn,
            timeoutInMinutes=60,
        )
    except codebuild.exceptions.ResourceAlreadyExistsException:
        codebuild.update_project(
            name=project_name,
            source=source,
            artifacts=artifacts,
            environment=environment,
            serviceRole=role_arn,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-name", default="cloud-modernization-agent")
    parser.add_argument("--image-tag", default="latest")
    parser.add_argument("--region", default=None)
    parser.add_argument("--project-name", default="cloud-modernization-agent-build")
    parser.add_argument("--codebuild-role-name", default="cloud-modernization-agent-codebuild-role")
    args = parser.parse_args()

    session = boto3.session.Session(region_name=args.region)
    region = session.region_name
    account_id = session.client("sts").get_caller_identity()["Account"]

    bucket_name = f"{args.repo_name}-codebuild-{account_id}-{region}"
    source_key = "source.zip"

    print(f"Account: {account_id}  Region: {region}")

    print(f"Zipping project source and uploading to s3://{bucket_name}/{source_key} ...")
    s3 = session.client("s3")
    _ensure_bucket(s3, bucket_name, region)
    s3.put_object(Bucket=bucket_name, Key=source_key, Body=_zip_project_source())

    print(f"Ensuring ECR repository '{args.repo_name}' exists ...")
    ecr = session.client("ecr")
    repo_uri = _ensure_ecr_repository(ecr, args.repo_name)
    print(f"ECR repository URI: {repo_uri}")

    print("Ensuring CodeBuild service role exists ...")
    iam = session.client("iam")
    role_arn = _ensure_codebuild_role(iam, args.codebuild_role_name, bucket_name)
    print(f"CodeBuild role: {role_arn}")
    print("Waiting a few seconds for IAM role propagation ...")
    time.sleep(10)  # newly created IAM roles can take a few seconds to become assumable

    print(f"Ensuring CodeBuild project '{args.project_name}' exists ...")
    codebuild = session.client("codebuild")
    _ensure_codebuild_project(
        codebuild,
        args.project_name,
        role_arn,
        f"{bucket_name}/{source_key}",
        {
            "AWS_ACCOUNT_ID": account_id,
            "AWS_DEFAULT_REGION": region,
            "IMAGE_REPO_NAME": args.repo_name,
            "IMAGE_TAG": args.image_tag,
        },
    )

    print("Starting build ...")
    build_id = codebuild.start_build(projectName=args.project_name)["build"]["id"]
    print(f"Build started: {build_id}")

    while True:
        time.sleep(10)
        build = codebuild.batch_get_builds(ids=[build_id])["builds"][0]
        status = build["buildStatus"]
        phase = build.get("currentPhase", "?")
        print(f"  status={status} phase={phase}")
        if status in _TERMINAL_BUILD_STATUSES:
            logs = build.get("logs", {})
            print(f"CloudWatch logs: {logs.get('deepLink', 'n/a')}")
            break

    if status != "SUCCEEDED":
        raise SystemExit(f"Build did not succeed (status={status}). Check the CloudWatch logs above.")

    image_uri = f"{repo_uri}:{args.image_tag}"
    print(f"\nBuild succeeded. IMAGE_URI={image_uri}")
    print("Use this value as --image-uri for deploy/deploy_runtime.py.")


if __name__ == "__main__":
    main()
