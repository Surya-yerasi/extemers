#!/usr/bin/env bash
# List (or delete) every resource tagged project=extemers across all regions,
# independent of Terraform state. A safety net: if state is ever lost/corrupted,
# or you just want to double-check `terraform destroy` got everything, this uses
# the Resource Groups Tagging API — the same source of truth AWS Billing uses
# for cost-allocation tags — to find anything with the umbrella tag.
#
# Usage:
#   ./scripts/audit_tags.sh              # list resources tagged project=extemers
#   ./scripts/audit_tags.sh --region us-west-2
#   PROJECT_TAG=other-project ./scripts/audit_tags.sh
set -euo pipefail

PROJECT_TAG="${PROJECT_TAG:-extemers}"
REGION="${AWS_REGION:-us-east-1}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

echo "Resources tagged project=${PROJECT_TAG} in ${REGION}:" >&2
echo >&2

aws resourcegroupstaggingapi get-resources \
  --region "$REGION" \
  --tag-filters "Key=project,Values=${PROJECT_TAG}" \
  --query 'ResourceTagMappingList[].ResourceARN' \
  --output table

echo >&2
echo "Note: CloudWatch Log Groups, API Gateway stages, and some other resource" >&2
echo "types are not always covered by this API. Cross-check with:" >&2
echo "  aws logs describe-log-groups --log-group-name-prefix /aws/lambda/<app>- --region $REGION" >&2
echo "  aws lambda list-functions --region $REGION --query \"Functions[?starts_with(FunctionName,'<app>-')].FunctionName\"" >&2
