#!/usr/bin/env bash
# Delete every resource tagged project=extemers that the Resource Groups Tagging
# API knows how to delete. Intended as a LAST RESORT when `terraform destroy`
# can't run (lost/corrupted state) — NOT a replacement for it. Always try
# `make tf-destroy` first.
#
# Requires explicit confirmation. Dry-run by default; pass --yes to actually delete.
#
# Usage:
#   ./scripts/nuke_orphans.sh                 # dry run: shows what would be deleted
#   ./scripts/nuke_orphans.sh --yes           # actually deletes
set -euo pipefail

PROJECT_TAG="${PROJECT_TAG:-extemers}"
REGION="${AWS_REGION:-us-east-1}"
CONFIRM=false

for arg in "$@"; do
  [[ "$arg" == "--yes" ]] && CONFIRM=true
done

echo "== Resources tagged project=${PROJECT_TAG} in ${REGION} ==" >&2
ARNS=$(aws resourcegroupstaggingapi get-resources \
  --region "$REGION" \
  --tag-filters "Key=project,Values=${PROJECT_TAG}" \
  --query 'ResourceTagMappingList[].ResourceARN' \
  --output text)

if [[ -z "$ARNS" ]]; then
  echo "Nothing found. Either already clean, or this account's state bucket/IAM" >&2
  echo "role were tagged separately (bootstrap stack is intentionally untagged)." >&2
  exit 0
fi

echo "$ARNS" | tr '\t' '\n'
echo >&2

if [[ "$CONFIRM" != true ]]; then
  echo "DRY RUN — nothing deleted. Re-run with --yes to actually delete the above." >&2
  exit 0
fi

read -r -p "Type the tag value '${PROJECT_TAG}' to confirm deletion: " typed
if [[ "$typed" != "$PROJECT_TAG" ]]; then
  echo "Confirmation did not match. Aborting." >&2
  exit 1
fi

echo "$ARNS" | tr '\t' '\n' | while read -r arn; do
  service=$(echo "$arn" | cut -d: -f3)
  case "$service" in
    lambda)
      name=$(echo "$arn" | awk -F: '{print $NF}')
      echo "Deleting Lambda function: $name"
      aws lambda delete-function --region "$REGION" --function-name "$name" || true
      ;;
    apigateway)
      api_id=$(echo "$arn" | sed -n 's#.*/apis/\([^/]*\)#\1#p')
      echo "Deleting API Gateway API: $api_id"
      aws apigatewayv2 delete-api --region "$REGION" --api-id "$api_id" || true
      ;;
    logs)
      lg=$(echo "$arn" | sed -n 's#.*log-group:\([^:]*\).*#\1#p')
      echo "Deleting log group: $lg"
      aws logs delete-log-group --region "$REGION" --log-group-name "$lg" || true
      ;;
    iam)
      role=$(echo "$arn" | awk -F/ '{print $NF}')
      echo "Skipping IAM role $role — delete manually after detaching/removing inline policies"
      ;;
    *)
      echo "No delete handler for service '$service' ($arn) — delete manually"
      ;;
  esac
done
