#!/usr/bin/env bash
# Package the Lambda, upload it, and deploy (or update) the stack.
#   ./scripts/deploy.sh you@example.com [single-instance|auto-scaling-group]
set -euo pipefail
EMAIL="${1:?usage: deploy.sh <alert-email> [mode]}"
MODE="${2:-single-instance}"
STACK="${STACK_NAME:-self-healing-ec2}"
REGION="${AWS_REGION:-$(aws configure get region)}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${ARTIFACT_BUCKET:-${STACK}-artifacts-${ACCOUNT}-${REGION}}"

aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || {
  if [ "$REGION" = "us-east-1" ]; then aws s3api create-bucket --bucket "$BUCKET"
  else aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration LocationConstraint="$REGION"; fi
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
}

aws cloudformation package --template-file template.yaml --s3-bucket "$BUCKET" \
  --output-template-file packaged.yaml
aws cloudformation deploy --template-file packaged.yaml --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides AlertEmail="$EMAIL" DeploymentMode="$MODE"
aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs" --output table
echo "Confirm the SNS subscription e-mail sent to $EMAIL, or you will not receive alerts."
