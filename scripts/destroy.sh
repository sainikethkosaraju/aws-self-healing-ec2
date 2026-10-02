#!/usr/bin/env bash
# Delete the stack and the artifact bucket created by deploy.sh.
set -euo pipefail
STACK="${STACK_NAME:-self-healing-ec2}"
REGION="${AWS_REGION:-$(aws configure get region)}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${ARTIFACT_BUCKET:-${STACK}-artifacts-${ACCOUNT}-${REGION}}"
aws cloudformation delete-stack --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --stack-name "$STACK"
aws s3 rm "s3://$BUCKET" --recursive && aws s3api delete-bucket --bucket "$BUCKET" || true
echo "Deleted $STACK"
